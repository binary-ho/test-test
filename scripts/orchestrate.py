"""Top-level orchestrator for the test-validity-evaluator skill.

Drives one iteration of the full pipeline as a state machine:

    init  tier_done  subject_done  awaiting:semantic_mutants
       awaiting:diagnoses  mut_finalize_done
       awaiting:critique_findings  awaiting:adversarial_cases
       adv_finalize_done  aggregated

At every `awaiting:*` boundary the orchestrator persists the current state
to `<iter_dir>/_orchestrator_state.json`, writes a `_pending_llm.json`
describing the prompt / context / expected output JSON path, and exits
with the `__PAUSE__` sentinel on stdout.

Callers (Claude Code) read `_pending_llm.json`, do the LLM work, write
the expected JSON, and re-invoke:

    orchestrate.py --resume <iter_dir>

After four LLM rounds (semantic mutants  diagnoses  critique findings
 adversarial cases) the pipeline aggregates, renders the HTML report,
and updates `session.json`.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    SKILL_ROOT, SCRIPTS_DIR, PROMPTS_DIR, load_yaml,
    STATE_LABELS, read_state, write_state, emit_pause, clear_pause,
    PAUSE_SENTINEL,
)


REPO_ROOT = SKILL_ROOT


# --- small utilities -------------------------------------------------------

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _load_adapter(adapter_path: Path) -> dict:
    return load_yaml(adapter_path)


def _session_root(out_root: Path, session_id: str) -> Path:
    return out_root / session_id


def _load_or_init_session(out_root: Path, session_id: str, policy: dict) -> dict:
    p = _session_root(out_root, session_id) / "session.json"
    if p.exists():
        return json.loads(p.read_text())
    return {
        "session_id": session_id,
        "started_at": _now(),
        "policy_snapshot": policy,
        "iterations": [],
        "status": "in_progress",
        "terminal_reason": None,
    }


def _save_session(out_root: Path, session: dict) -> None:
    p = _session_root(out_root, session["session_id"]) / "session.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session, indent=2, ensure_ascii=False))


def _delta_vs_previous(prev_findings: list[str], cur_findings: list[str]) -> dict:
    prev_set, cur_set = set(prev_findings), set(cur_findings)
    return {
        "new_findings":       sorted(cur_set - prev_set),
        "resolved_findings":  sorted(prev_set - cur_set),
        "recurring_findings": sorted(prev_set & cur_set),
        "score_delta": 0.0,
    }


def _check_termination(session: dict, policy: dict, score: float,
                       major_plus: int, suppressed_only: bool) -> Optional[str]:
    s = policy["session"]
    target = s.get("target_mutation_score", 0.85)
    max_iter = s.get("max_iterations", 5)

    if score >= target and major_plus == 0:
        return "target_reached"
    if suppressed_only:
        return "wontfix_only"
    if len(session["iterations"]) >= max_iter:
        return "max_iterations"

    if len(session["iterations"]) >= 2:
        last = session["iterations"][-1]
        score_delta_recent = last["delta_vs_previous"]["score_delta"]
        if score_delta_recent <= 0.005 and last["delta_vs_previous"]["new_findings"] == []:
            return "plateau"

    if len(session["iterations"]) >= 1:
        last = session["iterations"][-1]
        if last["delta_vs_previous"]["recurring_findings"]:
            recur_count = sum(
                1 for it in session["iterations"]
                if it["delta_vs_previous"]["recurring_findings"]
            )
            if recur_count >= 2:
                return "recurrence_blocked"

    budget = s.get("budget", {})
    total_seconds = sum(it.get("cumulative_seconds", 0) for it in session["iterations"])
    total_tokens  = sum(it.get("cumulative_tokens", 0) for it in session["iterations"])
    if total_seconds > budget.get("max_seconds", 1800):
        return "budget_exhausted"
    if total_tokens > budget.get("max_tokens", 600000):
        return "budget_exhausted"

    return None


def _aggregate(mutation_report: dict, adversarial_report: dict) -> list[dict]:
    findings: dict[str, dict] = {}
    for f in (mutation_report.get("findings", []) + adversarial_report.get("findings", [])):
        fid = f.get("id")
        if not fid:
            continue
        if fid not in findings:
            findings[fid] = f
        else:
            ex = findings[fid]
            ex.setdefault("caused_by_mutants", []).extend(f.get("caused_by_mutants", []))
            ex.setdefault("caused_by_cases", []).extend(f.get("caused_by_cases", []))
    return list(findings.values())


def _render_html(report: dict, out_html: Path) -> None:
    findings_html = "\n".join(
        f'<tr><td>{f.get("severity","info")}</td><td>{f.get("category","?")}</td>'
        f'<td>{f.get("test_id","")}</td><td><pre>{f.get("suggested_fix","")[:240]}</pre></td></tr>'
        for f in report.get("findings", [])
    )
    sev = report.get("summary", {}).get("finding_counts_by_severity", {})
    score = report.get("summary", {}).get("mutation_score", 0)
    out_html.write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Validity Report {report.get('session_id','')}</title>
<style>
  body{{font:14px -apple-system,BlinkMacSystemFont,sans-serif;margin:32px;color:#0f172a}}
  h1{{font-size:22px;margin:0 0 8px}}
  .stats{{display:flex;gap:18px;margin:18px 0}}
  .stat{{padding:12px 18px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc}}
  table{{width:100%;border-collapse:collapse;margin-top:24px}}
  th,td{{border:1px solid #e2e8f0;padding:8px 10px;text-align:left;vertical-align:top;font-size:13px}}
  th{{background:#f1f5f9}}
  pre{{margin:0;font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:pre-wrap}}
</style></head><body>
<h1>Validity Report</h1>
<p>Session: <code>{report.get('session_id','')}</code></p>
<div class="stats">
  <div class="stat"><strong>Mutation score</strong><br>{score:.2%}</div>
  <div class="stat"><strong>Critical</strong><br>{sev.get('critical',0)}</div>
  <div class="stat"><strong>Major</strong><br>{sev.get('major',0)}</div>
  <div class="stat"><strong>Minor</strong><br>{sev.get('minor',0)}</div>
  <div class="stat"><strong>Info</strong><br>{sev.get('info',0)}</div>
</div>
<table>
  <thead><tr><th>Severity</th><th>Category</th><th>Test</th><th>Suggested fix</th></tr></thead>
  <tbody>{findings_html}</tbody>
</table>
<p style="margin-top:32px;color:#64748b;font-size:12px">
  Generated by test-validity-evaluator. Adapter: {report.get('adapter_summary',{}).get('adapter_key','?')}
</p>
</body></html>""")


