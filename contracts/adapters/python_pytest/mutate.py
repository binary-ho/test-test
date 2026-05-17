"""Syntactic AST-based mutation operators for Python.

Used by `mutation-operator-syntactic` step of skill A. Each operator visits
the source AST and yields candidate Mutant dicts. Pure stdlib; no third-party
deps.

Usage from CLI:

    python mutate.py --file src/foo.py --span 40 72 \\
                     --operators AOR ROR BoundaryShift \\
                     --out mutants.json
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class Mutant:
    id: str
    operator: str
    file: str
    span: List[int]
    original: str
    mutated: str
    intent: str
    equivalence_status: str = "unknown"
    mutant_signature_hash: str = ""


# --- operator tables ---------------------------------------------------------

_AOR = {  # arithmetic
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.Div, ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult, ast.Mod: ast.Mult,
}

_ROR = {  # relational
    ast.Lt: [ast.LtE, ast.Eq, ast.GtE],
    ast.LtE: [ast.Lt, ast.Eq, ast.Gt],
    ast.Gt: [ast.GtE, ast.Eq, ast.LtE],
    ast.GtE: [ast.Gt, ast.Eq, ast.Lt],
    ast.Eq: [ast.NotEq],
    ast.NotEq: [ast.Eq],
}

_COR = {  # conditional
    ast.And: ast.Or, ast.Or: ast.And,
}


def _op_symbol(op_node) -> str:
    symbols = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.FloorDiv: "//", ast.Mod: "%",
        ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
        ast.Eq: "==", ast.NotEq: "!=",
        ast.And: "and", ast.Or: "or", ast.Not: "not",
    }
    return symbols.get(type(op_node), type(op_node).__name__)


def _hash_signature(file: str, span: List[int], operator: str, mutated: str) -> str:
    h = hashlib.sha256()
    h.update(f"{file}|{span[0]}-{span[1]}|{operator}|{mutated}".encode())
    return f"sha256:{h.hexdigest()[:16]}"


def _within_span(node: ast.AST, span: Optional[List[int]]) -> bool:
    if span is None:
        return True
    if not hasattr(node, "lineno"):
        return False
    return span[0] <= node.lineno <= span[1]


def _line_text(src_lines: List[str], lineno: int) -> str:
    return src_lines[lineno - 1] if 0 < lineno <= len(src_lines) else ""


# --- per-operator generators -------------------------------------------------

def _mutate_aor(tree, src_lines, file, span) -> Iterable[Mutant]:
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and type(node.op) in _AOR and _within_span(node, span):
            new_op_cls = _AOR[type(node.op)]
            line = _line_text(src_lines, node.lineno)
            before, after = _op_symbol(node.op), _op_symbol(new_op_cls())
            mutated_line = line.replace(before, after, 1)
            yield Mutant(
                id="", operator="AOR", file=file,
                span=[node.lineno, node.end_lineno or node.lineno],
                original=line.rstrip("\n"),
                mutated=mutated_line.rstrip("\n"),
                intent=f"arithmetic: {before} -> {after}",
            )


def _mutate_ror(tree, src_lines, file, span) -> Iterable[Mutant]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _within_span(node, span):
            for i, cmp_op in enumerate(node.ops):
                replacements = _ROR.get(type(cmp_op))
                if not replacements:
                    continue
                line = _line_text(src_lines, node.lineno)
                before = _op_symbol(cmp_op)
                for new_cls in replacements:
                    after = _op_symbol(new_cls())
                    mutated_line = line.replace(before, after, 1)
                    if mutated_line == line:
                        continue
                    yield Mutant(
                        id="", operator="ROR", file=file,
                        span=[node.lineno, node.end_lineno or node.lineno],
                        original=line.rstrip("\n"),
                        mutated=mutated_line.rstrip("\n"),
                        intent=f"relational: {before} -> {after}",
                    )


def _mutate_cor(tree, src_lines, file, span) -> Iterable[Mutant]:
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and type(node.op) in _COR and _within_span(node, span):
            line = _line_text(src_lines, node.lineno)
            before, after = _op_symbol(node.op), _op_symbol(_COR[type(node.op)]())
            mutated_line = line.replace(f" {before} ", f" {after} ", 1)
            if mutated_line == line:
                continue
            yield Mutant(
                id="", operator="COR", file=file,
                span=[node.lineno, node.end_lineno or node.lineno],
                original=line.rstrip("\n"),
                mutated=mutated_line.rstrip("\n"),
                intent=f"conditional: {before} -> {after}",
            )


def _mutate_lcr(tree, src_lines, file, span) -> Iterable[Mutant]:
    """Insert / remove a boolean negation on If conditions."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _within_span(node, span):
            line = _line_text(src_lines, node.lineno)
            if "if not " in line:
                mutated_line = line.replace("if not ", "if ", 1)
                intent = "logical: remove negation"
            else:
                mutated_line = line.replace("if ", "if not ", 1)
                intent = "logical: insert negation"
            if mutated_line == line:
                continue
            yield Mutant(
                id="", operator="LCR", file=file,
                span=[node.lineno, node.end_lineno or node.lineno],
                original=line.rstrip("\n"),
                mutated=mutated_line.rstrip("\n"),
                intent=intent,
            )


