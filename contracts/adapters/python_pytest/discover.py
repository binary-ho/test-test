"""Enumerate pytest tests inside a project root.

Strategy: shell out to `pytest --collect-only -q`. Fall back to a filesystem
walk if pytest is not installed. Returns a list of test_id strings.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def discover_tests(root: str = ".") -> list[str]:
    root_p = Path(root).resolve()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(root_p)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode == 0 or "test" in proc.stdout:
            ids = [
                line.strip()
                for line in proc.stdout.splitlines()
                if "::" in line and not line.startswith("=")
            ]
            if ids:
                return ids
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: filesystem walk
    test_id_re = re.compile(r"^def\s+(test_\w+)\s*\(")
    out: list[str] = []
    for p in root_p.rglob("test_*.py"):
        rel = p.relative_to(root_p)
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            m = test_id_re.match(line)
            if m:
                out.append(f"{rel}::{m.group(1)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    ids = discover_tests(args.root)
    print(json.dumps(ids, indent=2))


if __name__ == "__main__":
    main()