# --- step implementations --------------------------------------------------

def _step_tier(args, iter_dir: Path, state: dict) -> str:
    tier_path = iter_dir / "tier.json"
    _run([sys.executable, str(SCRIPTS_DIR / "classify.py"),
          "--root", args.root, "--adapter", args.adapter, "--out", str(tier_path)])
    state["tier_path"] = str(tier_path)
    return "continue"


def _step_subject(args, iter_dir: Path, state: dict) -> str:
    tier = json.loads(Path(state["tier_path"]).read_text())
    included = [t for t in tier if t["decision"] == "included"]
    adapter_manifest = _load_adapter(Path(args.adapter))
    resolver_rel = adapter_manifest.get("implementations", {}).get("subject_resolver")
    if not resolver_rel:
        raise RuntimeError(f"Adapter {args.adapter} has no implementations.subject_resolver")
    resolve_py = (REPO_ROOT / resolver_rel).resolve()
    subject_map: list[dict] = []
    for t in included:
        proc = _run([sys.executable, str(resolve_py),
                     "--test-file", t["test_file"], "--test-id", t["test_id"]])
        if proc.returncode == 0:
            try:
                subject_map.append(json.loads(proc.stdout))
            except json.JSONDecodeError:
                pass
    subject_path = iter_dir / "subject_map.json"
    subject_path.write_text(json.dumps(subject_map, indent=2, ensure_ascii=False))
    state["subject_map_path"] = str(subject_path)
    state["included_count"] = len(included)
    state["total_tier"] = len(tier)
    state["tier_excluded"] = {t["tier"]: 1 for t in tier if t["decision"] == "excluded"}

    if not subject_map:
        # Short-circuit: empty report, skip everything else.
        empty = {
            "session_id": args.session_id,
            "summary": {"mutation_score": 0,
                        "finding_counts_by_severity": {"critical": 0, "major": 0, "minor": 0, "info": 0}},
            "findings": [],
            "scope_summary": {"total_tests_examined": len(tier), "included": 0,
                              "excluded_by_tier": {}, "excluded_by_confidence": 0, "forced_via_annotation": 0},
            "adapter_summary": {"adapter_key": "?", "adapter_version": "?",
                                "capabilities_used": [], "capabilities_missing": [],
                                "skipped_due_to_capability": 0},
            "warnings": ["No unit tests passed tier classification — pipeline halted before mutation/adversarial."],
            "notices": [],
        }
        (iter_dir / "validity_report.json").write_text(json.dumps(empty, indent=2, ensure_ascii=False))
        _render_html(empty, iter_dir / "report.html")
        state["short_circuit_empty"] = True
        return "done"
    return "continue"


