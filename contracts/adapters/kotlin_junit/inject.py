"""Kotlin mutant injector — stub. No-op for the MVP path (no mutants to inject)."""
from __future__ import annotations

import argparse
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutant", required=True)
    ap.parse_args()
    print(json.dumps({"status": "noop", "detail": "kotlin injector stub"}))


if __name__ == "__main__":
    main()
