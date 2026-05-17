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
                primary.append({
                    "file": module.replace(".", "/") + ".py",
                    "symbol": name,
                    "kind": "function",
                    "span": [getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)],
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
