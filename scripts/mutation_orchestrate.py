"""Mutation testing meso skill orchestrator.

Deterministic stages run inline (syntactic mutation, equivalence rule-check,
injection, pytest run, scoring, ledger). LLM stages (semantic mutation,
LLM-based equivalence judgment, survivor diagnosis) are externalised: this
script writes a context file and exits, the caller (Claude Skill) reads the
prompt under ../prompts/, fills in the response, and re-invokes this script
with --resume.

Usage:

    # 1) Initial run: produces syntactic mutants + writes prompt context.
    python3 orchestrate.py --subject-map subject_map.json \\
        --adapter contracts/adapters/python.pytest.yaml \\
        --policy policy.json --session-id evs_… --out-dir out/ --phase init

    # 2) Caller fills in semantic mutants -> out/semantic_mutants.json
    # 3) Re-invoke to consume and proceed:
    python3 orchestrate.py --out-dir out/ --phase execute

    # 4) Caller fills in survivor diagnoses -> out/diagnoses.json
    # 5) Re-invoke to finalize:
    python3 orchestrate.py --out-dir out/ --phase finalize
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SKILL_ROOT, ADAPTERS_DIR as ADAPTER_DIR, load_yaml  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_ROOT


# ---------- ledger ----------

@dataclass
class LedgerRecord:
    mutant_id: str
    operator: str
    file: str
    span: list[int]
    before_code: str = ""
    after_code: str = ""
    intent: str = ""
    equivalence_status: str = "unknown"
    execution_status: Optional[str] = None
    killing_tests: list[str] = field(default_factory=list)
    wall_clock_seconds: float = 0.0
    cache_status: str = "miss"
    diagnosis: Optional[dict] = None
    suppressed: bool = False


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_ledger(adapter_key: str) -> dict:
    return {
        "generated_at": _now(),
        "tool_versions": {"adapter": f"{adapter_key}@0.1.0", "skill": "0.1.0"},
        "adapter_key": adapter_key,
        "records": [],
    }


def _ledger_path(out_dir: Path) -> Path:
    return out_dir / "ledger.json"


def _load_ledger(out_dir: Path) -> dict:
    p = _ledger_path(out_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_ledger(out_dir: Path, ledger: dict) -> None:
    _ledger_path(out_dir).write_text(json.dumps(ledger, indent=2, ensure_ascii=False))


# ---------- session cache ----------

def _cache_path(out_dir: Path) -> Path:
    return out_dir / "session_cache.json"


def _cache_lookup(out_dir: Path, signature: str) -> Optional[dict]:
    p = _cache_path(out_dir)
    if not p.exists():
        return None
    cache = json.loads(p.read_text())
    return cache.get(signature)


def _cache_store(out_dir: Path, signature: str, result: dict) -> None:
    p = _cache_path(out_dir)
    cache = json.loads(p.read_text()) if p.exists() else {}
    cache[signature] = result
    p.write_text(json.dumps(cache, indent=2))


# ---------- equivalence (rule-based subset) ----------

def _force_equivalent_signatures(eq_yml: Path) -> set[str]:
    if not eq_yml.exists():
        return set()
    out: set[str] = set()
    for line in eq_yml.read_text().splitlines():
        line = line.strip()
        if line.startswith("- mutant_signature_hash:"):
            out.add(line.split(":", 1)[1].strip())
        elif line.startswith("mutant_signature_hash:"):
            out.add(line.split(":", 1)[1].strip())
    return out


# ---------- adapter loading (delegated to shared loader) ----------

def _load_yaml(p: Path) -> dict:
    return load_yaml(p)


# ---------- phase 1: init (syntactic generation + semantic prompt) ----------

def phase_init(args) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    subject_map = json.loads(Path(args.subject_map).read_text())
    adapter = _load_yaml(Path(args.adapter))
    policy = json.loads(Path(args.policy).read_text())

    adapter_key = adapter["key"]
    ledger = _empty_ledger(adapter_key)

    # Persist run config (absolute paths so subsequent phases are cwd-independent).
    (out_dir / "_run.json").write_text(json.dumps({
        "subject_map": str(Path(args.subject_map).resolve()),
        "adapter":     str(Path(args.adapter).resolve()),
        "policy":      str(Path(args.policy).resolve()),
        "session_id":  args.session_id,
    }, indent=2))

    syntactic_ops = policy.get("operators", {}).get(
        "syntactic_enabled",
        ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
    )
    capability_skips = 0
    notices: list[str] = []

    # Dedup mutation sources by (file, span) and gather test_ids per unique location.
    file_span_tests: dict[tuple, set[str]] = {}
    for entry in subject_map if isinstance(subject_map, list) else [subject_map]:
        test_id = entry.get("test_id", "")
        for subj in entry.get("primary_subjects", []):
            key = (subj["file"], tuple(subj["span"]))
            file_span_tests.setdefault(key, set()).add(test_id)

    all_mutants: list[dict] = []
    mutate_path = REPO_ROOT / adapter["implementations"]["syntactic_operator"]
    mutant_counter = 0
    for (file, span), test_ids in file_span_tests.items():
        cmd = [
            sys.executable, str(mutate_path),
            "--file", file,
            "--span", str(span[0]), str(span[1]),
            "--operators", *syntactic_ops,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                mutants = json.loads(proc.stdout)
                for m in mutants:
                    mutant_counter += 1
                    # Rewrite ids to be globally unique across the (file,span) iterations.
                    m["id"] = f"m_{mutant_counter:04d}"
                    m["test_ids"] = sorted(test_ids)
                    all_mutants.append(m)
            else:
                notices.append(f"syntactic generation failed for {file}: {proc.stderr[:120]}")
        except subprocess.TimeoutExpired:
            notices.append(f"syntactic timeout on {file}")

    # Persist candidates + record in ledger.
    for m in all_mutants:
        ledger["records"].append(asdict(LedgerRecord(
            mutant_id=m["id"], operator=m["operator"],
            file=m["file"], span=m["span"],
            before_code=m["original"], after_code=m["mutated"],
            intent=m["intent"],
        )))

    (out_dir / "syntactic_mutants.json").write_text(json.dumps(all_mutants, indent=2, ensure_ascii=False))

    # Write prompt context for semantic operator (consumed by the calling LLM).
    semantic_ctx = []
    if policy.get("operators", {}).get("semantic_enabled", True):
        for entry in subject_map if isinstance(subject_map, list) else [subject_map]:
            for subj in entry.get("primary_subjects", []):
                # Include the whole function body.
                try:
                    src = Path(subj["file"]).read_text().splitlines(keepends=True)
                    body = "".join(src[subj["span"][0]-1:subj["span"][1]])
                except OSError:
                    body = ""
                semantic_ctx.append({
                    "test_id": entry.get("test_id"),
                    "subject": subj,
                    "function_body": body,
                })

    (out_dir / "semantic_context.json").write_text(
        json.dumps(semantic_ctx, indent=2, ensure_ascii=False))

    _save_ledger(out_dir, ledger)
    print(json.dumps({
        "phase": "init",
        "syntactic_mutants": len(all_mutants),
        "semantic_subjects_pending": len(semantic_ctx),
        "next_action": (
            f"LLM fills semantic mutants per prompts/semantic-operator.md, "
            f"writes to {out_dir}/semantic_mutants.json, then re-invoke with --phase execute"
        ),
        "notices": notices,
    }, indent=2, ensure_ascii=False))


# ---------- phase 2: execute (equivalence filter + run + ledger) ----------

def phase_execute(args) -> None:
    out_dir = Path(args.out_dir)
    run_cfg = json.loads((out_dir / "_run.json").read_text())
    adapter = _load_yaml(Path(run_cfg["adapter"]))
    policy = json.loads(Path(run_cfg["policy"]).read_text())
    ledger = _load_ledger(out_dir)

    # Merge syntactic + semantic.
    syntactic = json.loads((out_dir / "syntactic_mutants.json").read_text())
    semantic_path = out_dir / "semantic_mutants.json"
    semantic = json.loads(semantic_path.read_text()) if semantic_path.exists() else []
    for m in semantic:
        ledger["records"].append(asdict(LedgerRecord(
            mutant_id=m["id"], operator="semantic",
            file=m["file"], span=m["span"],
            before_code=m["original"], after_code=m["mutated"],
            intent=m.get("intent", ""),
        )))
    all_mutants = syntactic + semantic

    # Equivalence filter (rule-only here; LLM judge can be wired in by overriding).
    eq_yml_raw = policy.get("equivalence", {}).get(
        "equivalent_mutants_yml",
        policy.get("session", {}).get("equivalent_mutants_yml", ""),
    )
    forced_eq = _force_equivalent_signatures(Path(eq_yml_raw)) if eq_yml_raw else set()
    for rec in ledger["records"]:
        sig = rec.get("mutant_signature_hash", "")
        if sig in forced_eq:
            rec["equivalence_status"] = "force_equivalent"
            rec["execution_status"] = "filtered_equivalent"
            rec["suppressed"] = True

    # Budget.
    budget = policy.get("budget", {})
    max_mutants = int(budget.get("max_mutants", 200))
    runnable = [m for m in all_mutants if not any(
        r["mutant_id"] == m["id"] and r["execution_status"] == "filtered_equivalent"
        for r in ledger["records"]
    )]
    if len(runnable) > max_mutants:
        dropped = runnable[max_mutants:]
        runnable = runnable[:max_mutants]
        for d in dropped:
            for r in ledger["records"]:
                if r["mutant_id"] == d["id"]:
                    r["execution_status"] = "budget_dropped"

    # Inject + run each.
    run_path = REPO_ROOT / adapter["implementations"]["runner"]
    consecutive_fatal = 0
    degradations: list[dict] = []

    for m in runnable:
        sig = m.get("mutant_signature_hash", "")
        cached = _cache_lookup(out_dir, sig) if sig else None
        if cached is not None:
            result = dict(cached, cache_status="hit")
        else:
            tmp = out_dir / f"_mutant_{m['id']}.json"
            tmp.write_text(json.dumps(m))
            tests = m.get("test_ids") or ([m.get("test_id")] if m.get("test_id") else [])
            cmd = [sys.executable, str(run_path), "--mutant", str(tmp), "--timeout", "30"]
            if tests:
                cmd.extend(["--tests"] + [t for t in tests if t])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            tmp.unlink(missing_ok=True)
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError:
                result = {"status": "error", "detail": proc.stderr[:240]}
            if sig and result.get("status") not in {"error", "timeout"}:
                _cache_store(out_dir, sig, result)

        if result.get("status") in {"error"}:
            consecutive_fatal += 1
            degradations.append({
                "stage": "mutation.runner", "error_kind": "exception",
                "detail": result.get("detail", "")[:240], "occurred_at": _now(),
            })
            if consecutive_fatal >= 3:
                degradations.append({"stage": "mutation.runner", "error_kind": "abort",
                                     "detail": "3 consecutive fatal runs", "occurred_at": _now()})
                break
        else:
            consecutive_fatal = 0

        for r in ledger["records"]:
            if r["mutant_id"] == m["id"]:
                r["execution_status"] = result.get("status")
                r["killing_tests"] = result.get("killing_tests", [])
                r["wall_clock_seconds"] = result.get("wall_clock_seconds", 0)
                r["cache_status"] = result.get("cache_status", "miss")
                break

    _save_ledger(out_dir, ledger)
    (out_dir / "degradations.json").write_text(json.dumps(degradations, indent=2))

    survivors = [r for r in ledger["records"] if r["execution_status"] == "survived"]
    (out_dir / "survivors.json").write_text(json.dumps(survivors, indent=2, ensure_ascii=False))

    print(json.dumps({
        "phase": "execute",
        "total_records": len(ledger["records"]),
        "survivors": len(survivors),
        "killed": sum(1 for r in ledger["records"] if r["execution_status"] == "killed"),
        "filtered_equivalent": sum(1 for r in ledger["records"] if r["execution_status"] == "filtered_equivalent"),
        "budget_dropped": sum(1 for r in ledger["records"] if r["execution_status"] == "budget_dropped"),
        "next_action": (
            f"LLM diagnoses each survivor per prompts/survivor-diagnose.md, "
            f"writes to {out_dir}/diagnoses.json (list of TestFinding), "
            f"then re-invoke with --phase finalize"
        ),
        "degradations": len(degradations),
    }, indent=2, ensure_ascii=False))


# ---------- phase 3: finalize (scoring + report) ----------

def phase_finalize(args) -> None:
    out_dir = Path(args.out_dir)
    run_cfg = json.loads((out_dir / "_run.json").read_text())
    adapter = _load_yaml(Path(run_cfg["adapter"]))
    policy = json.loads(Path(run_cfg["policy"]).read_text())
    ledger = _load_ledger(out_dir)
    degradations = json.loads((out_dir / "degradations.json").read_text()) if (out_dir / "degradations.json").exists() else []
    diagnoses_path = out_dir / "diagnoses.json"
    findings = json.loads(diagnoses_path.read_text()) if diagnoses_path.exists() else []

    # Attach diagnosis to ledger records.
    by_mutant: dict[str, dict] = {}
    for f in findings:
        for mid in f.get("caused_by_mutants", []):
            by_mutant[mid] = f
    for r in ledger["records"]:
        if r["mutant_id"] in by_mutant:
            f = by_mutant[r["mutant_id"]]
            r["diagnosis"] = {
                "finding_id": f["id"], "summary": f.get("category", ""),
                "evidence":  f.get("evidence", {}).get("test_snippet", ""),
            }

    # Score.
    total = len(ledger["records"])
    killed = sum(1 for r in ledger["records"] if r["execution_status"] == "killed")
    survived = sum(1 for r in ledger["records"] if r["execution_status"] == "survived")
    timeout = sum(1 for r in ledger["records"] if r["execution_status"] == "timeout")
    error   = sum(1 for r in ledger["records"] if r["execution_status"] == "error")
    eq      = sum(1 for r in ledger["records"] if r["execution_status"] == "filtered_equivalent")
    dropped = sum(1 for r in ledger["records"] if r["execution_status"] == "budget_dropped")

    score_denominator = max(total - eq - dropped, 1)
    score = killed / score_denominator

    by_operator: dict[str, dict] = {}
    for r in ledger["records"]:
        op = r["operator"]
        d = by_operator.setdefault(op, {"killed": 0, "survived": 0, "other": 0})
        if r["execution_status"] == "killed":
            d["killed"] += 1
        elif r["execution_status"] == "survived":
            d["survived"] += 1
        else:
            d["other"] += 1

    hotspots: dict[str, int] = {}
    for r in ledger["records"]:
        if r["execution_status"] == "survived":
            hotspots[r["file"]] = hotspots.get(r["file"], 0) + 1
    hotspot_list = sorted(
        [{"file": k, "survived": v} for k, v in hotspots.items()],
        key=lambda x: x["survived"], reverse=True
    )[:10]

    adapter_summary = {
        "adapter_key": adapter["key"],
        "adapter_version": adapter.get("version", "0.0.0"),
        "capabilities_used": list(adapter["capabilities"].get("syntactic_operators", [])),
        "capabilities_missing": [],
        "skipped_due_to_capability": 0,
    }

    _save_ledger(out_dir, ledger)
    report = {
        "metrics": {
            "mutation_score": round(score, 4),
            "killed": killed, "survived": survived, "timeout": timeout,
            "error": error, "filtered_equivalent": eq, "budget_dropped": dropped,
            "by_operator": by_operator, "hotspots": hotspot_list,
        },
        "findings": findings,
        "mutation_ledger_ref": "ledger.json",
        "degradations": degradations,
        "adapter_summary": adapter_summary,
    }
    (out_dir / "mutation_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({
        "phase": "finalize",
        "mutation_score": round(score, 4),
        "killed": killed, "survived": survived, "filtered_equivalent": eq,
        "findings": len(findings),
        "report": str(out_dir / "mutation_report.json"),
        "ledger": str(out_dir / "ledger.json"),
    }, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["init", "execute", "finalize"], required=True)
    ap.add_argument("--subject-map")
    ap.add_argument("--adapter")
    ap.add_argument("--policy")
    ap.add_argument("--session-id")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    if args.phase == "init":
        for f in ("subject_map", "adapter", "policy"):
            if not getattr(args, f.replace("-", "_")):
                ap.error(f"--{f.replace('_','-')} required for phase init")
        phase_init(args)
    elif args.phase == "execute":
        phase_execute(args)
    else:
        phase_finalize(args)


if __name__ == "__main__":
    main()
