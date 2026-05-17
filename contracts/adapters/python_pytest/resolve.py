"""Best-effort subject-under-test resolver for Python tests.

Given a test_id and the test file, returns a TestSubjectMap fragment:
  primary_subjects[]   — imported, called, non-mocked symbols
  mocked_collaborators[] — symbols passed to mock/patch decorators
  confidence           — heuristic in [0,1]

This is a *deterministic* helper; the LLM-using `subject-under-test-locator`
skill builds on top of this with assertion-subject inference.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Optional


_MOCK_DECORATORS = {"patch", "patch.object", "patch.dict", "patch.multiple"}
_MOCK_CALLS = {"Mock", "MagicMock", "AsyncMock"}


def _find_source_file(test_file: Path, module_name: str) -> Optional[str]:
    """Best-effort lookup: walk upward from test_file, search common src layouts."""
    head = module_name.split(".")[0]
    candidates_rel = [
        f"{module_name.replace('.', '/')}.py",
        f"src/{module_name.replace('.', '/')}.py",
        f"{head}.py",
        f"src/{head}.py",
    ]
    cur = test_file.parent
    for _ in range(6):
        for rel in candidates_rel:
            cand = cur / rel
            if cand.is_file():
                return str(cand)
        cur = cur.parent
        if cur == cur.parent:
            break
    return None


def _find_symbol_span(file_path: str, symbol: str) -> list[int]:
    try:
        src = Path(file_path).read_text()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return [0, 0]
    target = symbol.split(".")[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == target:
            return [node.lineno, getattr(node, "end_lineno", node.lineno)]
    return [0, 0]


def resolve(test_file: str, test_id: str) -> dict:
    src = Path(test_file).read_text()
    tree = ast.parse(src)

    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name] = alias.name

    test_name = test_id.split("::")[-1]
    test_func: Optional[ast.FunctionDef] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name:
            test_func = node
            break
    if test_func is None:
        return {
            "test_id": test_id,
            "primary_subjects": [],
            "secondary_subjects": [],
            "mocked_collaborators": [],
            "confidence": 0.0,
        }

    primary: list[dict] = []
    mocked: list[dict] = []

    for node in ast.walk(test_func):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = f"{func.value.id}.{func.attr}"
            if not name:
                continue
            if name in _MOCK_CALLS or name in _MOCK_DECORATORS:
                continue
            module = imports.get(name.split(".")[0])
            if module:
                source_file = _find_source_file(Path(test_file), module) \
                              or (module.replace(".", "/") + ".py")
                span = _find_symbol_span(source_file, name) if Path(source_file).is_file() else \
                       [getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)]
                primary.append({
                    "file": source_file,
                    "symbol": name,
                    "kind": "function",
                    "span": span,
                    "evidence": f"{test_file}:{node.lineno}",
                })

    for deco in test_func.decorator_list:
        if isinstance(deco, ast.Call):
            fname = ""
            if isinstance(deco.func, ast.Name):
                fname = deco.func.id
            elif isinstance(deco.func, ast.Attribute):
                fname = deco.func.attr
            if fname in {"patch"} and deco.args and isinstance(deco.args[0], ast.Constant):
                target = deco.args[0].value
                mocked.append({"file": target.replace(".", "/"), "symbol": target.split(".")[-1]})

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
    ap.add_argument("--test-id",   required=True)
    args = ap.parse_args()
    print(json.dumps(resolve(args.test_file, args.test_id), indent=2))


if __name__ == "__main__":
    main()
