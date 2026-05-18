"""Kotlin syntactic mutator — minimal stub.

A real implementation would parse Kotlin (via the compiler API or a static
parser) and produce safe AOR/ROR/etc. mutants. This stub returns an empty
list so the mutation orchestrator records "no syntactic mutants" without
crashing, letting tier classification + subject location still produce a
useful ValidityReport on Kotlin projects.
"""
from __future__ import annotations

import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--span", nargs=2, type=int, required=True)
    ap.add_argument("--operators", nargs="*", default=[])
    ap.parse_args()
    print(json.dumps([]))


if __name__ == "__main__":
    main()
