"""Enumerate Jest tests inside a TypeScript project.

Filesystem walk for `*.test.ts(x)?` / `*.spec.ts(x)?` and any file under
`__tests__/`. Parses `describe(...)` / `it(...)` / `test(...)` blocks at the
top level and emits `<rel-file>::<describe>::<test>` ids — same `::`
separator the Python adapter uses so the rest of the pipeline is agnostic.

Best-effort static scan; no TypeScript compiler involved.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


_TEST_FILE_PATTERNS = (
    "**/*.test.ts", "**/*.test.tsx",
    "**/*.spec.ts", "**/*.spec.tsx",
    "**/__tests__/**/*.ts", "**/__tests__/**/*.tsx",
)
_EXCLUDE_DIRS = {"node_modules", "dist", "build", ".next", "coverage"}

# `describe('name', () => {`  or `describe("name", function() {`
_DESCRIBE_RE = re.compile(r"""\bdescribe\s*\(\s*['"`]([^'"`]+)['"`]""")
# `it('name', ...)` / `test('name', ...)`  (also xdescribe / xit / fit / it.skip / etc.)
_TEST_RE = re.compile(
    r"""\b(?:it|test|xit|xtest|fit|test\.skip|it\.skip|test\.only|it\.only)\s*\(\s*['"`]([^'"`]+)['"`]"""
)


def _filtered(p: Path) -> bool:
    return any(part in _EXCLUDE_DIRS for part in p.parts)


def _strip_strings_comments(src: str) -> str:
    """Replace string and comment contents with `_` so describe/it inside
    them don't trigger false matches. Keeps offsets unchanged."""
    out = []
    i = 0
    n = len(src)
    state = "code"
    quote = ""
    while i < n:
        c = src[i]
        nxt2 = src[i:i + 2]
        if state == "code":
            if nxt2 == "//":
                out.append("__")
                state = "line_comment"
                i += 2
                continue
            if nxt2 == "/*":
                out.append("__")
                state = "block_comment"
                i += 2
                continue
            if c in "'\"`":
                out.append(c)
                state = "string"
                quote = c
                i += 1
                continue
            out.append(c)
            i += 1
        elif state == "line_comment":
            if c == "\n":
                state = "code"
                out.append(c)
            else:
                out.append("_")
            i += 1
        elif state == "block_comment":
            if nxt2 == "*/":
                out.append("__")
                state = "code"
                i += 2
                continue
            out.append("\n" if c == "\n" else "_")
            i += 1
        elif state == "string":
            if c == "\\" and i + 1 < n:
                out.append("__")
                i += 2
                continue
            if c == quote:
                out.append(c)
                state = "code"
                i += 1
                continue
            if c == "\n" and quote in "'\"":
                state = "code"
                out.append(c)
                i += 1
                continue
            out.append("_")
            i += 1
    return "".join(out)


def _describe_scopes(text: str, masked: str) -> list[tuple[int, int, str]]:
    """Return [(start, end, name)] for every describe block.

    `start` is the position of `describe`, `end` is one past the matching `}`.
    Brace matching uses the masked text so braces inside strings/comments
    don't confuse the count.
    """
    scopes: list[tuple[int, int, str]] = []
    for m in _DESCRIBE_RE.finditer(text):
        d_start = m.start()
        if masked[d_start] != text[d_start]:
            continue
        # Walk forward to the first `{` in code, then brace-match.
        i = m.end()
        n = len(masked)
        while i < n and masked[i] != "{":
            i += 1
        if i >= n:
            continue
        depth = 1
        j = i + 1
        while j < n and depth > 0:
            ch = masked[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        scopes.append((d_start, j, m.group(1)))
    return scopes


def _enclosing_describe(scopes: list[tuple[int, int, str]], pos: int) -> str:
    """Full describe path (outer > inner) for `pos`, joined by ' > ', else ""."""
    names: list[str] = []
    for s, e, n in scopes:
        if s <= pos < e:
            names.append(n)
    return " > ".join(names)


def discover_tests(root: str = ".") -> list[str]:
    root_p = Path(root).resolve()
    files: set[Path] = set()
    for pattern in _TEST_FILE_PATTERNS:
        for p in root_p.glob(pattern):
            if not _filtered(p):
                files.add(p)
    out: list[str] = []
    for path in sorted(files):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        masked = _strip_strings_comments(text)
        scopes = _describe_scopes(text, masked)
        rel = path.relative_to(root_p)
        for m in _TEST_RE.finditer(text):
            pos = m.start()
            if masked[pos] != text[pos]:
                continue  # inside a string/comment
            test_name = m.group(1)
            describe = _enclosing_describe(scopes, pos)
            qualified = f"{describe}::{test_name}" if describe else test_name
            out.append(f"{rel}::{qualified}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    print(json.dumps(discover_tests(args.root), indent=2))


if __name__ == "__main__":
    main()
