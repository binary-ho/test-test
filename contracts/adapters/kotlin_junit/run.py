"""Kotlin/JUnit 5 test runner via Gradle.

Two responsibilities (matching `python_pytest/run.py`):

  1. `run_tests_for_mutant(mutant, test_ids, timeout, cwd)`  apply a
     mutant, invoke `./gradlew test --tests <FQN>` for the impacted tests,
     parse `build/test-results/test/*.xml` (JUnit XML) for kill/survive,
     roll back.
  2. `run_adversarial_case(case)`  drop a templated `.kt` test file into
     `src/test/kotlin/__adversarial/`, run it through Gradle, classify
     verdict, clean up.

JUnit XML is preferred over stdout parsing because Gradle's default test
logging does not include test names; XML is generated regardless.

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
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

from inject import MutationInjector


_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)")
_DEFAULT_TIMEOUT = 120


def _gradle_command(project_root: Path) -> list[str]:
    """Prefer ./gradlew, fall back to system `gradle`."""
    wrapper = project_root / "gradlew"
    if wrapper.exists() and os.access(wrapper, os.X_OK):
        return [str(wrapper)]
    if shutil.which("gradle"):
        return ["gradle"]
    raise RuntimeError("[run] no ./gradlew nor system gradle found")


def _guess_project_root(file_path: str) -> Path:
    """Walk up looking for the *outermost* Gradle root.

    Preference order:
      1. directory containing `gradlew` (the wrapper lives at the actual root)
      2. directory containing `settings.gradle(.kts)`
      3. directory containing `build.gradle(.kts)` (fallback for single-module)
    Multi-module projects (e.g. `outer/settings.gradle.kts` including
    `inner/`) need the outer dir — `gradlew` only exists at the outer level.
    """
    p = Path(file_path).resolve()
    settings_root: Optional[Path] = None
    build_root: Optional[Path] = None
    for cur in [p.parent, *p.parents]:
        if (cur / "gradlew").is_file():
            return cur
        if settings_root is None and (
            (cur / "settings.gradle.kts").is_file() or (cur / "settings.gradle").is_file()
        ):
            settings_root = cur
        if build_root is None and (
            (cur / "build.gradle.kts").is_file() or (cur / "build.gradle").is_file()
        ):
            build_root = cur
    return settings_root or build_root or p.parent


def _extract_package(file_path: Path) -> str:
    try:
        for line in file_path.read_text(errors="replace").splitlines():
            m = _PACKAGE_RE.match(line)
            if m:
                return m.group(1)
            stripped = line.strip()
            if stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                # Past the package declaration with no match — fall through.
                break
    except OSError:
        pass
    return ""


def _qualify(test_id: str, project_root: Path) -> str:
    """`src/test/kotlin/.../FooTest.kt::FooTest.shouldX`  `pkg.FooTest.shouldX`."""
    file_part, _, name_part = test_id.partition("::")
    if not name_part:
        return test_id
    abs_path = (project_root / file_part).resolve() if not Path(file_part).is_absolute() else Path(file_part)
    pkg = _extract_package(abs_path) if abs_path.is_file() else ""
    return f"{pkg}.{name_part}" if pkg else name_part


def _collect_xml_results(project_root: Path) -> Tuple[list[str], list[str]]:
    """Walk every `build/test-results/test/TEST-*.xml` under the project root.

    Returns (passed_ids, failed_ids) using `<class>.<method>` format.
    """
    passed: list[str] = []
    failed: list[str] = []
    for xml_path in project_root.rglob("build/test-results/test/TEST-*.xml"):
        try:
            root = ET.parse(xml_path).getroot()
        except (ET.ParseError, OSError):
            continue
        for tc in root.iter("testcase"):
            classname = tc.attrib.get("classname", "")
            name = tc.attrib.get("name", "")
            tid = f"{classname}.{name}"
            if tc.find("failure") is not None or tc.find("error") is not None:
                failed.append(tid)
            else:
                passed.append(tid)
    return passed, failed


def _clear_previous_results(project_root: Path) -> None:
    for d in project_root.rglob("build/test-results/test"):
        try:
            shutil.rmtree(d)
        except OSError:
            pass


def run_tests_for_mutant(mutant: dict, test_ids: list[str], timeout: int = _DEFAULT_TIMEOUT,
                         cwd: Optional[str] = None) -> dict:
    project_root = Path(cwd) if cwd else _guess_project_root(mutant["file"])
    try:
        base_cmd = _gradle_command(project_root)
    except RuntimeError as e:
        return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
                "wall_clock_seconds": 0, "detail": str(e), "cache_status": "miss"}

    qualified = [_qualify(t, project_root) for t in test_ids if t]
    cmd = base_cmd + ["test", "--no-daemon", "-q"]
    for tname in qualified:
        cmd += ["--tests", tname]

    started = time.monotonic()
    try:
        with MutationInjector(mutant):
            _clear_previous_results(project_root)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=timeout, check=False, cwd=str(project_root),
                )
            except subprocess.TimeoutExpired:
                return {"mutant_id": mutant["id"], "status": "timeout",
                        "killing_tests": [], "wall_clock_seconds": timeout, "cache_status": "miss"}
    except Exception as e:
        return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
                "wall_clock_seconds": round(time.monotonic() - started, 2),
                "detail": str(e), "cache_status": "miss"}

    elapsed = round(time.monotonic() - started, 2)
    passed, failed = _collect_xml_results(project_root)

    if proc.returncode == 0 and not failed:
        # All requested tests passed under the mutant => mutant survived.
        return {"mutant_id": mutant["id"], "status": "survived", "killing_tests": [],
                "wall_clock_seconds": elapsed, "cache_status": "miss"}
    if failed:
        return {"mutant_id": mutant["id"], "status": "killed",
                "killing_tests": failed, "wall_clock_seconds": elapsed, "cache_status": "miss"}
    # returncode != 0 but no test failures in XML — could be compile error
    # (kotlinc rejected the mutated source, which counts as the codebase
    # catching the mutation) or genuine infrastructure failure.
    combined = (proc.stderr + proc.stdout).lower()
    infra_error = (
        "no such task" in combined
        or "could not resolve" in combined
        or "could not download" in combined
        or "unable to access" in combined
        or "connection refused" in combined
        or "out of memory" in combined
    )
    if infra_error or not passed and not failed and proc.returncode != 0 and len(combined) < 50:
        return {"mutant_id": mutant["id"], "status": "error", "killing_tests": [],
                "wall_clock_seconds": elapsed,
                "detail": (proc.stderr or proc.stdout)[-400:], "cache_status": "miss"}
    # Default: no tests ran AND gradle returned non-zero → compile rejected
    # the mutation. Counted as killed (the codebase caught it).
    return {"mutant_id": mutant["id"], "status": "killed",
            "killing_tests": ["<kotlinc>"],
            "wall_clock_seconds": elapsed,
            "detail": "killed by compiler",
            "cache_status": "miss"}


# --- adversarial case ------------------------------------------------------

_ADVERSARIAL_TEMPLATE = textwrap.dedent("""\
    package __adversarial

    {import_line}

    class {class_name} {{
        @org.junit.jupiter.api.Test
        fun adversarial() {{
            val observed = {invocation}
            val expectedRepr = {expected_repr}
            if (expectedRepr != "ANY" && observed.toString() != expectedRepr) {{
                throw AssertionError("adversarial mismatch: observed=" + observed + " expected=" + expectedRepr)
            }}
        }}
    }}
