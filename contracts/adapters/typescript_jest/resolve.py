"""Best-effort subject-under-test resolver for TypeScript/Jest tests.

Heuristic (no TS compiler available):
  1. Parse `import` statements to learn what's available.
  2. Walk the named test's body looking for calls to imported symbols.
  3. Detect `jest.mock(...)` and `jest.fn()` to populate mocked_collaborators.
  4. Map import paths (`./calc`, `../src/foo`) to actual `.ts` / `.tsx`
     source files; fall back to leaving the relative path as-is.

Output shape mirrors `python_pytest/resolve.py` so the rest of the pipeline
is language-neutral.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import _strip_strings_comments  # type: ignore  # noqa: E402


_IMPORT_RE = re.compile(
    r"""^\s*import\s+(?P<lhs>.+?)\s+from\s+['"](?P<src>[^'"]+)['"]""",
    re.MULTILINE,
)
_NAMED_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_DEFAULT_RE = re.compile(r"^([A-Za-z_$][\w$]*)\b")
_NS_RE = re.compile(r"^\*\s+as\s+([A-Za-z_$][\w$]*)")
_JEST_MOCK_RE = re.compile(r"""\bjest\.mock\s*\(\s*['"]([^'"]+)['"]""")
_NEW_CALL_RE = re.compile(r"\bnew\s+([A-Z][\w$]*)\s*\(")
_PLAIN_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_DOT_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*\(")
_TEST_HEADER_RE = re.compile(
    r"""\b(?:it|test|xit|xtest|fit)\s*\(\s*['"`](?P<name>[^'"`]+)['"`]"""
)


_SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx")


def _parse_imports(text: str) -> dict[str, str]:
    """Return symbol -> source-module-spec ('./calc' / 'react' / ...)."""
    out: dict[str, str] = {}
    for m in _IMPORT_RE.finditer(text):
        lhs = m.group("lhs").strip()
        src = m.group("src")
        # mixed: default + { named } or default + * as ns
        parts = [p.strip() for p in re.split(r",\s*(?=(?:{|\*|[A-Za-z_$]))", lhs)]
        for part in parts:
            if not part:
                continue
            if part.startswith("{"):
                inner = _NAMED_BRACE_RE.search(part)
                if inner:
                    for nm in inner.group(1).split(","):
                        nm = nm.strip()
                        if not nm:
                            continue
                        if " as " in nm:
                            _orig, alias = (x.strip() for x in nm.split(" as ", 1))
                            out[alias] = src
                        else:
                            out[nm] = src
            elif part.startswith("*"):
                ns = _NS_RE.match(part)
                if ns:
                    out[ns.group(1)] = src
            else:
                dm = _DEFAULT_RE.match(part)
                if dm:
                    out[dm.group(1)] = src
    return out


def _resolve_module_path(test_file: Path, module: str) -> Optional[Path]:
    """./calc / ../foo  actual file on disk; bare 'react' returns None."""
    if not module.startswith("."):
        return None
    base = (test_file.parent / module).resolve()
    if base.is_dir():
        for ext in _SOURCE_EXTS:
            cand = base / f"index{ext}"
            if cand.is_file():
                return cand
    for ext in _SOURCE_EXTS:
        cand = base.with_suffix(ext)
        if cand.is_file():
            return cand
        cand = Path(str(base) + ext)
        if cand.is_file():
            return cand
    return None


def _find_test_body(text: str, masked: str, test_name: str) -> tuple[int, int]:
    """Return (start_line, end_line) for the body of `it/test(test_name, ...)`.

    Uses brace matching on the masked text so braces in strings don't confuse.
    Returns (0, 0) when the test is not found.
    """
    for m in _TEST_HEADER_RE.finditer(text):
        if masked[m.start()] != text[m.start()]:
            continue
        if m.group("name") != test_name:
            continue
        i = m.end()
        n = len(masked)
        while i < n and masked[i] != "{":
            i += 1
        if i >= n:
            return (0, 0)
        depth = 1
        j = i + 1
        while j < n and depth > 0:
            ch = masked[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        start_line = text[:i].count("\n") + 1
        end_line = text[:j].count("\n") + 1
        return (start_line, end_line)
    return (0, 0)


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


def _find_symbol_span(file_path: Path, symbol: str) -> list[int]:
    """Find the first declaration of `symbol` in a TS source file."""
    try:
        src = file_path.read_text(errors="replace")
    except OSError:
        return [0, 0]
    target = symbol.split(".")[-1]
    masked = _strip_strings_comments(src)
    patterns = [
        rf"\bclass\s+{re.escape(target)}\b",
        rf"\b(?:export\s+)?(?:async\s+)?function\s+{re.escape(target)}\b",
        rf"\b(?:export\s+)?(?:const|let|var)\s+{re.escape(target)}\b",
        rf"\b{re.escape(target)}\s*\(",  # method shorthand inside a class
    ]
    for pat in patterns:
        for m in re.finditer(pat, src):
            if masked[m.start()] != src[m.start()]:
                continue
            start_line = _line_of(src, m.start())
            # Brace-balance to find end.
            i = m.end()
            while i < len(masked) and masked[i] != "{":
                i += 1
            if i >= len(masked):
                return [start_line, start_line]
            depth = 1
            j = i + 1
            while j < len(masked) and depth > 0:
                if masked[j] == "{":
                    depth += 1
                elif masked[j] == "}":
                    depth -= 1
                j += 1
            end_line = _line_of(src, j)
            return [start_line, end_line]
    return [0, 0]


def resolve(test_file: str, test_id: str) -> dict:
    path = Path(test_file).resolve()
    text = path.read_text(errors="replace")
    masked = _strip_strings_comments(text)

    imports = _parse_imports(text)
    test_name = test_id.split("::")[-1]
    body_start, body_end = _find_test_body(text, masked, test_name)
    if body_start == 0:
        return {"test_id": test_id, "primary_subjects": [], "secondary_subjects": [],
                "mocked_collaborators": [], "confidence": 0.0}

    # Slice the body in the original text (so identifiers come through clean),
    # but also slice the masked version so we can detect strings/comments.
    lines = text.splitlines(keepends=True)
    body_text = "".join(lines[body_start - 1:body_end])
    body_masked = "".join(_strip_strings_comments(line) for line in lines[body_start - 1:body_end])

    seen: set[tuple[str, str]] = set()
    primary: list[dict] = []

    def add_subject(symbol: str):
        module = imports.get(symbol.split(".")[0])
        if not module:
            return
        if (symbol, module) in seen:
            return
        seen.add((symbol, module))
        resolved = _resolve_module_path(path, module)
        if resolved is None:
            return
        span = _find_symbol_span(resolved, symbol)
        primary.append({
            "file": str(resolved),
            "symbol": symbol.split(".")[0],
            "kind": "class" if symbol[:1].isupper() else "function",
            "span": span,
            "evidence": f"{test_file}:{body_start}",
        })

    for m in _NEW_CALL_RE.finditer(body_text):
        if body_masked[m.start()] == body_text[m.start()]:
            add_subject(m.group(1))
    for m in _DOT_CALL_RE.finditer(body_text):
        if body_masked[m.start()] == body_text[m.start()]:
            add_subject(m.group(1))
    for m in _PLAIN_CALL_RE.finditer(body_text):
        if body_masked[m.start()] == body_text[m.start()]:
            sym = m.group(1)
            if sym in {"describe", "it", "test", "expect", "beforeEach", "afterEach",
                       "beforeAll", "afterAll", "jest", "vi"}:
                continue
            add_subject(sym)

    mocked: list[dict] = []
    for m in _JEST_MOCK_RE.finditer(text):
        if masked[m.start()] != text[m.start()]:
            continue
        mod = m.group(1)
        resolved = _resolve_module_path(path, mod) if mod.startswith(".") else None
        mocked.append({"file": str(resolved) if resolved else mod, "symbol": mod.split("/")[-1]})

    confidence = min(1.0, 0.4 + 0.15 * len(primary)) if primary else 0.2
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
    ap.add_argument("--test-id", required=True)
    args = ap.parse_args()
    print(json.dumps(resolve(args.test_file, args.test_id), indent=2))


if __name__ == "__main__":
    main()
