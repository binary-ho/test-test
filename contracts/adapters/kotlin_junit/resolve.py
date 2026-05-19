"""Best-effort subject-under-test resolver for Kotlin/JUnit tests.

Heuristic (no Kotlin compiler available):
  1. Read imports in the test file.
  2. Match imports to actual source files under `src/main/kotlin/...`.
  3. Inside the target test method body, look for calls of `Name(...)` or
     `name.method(...)` where the base symbol is imported. The first such
     match is treated as the primary subject.
  4. Detect `mockk<Foo>()` / `@MockK Foo` / `Mockito.mock(Foo::class)` as
     mocked collaborators.

Returns a TestSubjectMap fragment. Confidence is intentionally conservative.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional


_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)")
_CLASS_DECL_RE = re.compile(r"^\s*(?:open\s+|abstract\s+|internal\s+|data\s+|sealed\s+)?class\s+(\w+)")
_FUN_DECL_RE = re.compile(r"^\s*(?:@\w[\w.()\"\s,=]*\s+)*fun\s+`?([\w\s]+?)`?\s*\(")
_MOCKK_RE = re.compile(r"mockk\s*<\s*(\w+)\s*>|@MockK\s+\w+\s*:\s*(\w+)|Mockito\.mock\(\s*(\w+)::class")
_CALL_RE = re.compile(r"\b([A-Z][\w]*)\s*\(")  # capitalized identifier call → likely class/factory
_LOWER_CALL_RE = re.compile(r"\b([a-z][\w]*)\s*\(")


def _find_source_file(test_file: Path, fqn: str) -> Optional[Path]:
    """Walk up from test_file looking for src/main/kotlin/<path>.kt."""
    rel = fqn.replace(".", "/") + ".kt"
    cur = test_file.parent
    for _ in range(8):
        cand = cur / "src" / "main" / "kotlin" / rel
        if cand.is_file():
            return cand
        cur = cur.parent
        if cur == cur.parent:
            break
    # As a last resort, fall back to globbing from the project root the test lives in.
    for parent in [test_file.parent, *test_file.parents]:
        cand = parent / "src" / "main" / "kotlin" / rel
        if cand.is_file():
            return cand
    return None


def _find_symbol_span(file_path: Path, symbol: str) -> list[int]:
    try:
        lines = file_path.read_text(errors="replace").splitlines()
    except OSError:
        return [0, 0]
    target = symbol.split(".")[-1]
    start = 0
    for i, line in enumerate(lines, start=1):
        cls = _CLASS_DECL_RE.match(line)
        if cls and cls.group(1) == target:
            start = i
            break
        fn = _FUN_DECL_RE.match(line)
        if fn and fn.group(1).strip() == target:
            start = i
            break
    if not start:
        return [0, 0]
    # Naïve brace-balance to find function/class end.
    depth = 0
    end = start
    started = False
    for i in range(start - 1, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            started = True
        if started and depth <= 0:
            end = i + 1
            break
    return [start, max(end, start)]


def _extract_method_body(lines: list[str], method_name: str, class_name: str) -> tuple[list[str], int, int]:
    """Find the lines belonging to the given method within the given class."""
    in_class = not class_name  # if no class, treat whole file as scope
    class_depth = 0
    for i, line in enumerate(lines):
        if not in_class:
            cm = _CLASS_DECL_RE.match(line)
            if cm and cm.group(1) == class_name:
                in_class = True
                class_depth = 0
            else:
                continue
        if in_class:
            class_depth += line.count("{") - line.count("}")
        fm = _FUN_DECL_RE.match(line)
        if fm and fm.group(1).strip() == method_name:
            # Capture until matching brace.
            depth = 0
            started = False
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if "{" in lines[j]:
                    started = True
                if started and depth <= 0:
                    return lines[i:j + 1], i + 1, j + 1
            return lines[i:], i + 1, len(lines)
    return [], 0, 0


def resolve(test_file: str, test_id: str) -> dict:
    test_path = Path(test_file).resolve()
    try:
        content = test_path.read_text(errors="replace")
    except OSError:
        return {"test_id": test_id, "primary_subjects": [], "secondary_subjects": [],
                "mocked_collaborators": [], "confidence": 0.0}
    lines = content.splitlines()

    imports: dict[str, str] = {}
    test_package = ""
    for line in lines:
        m = _IMPORT_RE.match(line)
        if m:
            fqn = m.group(1)
            alias = m.group(2) or fqn.split(".")[-1]
            imports[alias] = fqn
            continue
        pm = _PACKAGE_RE.match(line)
        if pm:
            test_package = pm.group(1)

    qualified = test_id.split("::", 1)[1] if "::" in test_id else test_id
    if "." in qualified:
        class_name, method_name = qualified.split(".", 1)
    else:
        class_name, method_name = "", qualified

    body_lines, body_start, body_end = _extract_method_body(lines, method_name, class_name)
    body_text = "\n".join(body_lines)

    primary: list[dict] = []
    seen: set[str] = set()
    for m in _CALL_RE.finditer(body_text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        if name in imports:
            fqn = imports[name]
        elif test_package:
            # Same-package fallback: tests in `package X.Y.Z` can reference
            # `Foo()` without an import. Try `src/main/kotlin/X/Y/Z/Foo.kt`.
            fqn = f"{test_package}.{name}"
        else:
            continue
        source_file = _find_source_file(test_path, fqn)
        if source_file:
            span = _find_symbol_span(source_file, name)
            primary.append({
                "file": str(source_file),
                "symbol": fqn,
                "kind": "class",
                "span": span,
                "evidence": f"{test_path}:{body_start}",
            })

    mocked: list[dict] = []
    for m in _MOCKK_RE.finditer(content):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            fqn = imports.get(name, name)
            mocked.append({"file": fqn.replace(".", "/") + ".kt", "symbol": name})

    confidence = min(0.9, 0.3 + 0.15 * len(primary)) if primary else 0.2
    return {
        "test_id": test_id,
        "primary_subjects": primary,
        "secondary_subjects": [],
        "mocked_collaborators": mocked,
        "confidence": round(confidence, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-file", required=True)
    ap.add_argument("--test-id",   required=True)
    args = ap.parse_args()
    print(json.dumps(resolve(args.test_file, args.test_id), indent=2))


if __name__ == "__main__":
    main()
