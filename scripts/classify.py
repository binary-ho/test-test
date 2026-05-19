"""Tier classifier — decides which tests qualify as unit and enter the pipeline.

Signal-weighted classifier per ADR-2 §5.3. Pure stdlib + PyYAML if available
(falls back to a tiny inline YAML parser for the adapter dictionary).

Usage:

    python3 classify.py --root . \\
        --adapter ../../../contracts/adapters/python.pytest.yaml \\
        --out tier_classification.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SKILL_ROOT, load_yaml as _load_yaml  # noqa: E402


REPO_ROOT = SKILL_ROOT  # back-compat alias for any external imports


def _parse_minimal_yaml(text: str) -> dict:
    """Tiny YAML subset: scalar key:value, nested maps, '- value' lists.

    A key registered with an empty value is provisionally a nested map; the
    first '- value' line at deeper indent retroactively converts it to a list
    on its parent dict.
    """
    root: dict = {}
    stack = [(0, root)]
    last_list_key: Optional[tuple[int, str]] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        ctx = stack[-1][1]
        if line.startswith("- "):
            item = line[2:].strip().strip("\"'")
            if last_list_key:
                parent_indent, key = last_list_key
                # Find the parent dict at the recorded indent and convert its
                # key to a list (replacing the placeholder empty dict).
                for s_indent, s_ctx in stack:
                    if s_indent == parent_indent and isinstance(s_ctx, dict):
                        if not isinstance(s_ctx.get(key), list):
                            s_ctx[key] = []
                        s_ctx[key].append(item)
                        break
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().strip("\"'")
            if not v:
                ctx[k] = {}
                stack.append((indent + 2, ctx[k]))
                last_list_key = (indent, k)
            else:
                ctx[k] = v
                last_list_key = None
    return root


# ---- data model ------------------------------------------------------------

@dataclass
class Signal:
    kind: str
    weight: str
    evidence: str


@dataclass
class Classification:
    test_file: str
    test_id: str
    tier: str = "unknown"
    confidence: float = 0.0
    signals: list[Signal] = field(default_factory=list)
    decision: str = "needs_review"


_PATH_UNIT = re.compile(
    r"(^|/)tests?/unit/|/__tests__/|\.unit\.|/test_\w+\.py$|/\w+_test\.py$|"
    r"(^|/)tests?/test_|(^|/)src/test/[^/]+/.+Test\.(kt|java)$|"
    r"\.test\.tsx?$|\.spec\.tsx?$|\.test\.jsx?$|\.spec\.jsx?$"
)
_PATH_NON_UNIT = re.compile(
    r"(^|/)tests/(integration|e2e|smoke)/|/cypress/|/playwright/|"
    r"IntegrationTest\.(kt|java)$|E2ETest\.(kt|java)$"
)
# Tier/skip annotations are comment-based and work for // and # style comments.
_ANNOT_TIER = re.compile(r"(?://|#)\s*@validity:tier\s*=\s*(unit|integration|e2e)")
_ANNOT_SKIP = re.compile(r"(?://|#)\s*@validity:skip\b")
_FRAMEWORK_MARKER_DEFAULT = re.compile(r"@pytest\.mark\.(integration|e2e|slow)")


def _import_signals(content: str, adapter_dict: dict) -> list[Signal]:
    """Detect db/network/driver/mock imports — adapter supplies the names."""
    out: list[Signal] = []
    for i, line in enumerate(content.splitlines(), start=1):
        stripped = line.lstrip()
        if not (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or stripped.startswith("import\t")
        ):
            continue
        for kind in ("db_imports", "network_imports", "driver_imports", "mock_libs"):
            for needle in adapter_dict.get(kind, []) or []:
                if needle in line:
                    weight = {
                        "db_imports": "high",
                        "network_imports": "high",
                        "driver_imports": "categorical",
                        "mock_libs": "medium",
                    }[kind]
                    out.append(Signal(kind={
                        "db_imports": "db_import",
                        "network_imports": "network_import",
                        "driver_imports": "driver_import",
                        "mock_libs": "mock_lib",
                    }[kind], weight=weight, evidence=f"L{i}  {line.strip()}"))
    return out


def _decide_tier(signals: list[Signal]) -> tuple[str, float]:
    """Aggregate weighted signals -> (tier, confidence)."""
    weights = {"high": 0.35, "medium": 0.15, "low": 0.05, "categorical": 1.0, "overrides": 1.0}
    unit_score = 0.0
    non_unit_score = 0.0
    forced_tier: Optional[str] = None
    for s in signals:
        w = weights.get(s.weight, 0.1)
        if s.kind in {"path_unit", "mock_lib"}:
            unit_score += w
        elif s.kind in {"path_non_unit", "db_import", "network_import"}:
            non_unit_score += w
        elif s.kind == "driver_import":
            forced_tier = "e2e"
        elif s.kind == "framework_marker":
            forced_tier = "integration" if "integration" in s.evidence else "e2e"
        elif s.kind == "annotation":
            m = _ANNOT_TIER.search(s.evidence)
            if m:
                forced_tier = m.group(1)
    if forced_tier:
        return forced_tier, 1.0
    if non_unit_score > unit_score:
        return ("integration", min(1.0, 0.5 + non_unit_score))
    if unit_score > 0:
        return ("unit", min(1.0, 0.4 + unit_score))
    return ("unknown", 0.2)


def _decide_routing(tier: str, confidence: float, has_skip: bool) -> str:
    if has_skip:
        return "excluded"
    if tier == "unit" and confidence >= 0.7:
        return "included"
    if tier == "unit":
        return "needs_review"
    if tier in {"integration", "e2e"}:
        return "excluded"
    return "excluded"  # unknown


def _classify_test_in_file(
    test_file: Path,
    test_id: str,
    content: str,
    heur: dict,
    framework_marker_re: re.Pattern,
) -> Classification:
    rel = str(test_file)

    path_signals: list[Signal] = []
    if _PATH_UNIT.search(rel):
        path_signals.append(Signal("path_unit", "high", rel))
    if _PATH_NON_UNIT.search(rel):
        path_signals.append(Signal("path_non_unit", "high", rel))

    imp_signals = _import_signals(content, heur)

    file_annotations: list[Signal] = []
    has_skip_file = bool(_ANNOT_SKIP.search(content))
    for i, line in enumerate(content.splitlines(), start=1):
        if _ANNOT_TIER.search(line):
            file_annotations.append(Signal("annotation", "overrides", f"L{i}  {line.strip()}"))
        if framework_marker_re.search(line):
            file_annotations.append(Signal("framework_marker", "categorical", f"L{i}  {line.strip()}"))

    signals = path_signals + imp_signals + file_annotations
    tier, confidence = _decide_tier(signals)
    decision = _decide_routing(tier, confidence, has_skip_file)
    return Classification(
        test_file=rel, test_id=test_id,
        tier=tier, confidence=round(confidence, 2),
        signals=signals, decision=decision,
    )


def _discover_test_ids(root: Path, adapter: dict) -> list[str]:
    """Delegate to the adapter's test_discoverer; expects test_id strings on stdout."""
    discoverer_rel = adapter.get("implementations", {}).get("test_discoverer")
    if not discoverer_rel:
        return []
    discoverer = (REPO_ROOT / discoverer_rel).resolve()
    if not discoverer.is_file():
        print(f"[classify] adapter test_discoverer not found: {discoverer}", file=sys.stderr)
        return []
    proc = subprocess.run(
        [sys.executable, str(discoverer), "--root", str(root)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        print(f"[classify] discoverer exit {proc.returncode}: {proc.stderr[:240]}", file=sys.stderr)
        return []
    try:
        ids = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[classify] discoverer produced non-JSON output: {e}", file=sys.stderr)
        return []
    return [t for t in ids if isinstance(t, str) and "::" in t]


def classify_project(root: Path, adapter_path: Path) -> list[Classification]:
    adapter = _load_yaml(adapter_path)
    heur = adapter.get("tier_heuristic_dictionary", {})
    framework_marker_pattern = adapter.get("framework_marker_pattern")
    framework_marker_re = re.compile(framework_marker_pattern) if framework_marker_pattern else _FRAMEWORK_MARKER_DEFAULT

    test_ids = _discover_test_ids(root, adapter)

    # Group test_ids by file so each file is read once.
    grouped: dict[str, list[str]] = defaultdict(list)
    for tid in test_ids:
        file_part = tid.split("::", 1)[0]
        grouped[file_part].append(tid)

    results: list[Classification] = []
    for file_part, tids in sorted(grouped.items()):
        # discoverer paths may be repo-relative; resolve against root.
        file_path = Path(file_part)
        if not file_path.is_absolute():
            file_path = (root / file_part).resolve()
        if not file_path.is_file():
            print(f"[classify] test file not found: {file_path}", file=sys.stderr)
            continue
        try:
            content = file_path.read_text(errors="replace")
        except OSError as e:
            print(f"[classify] error reading {file_path}: {e}", file=sys.stderr)
            continue
        for tid in tids:
            results.append(_classify_test_in_file(file_path, tid, content, heur, framework_marker_re))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    results = classify_project(Path(args.root), Path(args.adapter))
    serialized = [
        {**asdict(r), "signals": [asdict(s) for s in r.signals]}
        for r in results
    ]
    payload = json.dumps(serialized, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(payload)
    else:
        Path(args.out).write_text(payload)
        excluded = sum(1 for r in results if r.decision == "excluded")
        included = sum(1 for r in results if r.decision == "included")
        review   = sum(1 for r in results if r.decision == "needs_review")
        print(
            f"[classify] {len(results)} tests — "
            f"included={included}, excluded={excluded}, needs_review={review}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
