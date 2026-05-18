"""Enumerate JUnit 5 tests inside a Kotlin/Gradle project root.

Filesystem walk for `*Test.kt` / `*Tests.kt` under `src/test/kotlin`, plus
any other directories the project uses. Extracts `@Test`-annotated fun
declarations and emits `file::Class.method` ids (file path is repo-relative).

This is a best-effort static scan — no Kotlin compiler involved.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


_TEST_FILE_PATTERNS = ("**/src/test/kotlin/**/*Test.kt", "**/src/test/kotlin/**/*Tests.kt")
_CLASS_RE = re.compile(r"^\s*(?:open\s+|abstract\s+|internal\s+)?class\s+(\w+)")
# Catches @Test, @ParameterizedTest, @RepeatedTest possibly preceded by other annotations
_TEST_ANNOT_RE = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory)\b")
_FUN_RE = re.compile(r"^\s*(?:@\w[\w.()\"\s,=]*\s+)*fun\s+`?([\w\s]+?)`?\s*\(")


def _enclosing_class(lines: list[str], idx: int) -> str:
    """Walk backwards from idx looking for the nearest class declaration."""
    for j in range(idx, -1, -1):
        m = _CLASS_RE.match(lines[j])
        if m:
            return m.group(1)
    return ""


def discover_tests(root: str = ".") -> list[str]:
    root_p = Path(root).resolve()
    files: list[Path] = []
    for pattern in _TEST_FILE_PATTERNS:
        files.extend(root_p.glob(pattern))
    out: list[str] = []
    for path in sorted(set(files)):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        rel = path.relative_to(root_p)
        i = 0
        while i < len(lines):
            if _TEST_ANNOT_RE.search(lines[i]):
                # Find the next fun declaration within a small window.
                for j in range(i, min(i + 6, len(lines))):
                    fm = _FUN_RE.match(lines[j])
                    if fm:
                        method = fm.group(1).strip()
                        cls = _enclosing_class(lines, j)
                        qualified = f"{cls}.{method}" if cls else method
                        out.append(f"{rel}::{qualified}")
                        i = j
                        break
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    print(json.dumps(discover_tests(args.root), indent=2))


if __name__ == "__main__":
    main()
