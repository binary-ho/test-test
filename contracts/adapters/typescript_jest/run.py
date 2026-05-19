"""TypeScript/Jest test runner.

Two responsibilities (matching `python_pytest/run.py` and the Kotlin
adapter):

  1. `run_tests_for_mutant(mutant, test_ids, timeout, cwd)`  apply a
     mutant, invoke jest scoped to the impacted tests, parse jest's
     `--json` output for kill/survive, roll back.
  2. `run_adversarial_case(case)`  drop a templated `.test.ts` next to
     the target source, run it through jest, classify verdict.

Package manager: prefers `npx` (which works across npm / pnpm / yarn
installations); if a `pnpm-lock.yaml` or `yarn.lock` is detected, uses
`pnpm exec` / `yarn` instead.

CLI:

    python run.py --mutant mutant.json --tests t1 t2 --timeout 60
    python run.py --case case.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from inject import MutationInjector


_DEFAULT_TIMEOUT = 120


def _guess_project_root(file_path: str) -> Path:
    p = Path(file_path).resolve()
    for cur in [p.parent, *p.parents]:
        if (cur / "package.json").is_file():
            return cur
    return p.parent


def _jest_invocation(project_root: Path) -> list[str]:
    """Pick the package-manager-appropriate jest invocation."""
    if (project_root / "pnpm-lock.yaml").is_file() and shutil.which("pnpm"):
        return ["pnpm", "exec", "jest"]
    if (project_root / "yarn.lock").is_file() and shutil.which("yarn"):
        return ["yarn", "jest"]
    if shutil.which("npx"):
        return ["npx", "--no-install", "jest"]
    if shutil.which("jest"):
        return ["jest"]
    raise RuntimeError("[run] no jest / npx / pnpm / yarn on PATH")


def _split_test_id(test_id: str) -> tuple[str, str, str]:
    """`<file>::<describe path>::<test name>`  (file, describe, test)."""
    parts = test_id.split("::")
    if len(parts) >= 3:
        return parts[0], "::".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], "", ""


def _escape_for_regex(s: str) -> str:
    return re.escape(s)


def _build_jest_args(test_ids: list[str]) -> list[str]:
    """Translate (file, name) pairs into jest filter flags.

    Jest accepts one `--testPathPattern` and one `--testNamePattern`; if we
    have multiple distinct files or names, we run them via a regex union.
    """
    files: list[str] = []
    names: list[str] = []
    for tid in test_ids:
        f, describe, name = _split_test_id(tid)
        if f:
            files.append(_escape_for_regex(f))
        composed = (describe + " " + name).strip() if describe else name
        if composed:
            names.append(_escape_for_regex(composed))
    args: list[str] = []
    if files:
        args += ["--testPathPattern", f"({'|'.join(sorted(set(files)))})"]
    if names:
        args += ["--testNamePattern", f"({'|'.join(sorted(set(names)))})"]
    return args


def _parse_jest_json(payload: str) -> tuple[list[str], list[str]]:
    """Return (passed_ids, failed_ids) from a jest `--json` stdout blob."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return [], []
    passed: list[str] = []
    failed: list[str] = []
    for tr in data.get("testResults", []):
        file_path = tr.get("name") or tr.get("testFilePath") or ""
        for ar in tr.get("assertionResults", []) or tr.get("testResults", []):
            full = ar.get("fullName") or " > ".join(ar.get("ancestorTitles", []) + [ar.get("title", "")])
            tid = f"{file_path}::{full.strip()}"
            status = ar.get("status", "")
            if status == "passed":
                passed.append(tid)
            elif status in ("failed", "broken", "errored"):
                failed.append(tid)
    return passed, failed


def run_tests_for_mutant(mutant: dict, test_ids: list[str], timeout: int = _DEFAULT_TIMEOUT,
                         cwd: Optional[str] = None) -> dict:
    project_root = Path(cwd) if cwd else _guess_project_root(mutant["file"])
    try:
        base_cmd = _jest_invocation(project_root)
    except RuntimeError as e:
        return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
                "wall_clock_seconds": 0, "detail": str(e), "cache_status": "miss"}

    jest_args = _build_jest_args(test_ids)
    cmd = base_cmd + ["--json", "--silent", "--runInBand"] + jest_args

    started = time.monotonic()
    try:
        with MutationInjector(mutant):
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, check=False, cwd=str(project_root),
                    env={**os.environ, "CI": "true"},
                )
            except subprocess.TimeoutExpired:
                return {"mutant_id": mutant["id"], "status": "timeout",
                        "killing_tests": [], "wall_clock_seconds": timeout, "cache_status": "miss"}
    except Exception as e:
        return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
                "wall_clock_seconds": round(time.monotonic() - started, 2),
                "detail": str(e), "cache_status": "miss"}

    elapsed = round(time.monotonic() - started, 2)
    passed, failed = _parse_jest_json(proc.stdout)

    if proc.returncode == 0 and not failed:
        return {"mutant_id": mutant["id"], "status": "survived", "killing_tests": [],
                "wall_clock_seconds": elapsed, "cache_status": "miss"}
    if failed:
        return {"mutant_id": mutant["id"], "status": "killed",
                "killing_tests": failed, "wall_clock_seconds": elapsed, "cache_status": "miss"}
    return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
            "wall_clock_seconds": elapsed,
            "detail": (proc.stderr or proc.stdout)[-400:], "cache_status": "miss"}


