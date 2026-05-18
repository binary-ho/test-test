"""Shared utilities for the test-validity-evaluator skill package.

This module lives at `<SKILL_ROOT>/scripts/_common.py`. When installed via
symlink (e.g. `~/.claude/skills/test-validity-evaluator`), `Path(__file__)
.resolve()` still points at the real package directory.

Override with the `CHENNAI_SKILL_ROOT` env var when forcing a different
layout (mostly useful for tests).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _detect_skill_root() -> Path:
    override = os.environ.get("CHENNAI_SKILL_ROOT")
    if override:
        return Path(override).resolve()
    # scripts/_common.py → parents[0]=scripts, parents[1]=<SKILL_ROOT>.
    return Path(__file__).resolve().parents[1]


SKILL_ROOT: Path = _detect_skill_root()
SCRIPTS_DIR: Path = SKILL_ROOT / "scripts"
CONTRACTS_DIR: Path = SKILL_ROOT / "contracts"
ADAPTERS_DIR: Path = CONTRACTS_DIR / "adapters"
PROMPTS_DIR: Path = SKILL_ROOT / "prompts"


def load_yaml(path: Path) -> dict:
    """Load an adapter manifest or any small YAML file.

    Prefers PyYAML; falls back to the inline minimal parser in classify.py.
    The fallback handles scalars, nested maps, and '- value' lists — see
    classify._parse_minimal_yaml.
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text())
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from classify import _parse_minimal_yaml  # type: ignore
        return _parse_minimal_yaml(path.read_text())