def _step_mut_init_pause(args, iter_dir: Path, state: dict) -> str:
    mutation_out = iter_dir / "mutation"
    state["mutation_out"] = str(mutation_out)
    _run([sys.executable, str(SCRIPTS_DIR / "mutation_orchestrate.py"),
          "--phase", "init",
          "--subject-map", state["subject_map_path"],
          "--adapter", str(Path(args.adapter).resolve()),
          "--policy", str(Path(args.policy).resolve()),
          "--session-id", args.session_id,
          "--out-dir", str(mutation_out)])
    emit_pause(
        iter_dir,
        phase_id="mutation.semantic_operator",
        prompt=str(PROMPTS_DIR / "semantic-operator.md"),
        context=str(mutation_out / "semantic_context.json"),
        output=str(mutation_out / "semantic_mutants.json"),
        schema_hint="list[Mutant]  see contracts/types.md §3.Mutant",
        note="If no semantic mutants are warranted, write `[]`.",
    )
    return "paused"


def _step_mut_execute_pause(args, iter_dir: Path, state: dict) -> str:
    mutation_out = Path(state["mutation_out"])
    _run([sys.executable, str(SCRIPTS_DIR / "mutation_orchestrate.py"),
          "--phase", "execute", "--out-dir", str(mutation_out)])
    emit_pause(
        iter_dir,
        phase_id="mutation.survivor_diagnose",
        prompt=str(PROMPTS_DIR / "survivor-diagnose.md"),
        context=str(mutation_out / "survivors.json"),
        output=str(mutation_out / "diagnoses.json"),
        schema_hint="list[TestFinding]  see contracts/types.md §4.TestFinding",
        note="Empty list `[]` is valid when there are no survivors.",
    )
    return "paused"


def _step_mut_finalize(args, iter_dir: Path, state: dict) -> str:
    mutation_out = Path(state["mutation_out"])
    _run([sys.executable, str(SCRIPTS_DIR / "mutation_orchestrate.py"),
          "--phase", "finalize", "--out-dir", str(mutation_out)])
    report_path = mutation_out / "mutation_report.json"
    state["mutation_report"] = (
        json.loads(report_path.read_text()) if report_path.is_file()
        else {"findings": [], "metrics": {"mutation_score": 0.0}}
    )
    return "continue"


