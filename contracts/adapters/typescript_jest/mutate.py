"""TypeScript syntactic mutation operators.

Mask builder handles TS-specific lexical elements:

  * `'...'`, `"..."`, `` `...` `` (template literals are masked as a whole;
    `${...}` interpolations are not unmasked — v1 safety trade-off).
  * `//` line and `/* */` block comments (including `/** */`).
  * `===` / `!==` are not mutated (the ROR emitter's negative-lookahead
    handles this).

All operator emission delegated to `_operator_tables.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _operator_tables import (  # noqa: E402
    Mutant, hash_signature,
    line_starts, emit_aor, emit_ror, emit_cor, emit_lcr_paren_bang,
    emit_boundary, emit_constant,
)


_TS_COR = {"&&": "||", "||": "&&"}


def _build_masks(src: str) -> Tuple[List[bool], List[Tuple[int, int]]]:
    """Return (skip_mask, single-line-string-regions).

    Template literals (``...``) span multiple lines and are masked but not
    returned as string regions.
    """
    n = len(src)
    mask = [False] * n
    strings: List[Tuple[int, int]] = []
    i = 0
    state = "code"
    quote = ""
    str_open = -1
    while i < n:
        c = src[i]
        nxt2 = src[i:i + 2]
        if state == "code":
            if nxt2 == "//":
                mask[i] = mask[i + 1] = True
                state = "line_comment"
                i += 2
                continue
            if nxt2 == "/*":
                mask[i] = mask[i + 1] = True
                state = "block_comment"
                i += 2
                continue
            if c == "`":
                mask[i] = True
                state = "template"
                i += 1
                continue
            if c in "'\"":
                mask[i] = True
                state = "string"
                quote = c
                str_open = i
                i += 1
                continue
            i += 1
            continue
        if state == "line_comment":
            mask[i] = True
            if c == "\n":
                state = "code"
            i += 1
            continue
        if state == "block_comment":
            if nxt2 == "*/":
                mask[i] = mask[i + 1] = True
                state = "code"
                i += 2
                continue
            mask[i] = True
            i += 1
            continue
        if state == "string":
            mask[i] = True
            if c == "\\" and i + 1 < n:
                mask[i + 1] = True
                i += 2
                continue
            if c == quote:
                strings.append((str_open, i))
                state = "code"
                quote = ""
                str_open = -1
                i += 1
                continue
            if c == "\n":
                state = "code"
                quote = ""
                str_open = -1
                i += 1
                continue
            i += 1
            continue
        if state == "template":
            mask[i] = True
            if c == "\\" and i + 1 < n:
                mask[i + 1] = True
                i += 2
                continue
            if c == "`":
                state = "code"
                i += 1
                continue
            i += 1
            continue
    return mask, strings


_OPERATOR_NAMES = ["AOR", "ROR", "COR", "LCR", "BoundaryShift", "ConstantReplace"]


def generate_mutants(file: str, span: Optional[List[int]] = None,
                     operators: Optional[List[str]] = None) -> List[Mutant]:
    src = Path(file).read_text()
    mask, strings = _build_masks(src)
    starts = line_starts(src)
    ops = operators or _OPERATOR_NAMES
    out: List[Mutant] = []
    counter = 0
    for op_name in ops:
        if op_name == "AOR":
            it = emit_aor(src, mask, starts, file, span)
        elif op_name == "ROR":
            it = emit_ror(src, mask, starts, file, span)
        elif op_name == "COR":
            it = emit_cor(src, mask, starts, _TS_COR, file, span)
        elif op_name == "LCR":
            it = emit_lcr_paren_bang(src, mask, starts, file, span)
        elif op_name == "BoundaryShift":
            it = emit_boundary(src, mask, starts, file, span)
        elif op_name == "ConstantReplace":
            it = emit_constant(src, mask, starts, strings, file, span)
        else:
            continue
        for m in it:
            counter += 1
            m.id = f"m_{counter:04d}"
            m.mutant_signature_hash = hash_signature(m.file, m.span, m.operator, m.mutated)
            out.append(m)
    return out


def main():
    ap = argparse.ArgumentParser(description="Regex-based TypeScript mutator.")
    ap.add_argument("--file", required=True)
    ap.add_argument("--span", nargs=2, type=int, default=None)
    ap.add_argument("--operators", nargs="+", default=None,
                    choices=_OPERATOR_NAMES)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    mutants = generate_mutants(args.file, args.span, args.operators)
    payload = json.dumps([asdict(m) for m in mutants], ensure_ascii=False, indent=2)
    if args.out == "-":
        print(payload)
    else:
        Path(args.out).write_text(payload)
        print(f"[mutate] wrote {len(mutants)} mutants to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
