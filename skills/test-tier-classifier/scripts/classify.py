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
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---- minimal YAML loader (adapter manifests use simple subset) -------------

def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text())
    except ImportError:
        return _parse_minimal_yaml(path.read_text())


def _parse_minimal_yaml(text: str) -> dict:
    """Tiny YAML subset: scalar key:value, nested maps, '- value' lists."""
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
                parent = stack[-1][1] if stack[-1][0] == parent_indent else ctx
                parent.setdefault(key, []).append(item)
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


_PATH_UNIT = re.compile(r"(^|/)tests/unit/|/__tests__/|\.unit\.|_test\.py$|^test_")
_PATH_NON_UNIT = re.compile(r"(^|/)tests/(integration|e2e|smoke)/|/cypress/|/playwright/")
_ANNOT_TIER = re.compile(r"#\s*@validity:tier\s*=\s*(unit|integration|e2e)")
_ANNOT_SKIP = re.compile(r"#\s*@validity:skip\b")
_FRAMEWORK_MARKER = re.compile(r"@pytest\.mark\.(integration|e2e|slow)")
_TEST_DEF = re.compile(r"^def\s+(test_\w+)\s*\(")


def _import_signals(content: str, adapter_dict: dict) -> list[Signal]:
    """Detect db/network/driver/mock imports — adapter supplies the names."""
    out: list[Signal] = []
    for i, line in enumerate(content.splitlines(), start=1):
        if not (line.startswith("import ") or line.startswith("from ")):
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


def classify_file(test_file: Path, adapter_dict: dict) -> list[Classification]:
    content = test_file.read_text()
    rel = str(test_file)
    out: list[Classification] = []

    path_signals: list[Signal] = []
    if _PATH_UNIT.search(rel):
        path_signals.append(Signal("path_unit", "high", rel))
    if _PATH_NON_UNIT.search(rel):
        path_signals.append(Signal("path_non_unit", "high", rel))

    imp_signals = _import_signals(content, adapter_dict)

    file_annotations: list[Signal] = []
    has_skip_file = bool(_ANNOT_SKIP.search(content))
    for i, line in enumerate(content.splitlines(), start=1):
        if _ANNOT_TIER.search(line):
            file_annotations.append(Signal("annotation", "overrides", f"L{i}  {line.strip()}"))
        if _FRAMEWORK_MARKER.search(line):
            file_annotations.append(Signal("framework_marker", "categorical", f"L{i}  {line.strip()}"))

    for i, line in enumerate(content.splitlines(), start=1):
        m = _TEST_DEF.match(line)
        if not m:
            continue
        test_id = f"{rel}::{m.group(1)}"
        signals = path_signals + imp_signals + file_annotations
        tier, confidence = _decide_tier(signals)
        decision = _decide_routing(tier, confidence, has_skip_file)
        out.append(Classification(
            test_file=rel, test_id=test_id,
            tier=tier, confidence=round(confidence, 2),
            signals=signals, decision=decision,
        ))
    return out


def classify_project(root: Path, adapter_path: Path) -> list[Classification]:
    adapter = _load_yaml(adapter_path)
    heur = adapter.get("tier_heuristic_dictionary", {})
    results: list[Classification] = []
    for p in sorted(root.rglob("test_*.py")):
        # skip self
        if "skills/test-tier-classifier" in str(p):
            continue
        try:
            results.extend(classify_file(p, heur))
        except Exception as e:
            print(f"[classify] error on {p}: {e}", file=sys.stderr)
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