def _mutate_boundary(tree, src_lines, file, span) -> Iterable[Mutant]:
    """Off-by-one: integer literals n -> n+1, n-1; range bounds."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and _within_span(node, span):
            line = _line_text(src_lines, node.lineno)
            for shift in (+1, -1):
                new_val = node.value + shift
                before = str(node.value)
                after = str(new_val)
                mutated_line = line.replace(before, after, 1)
                if mutated_line == line:
                    continue
                yield Mutant(
                    id="", operator="BoundaryShift", file=file,
                    span=[node.lineno, node.end_lineno or node.lineno],
                    original=line.rstrip("\n"),
                    mutated=mutated_line.rstrip("\n"),
                    intent=f"boundary: {before} -> {after}",
                )


def _mutate_constant(tree, src_lines, file, span) -> Iterable[Mutant]:
    """Replace integer with 0 (and 0 with 1); replace nonempty string with empty."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not _within_span(node, span):
            continue
        line = _line_text(src_lines, node.lineno)
        if isinstance(node.value, int):
            target_val = 1 if node.value == 0 else 0
            before, after = str(node.value), str(target_val)
            mutated_line = line.replace(before, after, 1)
            if mutated_line == line:
                continue
            yield Mutant(
                id="", operator="ConstantReplace", file=file,
                span=[node.lineno, node.end_lineno or node.lineno],
                original=line.rstrip("\n"),
                mutated=mutated_line.rstrip("\n"),
                intent=f"constant: {before} -> {after}",
            )
        elif isinstance(node.value, str) and node.value:
            quoted_before = repr(node.value)
            mutated_line = line.replace(quoted_before, "''", 1)
            if mutated_line == line:
                continue
            yield Mutant(
                id="", operator="ConstantReplace", file=file,
                span=[node.lineno, node.end_lineno or node.lineno],
                original=line.rstrip("\n"),
                mutated=mutated_line.rstrip("\n"),
                intent=f"constant: {quoted_before} -> ''",
            )


_OPERATORS = {
    "AOR": _mutate_aor,
    "ROR": _mutate_ror,
    "COR": _mutate_cor,
    "LCR": _mutate_lcr,
    "BoundaryShift": _mutate_boundary,
    "ConstantReplace": _mutate_constant,
}


def generate_mutants(
    file: str,
    span: Optional[List[int]] = None,
    operators: Optional[List[str]] = None,
) -> List[Mutant]:
    src = Path(file).read_text()
    src_lines = src.splitlines(keepends=False)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"[mutate] parse error in {file}: {e}", file=sys.stderr)
        return []
    ops = operators or list(_OPERATORS.keys())
    out: List[Mutant] = []
    counter = 0
    for op_name in ops:
        gen = _OPERATORS.get(op_name)
        if not gen:
            continue
        for m in gen(tree, src_lines, file, span):
            counter += 1
            m.id = f"m_{counter:04d}"
            m.mutant_signature_hash = _hash_signature(m.file, m.span, m.operator, m.mutated)
            out.append(m)
    return out


def main():
    ap = argparse.ArgumentParser(description="AST-based mutation generator (Python).")
    ap.add_argument("--file", required=True)
    ap.add_argument("--span", nargs=2, type=int, default=None, help="start_line end_line (inclusive)")
    ap.add_argument("--operators", nargs="+", default=None, choices=list(_OPERATORS.keys()))
    ap.add_argument("--out", default="-", help="output JSON path or - for stdout")
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