def _step_adv_init_pause(args, iter_dir: Path, state: dict) -> str:
    adv_out = iter_dir / "adversarial"
    state["adv_out"] = str(adv_out)
    _run([sys.executable, str(SCRIPTS_DIR / "adversarial_orchestrate.py"),
          "--phase", "init",
          "--subject-map", state["subject_map_path"],
          "--adapter", str(Path(args.adapter).resolve()),
          "--policy", str(Path(args.policy).resolve()),
          "--session-id", args.session_id,
          "--out-dir", str(adv_out)])
    critique_prompts = [
        str(PROMPTS_DIR / "critique-assertion.md"),
        str(PROMPTS_DIR / "critique-behavior-naming.md"),
        str(PROMPTS_DIR / "critique-coupling.md"),
        str(PROMPTS_DIR / "critique-tautology.md"),
    ]
    emit_pause(
        iter_dir,
        phase_id="adversarial.critique",
        prompt=critique_prompts,
        context=str(adv_out / "critique_context.json"),
        output=str(adv_out / "critique_findings.json"),
        schema_hint="list[TestFinding] merged across the 4 critique analyzers",
        note="Each analyzer can emit 0..N findings; concatenate into a single list.",
    )
    return "paused"


def _step_adv_generate_pause(args, iter_dir: Path, state: dict) -> str:
    adv_out = Path(state["adv_out"])
    _run([sys.executable, str(SCRIPTS_DIR / "adversarial_orchestrate.py"),
          "--phase", "generate", "--out-dir", str(adv_out)])
    adversarial_prompts = [
        str(PROMPTS_DIR / "adversarial-boundary.md"),
        str(PROMPTS_DIR / "adversarial-edge-case.md"),
        str(PROMPTS_DIR / "adversarial-fuzzer.md"),
        str(PROMPTS_DIR / "adversarial-spec-divergence.md"),
        str(PROMPTS_DIR / "adversarial-state.md"),
    ]
    emit_pause(
        iter_dir,
        phase_id="adversarial.generate_cases",
        prompt=adversarial_prompts,
        context=str(adv_out / "adversarial_context.json"),
        output=str(adv_out / "adversarial_cases.json"),
        schema_hint="list[AdversarialCase]  see contracts/types.md §5",
        note="Empty list `[]` is valid; otherwise each case needs case_id, generator, target_symbol, invocation, expected.",
    )
    return "paused"


def _step_adv_execute_finalize(args, iter_dir: Path, state: dict) -> str:
    adv_out = Path(state["adv_out"])
    _run([sys.executable, str(SCRIPTS_DIR / "adversarial_orchestrate.py"),
          "--phase", "execute", "--out-dir", str(adv_out)])
    _run([sys.executable, str(SCRIPTS_DIR / "adversarial_orchestrate.py"),
          "--phase", "finalize", "--out-dir", str(adv_out)])
    report_path = adv_out / "red_team_report.json"
    state["adv_report"] = (
        json.loads(report_path.read_text()) if report_path.is_file()
        else {"findings": [], "summary": {}}
    )
    return "continue"


