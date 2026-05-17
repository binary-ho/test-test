"""File-based mutation injection with automatic rollback.

Provides a `MutationInjector` context manager: it writes the mutated source
to disk, yields, and restores the original content on exit (even on
exceptions). The runner.py is expected to wrap each mutant run with this.

CLI usage:

    python inject.py --mutant mutant.json --check        # dry run, show diff
    python inject.py --mutant mutant.json --apply        # write
    python inject.py --mutant mutant.json --rollback     # restore from backup
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Generator


_BACKUP_SUFFIX = ".testvalidity-backup"


def _verify_clean(path: Path) -> None:
    """Make sure the file has no leftover backup before we touch it."""
    bak = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    if bak.exists():
        raise RuntimeError(
            f"[inject] stale backup {bak} exists. "
            "Previous run did not roll back; investigate before continuing."
        )


def apply_mutant(mutant: dict) -> Path:
    """Backup `mutant['file']` and replace the line at mutant['span'][0] with mutant['mutated']."""
    path = Path(mutant["file"])
    _verify_clean(path)
    bak = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    shutil.copy2(path, bak)

    src_lines = path.read_text().splitlines(keepends=True)
    line_idx = mutant["span"][0] - 1
    if line_idx < 0 or line_idx >= len(src_lines):
        rollback(path)
        raise IndexError(f"[inject] span out of range for {path}: {mutant['span']}")

    original_line = src_lines[line_idx].rstrip("\n")
    if original_line.strip() != mutant["original"].strip():
        rollback(path)
        raise RuntimeError(
            f"[inject] file drifted: expected `{mutant['original']}` "
            f"at {path}:{mutant['span'][0]}, found `{original_line}`."
        )

    # Preserve original line ending
    ending = "\n" if src_lines[line_idx].endswith("\n") else ""
    src_lines[line_idx] = mutant["mutated"] + ending
    path.write_text("".join(src_lines))
    return bak


def rollback(path: Path) -> None:
    bak = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
    if bak.exists():
        shutil.copy2(bak, path)
        bak.unlink()


@contextlib.contextmanager
def MutationInjector(mutant: dict) -> Generator[None, None, None]:
    path = Path(mutant["file"])
    apply_mutant(mutant)
    try:
        yield
    finally:
        rollback(path)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Mutation injector with safe rollback.")
    ap.add_argument("--mutant", required=True, help="path to mutant JSON")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="dry run: show diff only")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    mutant = json.loads(Path(args.mutant).read_text())
    path = Path(mutant["file"])

    if args.check:
        print(f"--- {path}:{mutant['span'][0]} (original)\n{mutant['original']}")
        print(f"+++ {path}:{mutant['span'][0]} (mutated)\n{mutant['mutated']}")
        return
    if args.apply:
        bak = apply_mutant(mutant)
        print(f"[inject] applied {mutant['id']} ({mutant['operator']}); backup={bak}", file=sys.stderr)
        return
    if args.rollback:
        rollback(path)
        print(f"[inject] rolled back {path}", file=sys.stderr)
        return


if __name__ == "__main__":
    main()
