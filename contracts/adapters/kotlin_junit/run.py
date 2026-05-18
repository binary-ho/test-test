"""Kotlin test runner — stub.

A real runner would invoke `gradle test --tests <test_id>`. The MVP path
does not exercise this because the mutate.py stub produces zero mutants;
returning a benign "skipped" status here keeps the mutation orchestrator
from interpreting the call as a fatal error if it is ever invoked.
"""
from __future__ import annotations

import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutant", required=True)
    ap.add_argument("--tests", nargs="*", default=[])
    ap.add_argument("--timeout", type=int, default=30)
    ap.parse_args()
    print(json.dumps({
        "status": "skipped",
        "detail": "kotlin runner stub — gradle invocation not implemented",
        "killing_tests": [],
        "wall_clock_seconds": 0,
    }))


if __name__ == "__main__":
    main()