def _step_aggregate(args, iter_dir: Path, state: dict) -> str:
    mutation_report = state.get("mutation_report",
                                {"findings": [], "metrics": {"mutation_score": 0.0}})
    adv_report = state.get("adv_report", {"findings": [], "summary": {}})
    findings = _aggregate(mutation_report, adv_report)
    sev_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for f in findings:
        sev_counts[f.get("severity", "info")] = sev_counts.get(f.get("severity", "info"), 0) + 1

    report = {
        "session_id": args.session_id,
        "summary": {
            "mutation_score": mutation_report.get("metrics", {}).get("mutation_score", 0.0),
            "finding_counts_by_severity": sev_counts,
        },
        "test_subject_map_ref":   state["subject_map_path"],
        "mutation_ledger_ref":    str(Path(state["mutation_out"]) / "ledger.json"),
        "adversarial_ledger_ref": str(Path(state["adv_out"]) / "ledger.json"),
        "findings": findings,
        "recommendations": [],
        "scope_summary": {
            "total_tests_examined": state.get("total_tier", 0),
            "included":             state.get("included_count", 0),
            "excluded_by_tier":     state.get("tier_excluded", {}),
            "excluded_by_confidence": 0,
            "forced_via_annotation": 0,
        },
        "adapter_summary": mutation_report.get(
            "adapter_summary",
            {"adapter_key": "?", "adapter_version": "?", "capabilities_used": [],
             "capabilities_missing": [], "skipped_due_to_capability": 0},
        ),
        "warnings": [],
        "notices": [],
    }
    (iter_dir / "validity_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _render_html(report, iter_dir / "report.html")
    state["validity_report"] = report
    return "done"


# Ordered (from_label, to_label, step_fn). The dispatcher reads `state["state"]`
# and runs steps whose `from_label` == current state, advancing until paused
# or done.
_STEPS: list[tuple[str, str, Callable]] = [
    ("init",                          "tier_done",                       _step_tier),
    ("tier_done",                     "subject_done",                    _step_subject),
    ("subject_done",                  "awaiting:semantic_mutants",       _step_mut_init_pause),
    ("awaiting:semantic_mutants",     "awaiting:diagnoses",              _step_mut_execute_pause),
    ("awaiting:diagnoses",            "mut_finalize_done",               _step_mut_finalize),
    ("mut_finalize_done",             "awaiting:critique_findings",      _step_adv_init_pause),
    ("awaiting:critique_findings",    "awaiting:adversarial_cases",      _step_adv_generate_pause),
    ("awaiting:adversarial_cases",    "adv_finalize_done",               _step_adv_execute_finalize),
    ("adv_finalize_done",             "aggregated",                      _step_aggregate),
]


# --- pipeline dispatcher ---------------------------------------------------

class _ArgsShim:
    """Reconstruct an argparse-Namespace-like object from saved state on --resume."""
    def __init__(self, d: dict):
        for k, v in d.items():
            setattr(self, k, v)


def _run_pipeline(args, iter_dir: Path, state: dict) -> dict:
    started = datetime.datetime.utcnow()
    current = state.get("state", "init")
    while True:
        # Short-circuit empty-tier case.
        if state.get("short_circuit_empty"):
            return {
                "validity_report": json.loads((iter_dir / "validity_report.json").read_text()),
                "elapsed_seconds": (datetime.datetime.utcnow() - started).total_seconds(),
                "paused": False,
            }
        # Find the next step.
        step_idx = None
        for i, (from_label, _, _) in enumerate(_STEPS):
            if from_label == current:
                step_idx = i
                break
        if step_idx is None:
            # Either fully done or unrecognised — treat as done.
            break
        from_label, to_label, fn = _STEPS[step_idx]
        result = fn(args, iter_dir, state)
        state["state"] = to_label
        write_state(iter_dir, state)
        if result == "paused":
            return {"paused": True, "state": to_label,
                    "elapsed_seconds": (datetime.datetime.utcnow() - started).total_seconds()}
        if result == "done":
            break
        current = to_label

    return {
        "validity_report": state.get("validity_report") or
            json.loads((iter_dir / "validity_report.json").read_text()),
        "elapsed_seconds": (datetime.datetime.utcnow() - started).total_seconds(),
        "paused": False,
    }


def _finalize_session(args, iter_dir: Path, result: dict) -> None:
    """Update session.json after a fully completed iteration."""
    out_root = Path(args.out_root)
    policy = json.loads(Path(args.policy).read_text())
    session = _load_or_init_session(out_root, args.session_id, policy)
    report = result["validity_report"]
    prev_findings = []
    if session["iterations"]:
        prev_findings = [f["id"] for f in session["iterations"][-1].get("findings_ids", [])]
    cur_findings = [f["id"] for f in report["findings"]]
    delta = _delta_vs_previous(prev_findings, cur_findings)
    prev_score = session["iterations"][-1].get("score", 0) if session["iterations"] else 0
    delta["score_delta"] = round(report["summary"]["mutation_score"] - prev_score, 4)
    iter_n = len(session["iterations"])
    iter_rec = {
        "iteration_index": iter_n,
        "validity_report_ref": str(iter_dir / "validity_report.json"),
        "delta_vs_previous": delta,
        "cumulative_seconds": int(result["elapsed_seconds"]),
        "cumulative_tokens": 0,
        "findings_ids": [{"id": f["id"]} for f in report["findings"]],
        "score": report["summary"]["mutation_score"],
    }
    session["iterations"].append(iter_rec)
    major_plus = sum(report["summary"]["finding_counts_by_severity"].get(k, 0)
                     for k in ("critical", "major"))
    all_suppressed = bool(report["findings"]) and all(f.get("suppressed", False) for f in report["findings"])
    terminal = _check_termination(session, policy, report["summary"]["mutation_score"], major_plus, all_suppressed)
    if terminal:
        session["status"] = "converged" if terminal in {"target_reached", "wontfix_only"} else "terminated"
        session["terminal_reason"] = terminal
    _save_session(out_root, session)
    print(json.dumps({
        "iteration": iter_n,
        "mutation_score": report["summary"]["mutation_score"],
        "by_severity": report["summary"]["finding_counts_by_severity"],
        "findings": len(report["findings"]),
        "status": session["status"],
        "terminal_reason": session["terminal_reason"],
        "validity_report": str(iter_dir / "validity_report.json"),
        "report_html": str(iter_dir / "report.html"),
        "session_state": str(_session_root(out_root, args.session_id) / "session.json"),
    }, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", help="continue from <iter_dir>; reads saved orchestrator state")
    ap.add_argument("--root")
    ap.add_argument("--adapter")
    ap.add_argument("--policy")
    ap.add_argument("--session-id", dest="session_id")
    ap.add_argument("--out-root", dest="out_root", default="out")
    args = ap.parse_args()

    if args.resume:
        iter_dir = Path(args.resume).resolve()
        state = read_state(iter_dir)
        if not state:
            print(f"[orchestrate] no saved state at {iter_dir}", file=sys.stderr)
            sys.exit(2)
        saved_args = _ArgsShim(state["args"])
        clear_pause(iter_dir)
        result = _run_pipeline(saved_args, iter_dir, state)
        if result.get("paused"):
            return
        _finalize_session(saved_args, iter_dir, result)
        return

    # Fresh run.
    missing = [k for k in ("root", "adapter", "policy", "session_id") if getattr(args, k) is None]
    if missing:
        ap.error(f"missing required: {missing}")

    policy = json.loads(Path(args.policy).read_text())
    out_root = Path(args.out_root)
    session_root = _session_root(out_root, args.session_id)
    session_root.mkdir(parents=True, exist_ok=True)
    session = _load_or_init_session(out_root, args.session_id, policy)
    iter_n = len(session["iterations"])
    iter_dir = session_root / f"iter-{iter_n}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    state: dict = {
        "state": "init",
        "args": {
            "root": str(Path(args.root).resolve()),
            "adapter": str(Path(args.adapter).resolve()),
            "policy": str(Path(args.policy).resolve()),
            "session_id": args.session_id,
            "out_root": str(out_root.resolve()),
        },
        "iter_dir": str(iter_dir),
        "started_at": _now(),
    }
    write_state(iter_dir, state)
    saved_args = _ArgsShim(state["args"])
    result = _run_pipeline(saved_args, iter_dir, state)
    if result.get("paused"):
        return
    _finalize_session(saved_args, iter_dir, result)


if __name__ == "__main__":
    main()
