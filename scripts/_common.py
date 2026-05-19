"""Shared utilities for the test-validity-evaluator skill package.

This module lives at `<SKILL_ROOT>/scripts/_common.py`. When installed via
symlink (e.g. `~/.claude/skills/test-validity-evaluator`), `Path(__file__)
.resolve()` still points at the real package directory.

Override with the `CHENNAI_SKILL_ROOT` env var when forcing a different
layout (mostly useful for tests).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


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


# --- pause / resume state machine for the top orchestrator -----------------

PAUSE_SENTINEL = "__PAUSE__"
STATE_FILE = "_orchestrator_state.json"
PENDING_FILE = "_pending_llm.json"


def read_state(iter_dir: Path) -> dict[str, Any]:
    p = iter_dir / STATE_FILE
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def write_state(iter_dir: Path, state: dict[str, Any]) -> None:
    """Atomic write via temp file + rename so a crashed resume can't read a half file."""
    iter_dir.mkdir(parents=True, exist_ok=True)
    p = iter_dir / STATE_FILE
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(p)


def emit_pause(iter_dir: Path, *, phase_id: str, prompt: str | list[str],
               context: str, output: str, schema_hint: str,
               note: str = "") -> None:
    """Write `_pending_llm.json` and print a one-line sentinel to stdout.

    `prompt` accepts either a single path (single-prompt LLM stage) or a list
    of paths (multi-prompt batch like the 4 critique analyzers or 5
    adversarial generators).
    """
    iter_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase_id": phase_id,
        "prompt": prompt,
        "context": context,
        "output": output,
        "schema_hint": schema_hint,
        "next_step": (
            f"Fill output JSON per the prompt and context above, then re-invoke "
            f"`orchestrate.py --resume {iter_dir}`."
        ),
        "note": note,
    }
    (iter_dir / PENDING_FILE).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"{PAUSE_SENTINEL} {phase_id} -> see {iter_dir / PENDING_FILE}")


def clear_pause(iter_dir: Path) -> None:
    """Called after the LLM output has been written and a resume is starting."""
    p = iter_dir / PENDING_FILE
    if p.exists():
        p.unlink()


# Canonical state transition labels. Listed in execution order so a resume
# can `index(current) + 1` to pick the next step deterministically.
STATE_LABELS = [
    "init",
    "tier_done",
    "subject_done",
    "mut_init_done",
    "awaiting:semantic_mutants",
    "mut_execute_done",
    "awaiting:diagnoses",
    "mut_finalize_done",
    "adv_init_done",
    "awaiting:critique_findings",
    "adv_generate_done",
    "awaiting:adversarial_cases",
    "adv_execute_done",
    "adv_finalize_done",
    "aggregated",
]
