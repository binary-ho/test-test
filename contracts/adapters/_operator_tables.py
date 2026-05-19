"""Shared mutation primitives for language adapters.

Holds the pieces that are identical for every C-family / Python-family
language: the `Mutant` wire format, the operator semantic tables
(AOR / ROR), and the regex-driven emitters that walk a pre-computed
character mask.

Each adapter's `mutate.py` is responsible for building the language-specific
character mask (which positions are inside strings/comments) and then asks
this module to emit `Mutant`s. COR/LCR spellings are language-specific and
live in the adapter file.

Adapters bring `_operator_tables` onto `sys.path` by inserting their parent
directory (`contracts/adapters/`) before importing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple


# --- Wire format ------------------------------------------------------------

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


def hash_signature(file: str, span: List[int], operator: str, mutated: str) -> str:
    h = hashlib.sha256()
    h.update(f"{file}|{span[0]}-{span[1]}|{operator}|{mutated}".encode())
    return f"sha256:{h.hexdigest()[:16]}"


# --- Operator semantic tables ----------------------------------------------

AOR: dict[str, str] = {
    "+": "-",
    "-": "+",
    "*": "/",
    "/": "*",
    "%": "*",
}

ROR: dict[str, list[str]] = {
    "<":  ["<=", "==", ">="],
    "<=": ["<",  "==", ">"],
    ">":  [">=", "==", "<="],
    ">=": [">",  "==", "<"],
    "==": ["!="],
    "!=": ["=="],
}


# --- Line-position helpers --------------------------------------------------

def line_starts(src: str) -> List[int]:
    out = [0]
    for idx, ch in enumerate(src):
        if ch == "\n":
            out.append(idx + 1)
    return out


def line_of(starts: List[int], pos: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def line_text(src: str, starts: List[int], line_no: int) -> Tuple[int, str]:
    start = starts[line_no - 1]
    end = starts[line_no] - 1 if line_no < len(starts) else len(src)
    if end > 0 and src[end - 1:end] == "\n":
        end -= 1
    return start, src[start:end]


def within_span(line_no: int, span: Optional[List[int]]) -> bool:
    return span is None or span[0] <= line_no <= span[1]


def line_is_import(text: str) -> bool:
    s = text.lstrip()
    return s.startswith("import ") or s.startswith("package ") or s.startswith("from ")


# --- Operator emitters ------------------------------------------------------

_AOR_RE = re.compile(r"(?<![+\-*/%=!<>])([+\-*/%])(?![=+\-*/%])")
_ROR_2CHAR = re.compile(r"(<=|>=|==|!=)(?!=)")
# `<` / `>` for relational, but skip generics: `Map<String, Int>`, `List<T>`,
# `): PageResult<Foo>`. Heuristic: `<` followed by whitespace then a capital
# letter is almost always a generic type parameter. Same for `>` preceded by
# something that looks like end-of-generic (capital identifier or `>`).
_ROR_1CHAR = re.compile(
    r"(?<=[\w)\]])\s*(<|>)\s*(?=[\w(\[])"
)
_GENERIC_LT_AFTER = re.compile(r"^\s*[A-Z]")
_GENERIC_GT_BEFORE = re.compile(r"[A-Z\w]>?\s*$")
_INT_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def emit_aor(src: str, mask: List[bool], starts: List[int],
             file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    for m in _AOR_RE.finditer(src):
        pos = m.start()
        if mask[pos]:
            continue
        op = m.group(1)
        if op not in AOR:
            continue
        j = pos - 1
        while j >= 0 and src[j] in " \t":
            j -= 1
        if j < 0 or src[j] in "({[,=;:?&|<>!+-*/%":
            continue
        # skip lambda arrows: Kotlin `->`, TS `=>` (the `=` was already handled
        # by the lookbehind on `=`; this catches the `-` of `->`).
        if op == "-" and pos + 1 < len(src) and src[pos + 1] == ">":
            continue
        line_no = line_of(starts, pos)
        start, text = line_text(src, starts, line_no)
        if line_is_import(text):
            continue
        if not within_span(line_no, span):
            continue
        new_op = AOR[op]
        col = pos - start
        mutated = text[:col] + new_op + text[col + len(op):]
        if mutated == text:
            continue
        yield Mutant(id="", operator="AOR", file=file,
                     span=[line_no, line_no],
                     original=text, mutated=mutated,
                     intent=f"arithmetic: {op} -> {new_op}")


def emit_ror(src: str, mask: List[bool], starts: List[int],
             file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    seen: set[Tuple[int, str]] = set()
    for regex in (_ROR_2CHAR, _ROR_1CHAR):
        for m in regex.finditer(src):
            op = m.group(1)
            pos = m.start(1)
            if mask[pos]:
                continue
            if op == ">" and pos > 0 and src[pos - 1] == "-":
                continue
            # Generic guard for single-char `<` / `>`: `<Foo>` / `<T>` look
            # like comparisons under the bare regex. Skip when the operator
            # sits adjacent to an uppercase identifier.
            if op == "<" and _GENERIC_LT_AFTER.match(src[pos + 1:pos + 6]):
                continue
            if op == ">":
                # Look backwards skipping whitespace.
                bj = pos - 1
                while bj >= 0 and src[bj] in " \t":
                    bj -= 1
                if bj >= 0 and (src[bj].isupper() or src[bj] == ">"):
                    continue
            line_no = line_of(starts, pos)
            if not within_span(line_no, span):
                continue
            if (pos, op) in seen:
                continue
            seen.add((pos, op))
            start, text = line_text(src, starts, line_no)
            col = pos - start
            for new_op in ROR.get(op, []):
                mutated = text[:col] + new_op + text[col + len(op):]
                if mutated == text:
                    continue
                yield Mutant(id="", operator="ROR", file=file,
                             span=[line_no, line_no],
                             original=text, mutated=mutated,
                             intent=f"relational: {op} -> {new_op}")


def emit_cor(src: str, mask: List[bool], starts: List[int],
             cor_table: dict[str, str],
             file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    if not cor_table:
        return
    keys = sorted(cor_table.keys(), key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    for m in pat.finditer(src):
        pos = m.start()
        if mask[pos]:
            continue
        op = m.group(0)
        new_op = cor_table.get(op)
        if not new_op:
            continue
        line_no = line_of(starts, pos)
        if not within_span(line_no, span):
            continue
        start, text = line_text(src, starts, line_no)
        col = pos - start
        mutated = text[:col] + new_op + text[col + len(op):]
        if mutated == text:
            continue
        yield Mutant(id="", operator="COR", file=file,
                     span=[line_no, line_no],
                     original=text, mutated=mutated,
                     intent=f"conditional: {op} -> {new_op}")


def emit_lcr_paren_bang(src: str, mask: List[bool], starts: List[int],
                        file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    """LCR for C-family `if (cond)` / `if (!cond)`."""
    pat = re.compile(r"\bif\s*\(\s*(!)?")
    for m in pat.finditer(src):
        pos = m.start()
        if mask[pos]:
            continue
        has_neg = m.group(1) is not None
        line_no = line_of(starts, pos)
        if not within_span(line_no, span):
            continue
        start, text = line_text(src, starts, line_no)
        if has_neg:
            mutated = re.sub(r"if\s*\(\s*!", "if (", text, count=1)
            intent = "logical: remove negation"
        else:
            mutated = re.sub(r"if\s*\(", "if (!", text, count=1)
            intent = "logical: insert negation"
        if mutated == text:
            continue
        yield Mutant(id="", operator="LCR", file=file,
                     span=[line_no, line_no],
                     original=text, mutated=mutated, intent=intent)


def emit_boundary(src: str, mask: List[bool], starts: List[int],
                  file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    for m in _INT_RE.finditer(src):
        pos = m.start()
        if mask[pos]:
            continue
        try:
            val = int(m.group(1))
        except ValueError:
            continue
        line_no = line_of(starts, pos)
        if not within_span(line_no, span):
            continue
        start, text = line_text(src, starts, line_no)
        col = pos - start
        for shift in (+1, -1):
            new_val = val + shift
            mutated = text[:col] + str(new_val) + text[col + len(m.group(1)):]
            if mutated == text:
                continue
            yield Mutant(id="", operator="BoundaryShift", file=file,
                         span=[line_no, line_no],
                         original=text, mutated=mutated,
                         intent=f"boundary: {val} -> {new_val}")


def emit_constant(src: str, mask: List[bool], starts: List[int],
                  string_regions: List[Tuple[int, int]],
                  file: str, span: Optional[List[int]]) -> Iterable[Mutant]:
    # Integer literals.
    for m in _INT_RE.finditer(src):
        pos = m.start()
        if mask[pos]:
            continue
        try:
            val = int(m.group(1))
        except ValueError:
            continue
        target = 1 if val == 0 else 0
        if target == val:
            continue
        line_no = line_of(starts, pos)
        if not within_span(line_no, span):
            continue
        start, text = line_text(src, starts, line_no)
        col = pos - start
        mutated = text[:col] + str(target) + text[col + len(m.group(1)):]
        if mutated == text:
            continue
        yield Mutant(id="", operator="ConstantReplace", file=file,
                     span=[line_no, line_no],
                     original=text, mutated=mutated,
                     intent=f"constant: {val} -> {target}")

    # Single-line string literals (start_inclusive, end_inclusive both quotes).
    for (s, e) in string_regions:
        if e - s <= 1:
            continue
        line_no = line_of(starts, s)
        end_line = line_of(starts, e)
        if line_no != end_line:
            continue
        if not within_span(line_no, span):
            continue
        line_start, text = line_text(src, starts, line_no)
        col_s = s - line_start
        col_e = e - line_start + 1
        before = text[col_s:col_e]
        # Preserve the quote style (single or double or backtick).
        quote = text[col_s] if col_s < len(text) else '"'
        mutated = text[:col_s] + quote + quote + text[col_e:]
        if mutated == text:
            continue
        yield Mutant(id="", operator="ConstantReplace", file=file,
                     span=[line_no, line_no],
                     original=text, mutated=mutated,
                     intent=f"constant: {before} -> {quote}{quote}")


# Bundle emitters by name. CFamily LCR uses `if (...)`; languages with
# different syntax must register their own emitter.
def standard_emitters(cor_table: dict[str, str]) -> dict[str, Callable]:
    return {
        "AOR": emit_aor,
        "ROR": emit_ror,
        "COR": lambda src, mask, starts, file, span: emit_cor(src, mask, starts, cor_table, file, span),
        "LCR": emit_lcr_paren_bang,
        "BoundaryShift": emit_boundary,
        "ConstantReplace": None,  # special — caller passes string_regions
    }