""")


def _safe_class_name(case_id: str) -> str:
    return "AdvCase_" + re.sub(r"[^A-Za-z0-9_]", "_", case_id)


def run_adversarial_case(case: dict) -> dict:
    sym = case.get("target_symbol", "")
    if ":" in sym:
        source_path_str, symbol = sym.split(":", 1)
    else:
        source_path_str, symbol = sym, ""
    if not source_path_str:
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": "missing target_symbol", "wall_clock_seconds": 0}

    source_path = Path(source_path_str)
    project_root = _guess_project_root(str(source_path))
    package = _extract_package(source_path)
    import_line = f"import {package}.*" if package else ""
    invocation = case.get("invocation", "Unit")
    expected = case.get("expected", "ANY")
    expected_repr = json.dumps(str(expected))

    class_name = _safe_class_name(case["case_id"])
    adv_dir = project_root / "src" / "test" / "kotlin" / "__adversarial"
    adv_dir.mkdir(parents=True, exist_ok=True)
    test_file = adv_dir / f"{class_name}.kt"
    rendered = _ADVERSARIAL_TEMPLATE.format(
        import_line=import_line, class_name=class_name,
        invocation=invocation, expected_repr=expected_repr,
    )
    test_file.write_text(rendered)

    try:
        base_cmd = _gradle_command(project_root)
    except RuntimeError as e:
        test_file.unlink(missing_ok=True)
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": str(e), "wall_clock_seconds": 0}

    cmd = base_cmd + ["test", "--no-daemon", "-q",
                      "--tests", f"__adversarial.{class_name}.adversarial"]
    started = time.monotonic()
    try:
        _clear_previous_results(project_root)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=case.get("timeout", _DEFAULT_TIMEOUT),
                              check=False, cwd=str(project_root))
    except subprocess.TimeoutExpired:
        test_file.unlink(missing_ok=True)
        return {"case_id": case["case_id"], "verdict": "inconclusive",
                "observed_behavior": "timeout", "wall_clock_seconds": case.get("timeout", _DEFAULT_TIMEOUT)}
    finally:
        try:
            test_file.unlink()
        except OSError:
            pass

    elapsed = round(time.monotonic() - started, 2)
    passed, failed = _collect_xml_results(project_root)
    case_id_full = f"__adversarial.{class_name}.adversarial"

    if proc.returncode == 0 and case_id_full in passed:
        verdict = "caught_by_existing_test"
    elif case_id_full in failed:
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