# --- adversarial case ------------------------------------------------------

_ADV_TEMPLATE = """\
// Auto-generated by test-validity-evaluator adversarial runner.
// case_id: {case_id}
{import_line}

test('adversarial_{safe_id}', () => {{
  const observed = {invocation};
  const expectedRepr = {expected_repr};
  if (expectedRepr !== 'ANY' && JSON.stringify(observed) !== expectedRepr) {{
    throw new Error('adversarial mismatch: observed=' + JSON.stringify(observed) +
                    ' expected=' + expectedRepr);
  }}
}});
"""


def _safe_id(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", case_id)


def _import_for(target_symbol: str, target_file: Path, test_file: Path) -> str:
    if ":" not in target_symbol:
        return ""
    _, symbol = target_symbol.split(":", 1)
    base = symbol.split(".")[0]
    rel = os.path.relpath(target_file.with_suffix(""), test_file.parent)
    if not rel.startswith("."):
        rel = "./" + rel
    return f"import {{ {base} }} from '{rel}';"


def run_adversarial_case(case: dict) -> dict:
    sym = case.get("target_symbol", "")
    if ":" not in sym:
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": "missing target_symbol path:symbol",
                "wall_clock_seconds": 0}
    source_path_str, _ = sym.split(":", 1)
    source_path = Path(source_path_str).resolve()
    if not source_path.is_file():
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": f"target file not found: {source_path}",
                "wall_clock_seconds": 0}

    project_root = _guess_project_root(str(source_path))
    safe = _safe_id(case["case_id"])
    adv_dir = project_root / "__adversarial"
    adv_dir.mkdir(parents=True, exist_ok=True)
    test_file = adv_dir / f"adv_{safe}.test.ts"
    import_line = _import_for(sym, source_path, test_file)
    invocation = case.get("invocation", "undefined")
    expected = case.get("expected", "ANY")
    expected_repr = json.dumps(str(expected))

    test_file.write_text(_ADV_TEMPLATE.format(
        case_id=case["case_id"], safe_id=safe, import_line=import_line,
        invocation=invocation, expected_repr=expected_repr,
    ))

    try:
        base_cmd = _jest_invocation(project_root)
    except RuntimeError as e:
        test_file.unlink(missing_ok=True)
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": str(e), "wall_clock_seconds": 0}

    cmd = base_cmd + ["--json", "--silent", "--runInBand",
                      "--testPathPattern", _escape_for_regex(str(test_file))]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=case.get("timeout", _DEFAULT_TIMEOUT),
                              check=False, cwd=str(project_root),
                              env={**os.environ, "CI": "true"})
    except subprocess.TimeoutExpired:
        test_file.unlink(missing_ok=True)
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": "timeout",
                "wall_clock_seconds": case.get("timeout", _DEFAULT_TIMEOUT)}
    finally:
        try:
            test_file.unlink()
        except OSError:
            pass

    elapsed = round(time.monotonic() - started, 2)
    passed, failed = _parse_jest_json(proc.stdout)
    if proc.returncode == 0 and passed:
        verdict = "caught_by_existing_test"
    elif failed:
        verdict = "uncaught"
    else:
        verdict = "inconclusive"
    return {"case_id": case["case_id"], "verdict": verdict,
            "observed_behavior": (proc.stdout + proc.stderr)[-400:],
            "wall_clock_seconds": elapsed}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--mutant", help="mutant JSON path")
    g.add_argument("--case", help="adversarial case JSON path")
    ap.add_argument("--tests", nargs="*", default=[])
    ap.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    ap.add_argument("--cwd", default=None)
    args = ap.parse_args()

    if args.mutant:
        m = json.loads(Path(args.mutant).read_text())
        print(json.dumps(run_tests_for_mutant(m, args.tests, args.timeout, args.cwd), indent=2))
    else:
        c = json.loads(Path(args.case).read_text())
        print(json.dumps(run_adversarial_case(c), indent=2))


if __name__ == "__main__":
    main()
