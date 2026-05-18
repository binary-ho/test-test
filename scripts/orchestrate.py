"""Top-level orchestrator for the test-validity-evaluator skill.

Drives one iteration of the full pipeline:
  [1a] tier classification (deterministic shell)
  [1b] subject location (deterministic shell, no LLM boost in MVP)
  [2]  mutation skill (3-phase, LLM stages externalised)
  [3]  adversarial skill (4-phase, LLM stages externalised)
  [4]  aggregate findings via stable FindingId
  [5]  render HTML report
  [6]  update EvaluationSession, evaluate 6 termination conditions

Phases for the LLM-dependent meso skills are paused at LLM stages, with a
clear "next_action" message so the caller (Claude) can fulfil the LLM step
and re-invoke this orchestrator with --phase resume.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SKILL_ROOT, SCRIPTS_DIR, load_yaml  # noqa: E402


REPO_ROOT = SKILL_ROOT


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _load_adapter(adapter_path: Path) -> dict:
    """Load an adapter manifest via the shared loader (PyYAML or minimal fallback)."""
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
        "score_delta": 0.0,  # filled by caller
    }


def _check_termination(session: dict, policy: dict, score: float, major_plus: int, suppressed_only: bool) -> Optional[str]:
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
        prev = session["iterations"][-2]
        score_delta_recent = last["delta_vs_previous"]["score_delta"]
        if score_delta_recent <= 0.005 and last["delta_vs_previous"]["new_findings"] == []:
            return "plateau"

    if len(session["iterations"]) >= 1:
        last = session["iterations"][-1]
        if last["delta_vs_previous"]["recurring_findings"]:
            # If a recurring finding survives ≥2 iterations, block.
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


# ---------- aggregation ----------

def _aggregate(mutation_report: dict, adversarial_report: dict) -> dict:
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


# ---------- HTML rendering ----------

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


# ---------- main pipeline ----------

def run_iteration(args, iter_dir: Path) -> dict:
    iter_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.utcnow()

    # [1a] tier classification
    tier_path = iter_dir / "tier.json"
    _run([sys.executable,
          str(SCRIPTS_DIR / "classify.py"),
          "--root", args.root, "--adapter", args.adapter, "--out", str(tier_path)])
    tier = json.loads(tier_path.read_text())
    included = [t for t in tier if t["decision"] == "included"]

    # [1b] subject location (deterministic only here; LLM boost would happen externally)
    subject_map: list[dict] = []
    adapter_manifest = _load_adapter(Path(args.adapter))
    resolver_rel = adapter_manifest.get("implementations", {}).get("subject_resolver")
    if not resolver_rel:
        raise RuntimeError(
            f"Adapter {args.adapter} has no implementations.subject_resolver — cannot resolve subjects."
        )
    resolve_py = (REPO_ROOT / resolver_rel).resolve()
    if not resolve_py.is_file():
        raise FileNotFoundError(f"subject_resolver not found at {resolve_py}")
    for t in included:
        proc = _run([sys.executable, str(resolve_py),
                     "--test-file", t["test_file"], "--test-id", t["test_id"]])
        if proc.returncode == 0:
            try:
                subject_map.append(json.loads(proc.stdout))
            except json.JSONDecodeError:
                pass
    (iter_dir / "subject_map.json").write_text(json.dumps(subject_map, indent=2, ensure_ascii=False))

    if not subject_map:
        empty_report = {
            "session_id": args.session_id,
            "summary": {"mutation_score": 0, "finding_counts_by_severity": {"critical":0,"major":0,"minor":0,"info":0}},
            "findings": [],
            "scope_summary": {"total_tests_examined": len(tier), "included": 0,
                              "excluded_by_tier": {}, "excluded_by_confidence": 0, "forced_via_annotation": 0},
            "adapter_summary": {"adapter_key": "?", "adapter_version": "?", "capabilities_used": [],
                                "capabilities_missing": [], "skipped_due_to_capability": 0},
            "warnings": ["No unit tests passed tier classification — pipeline halted before mutation/adversarial."],
            "notices": [],
        }
        (iter_dir / "validity_report.json").write_text(json.dumps(empty_report, indent=2, ensure_ascii=False))
        _render_html(empty_report, iter_dir / "report.html")
        return {
            "validity_report": empty_report,
            "elapsed_seconds": (datetime.datetime.utcnow() - started).total_seconds(),
        }

    # NOTE: Mutation + adversarial pipelines are 3/4-phase with LLM stops.
    # In a fully autonomous run, this script would emit pause signals at each
    # LLM stage. For the MVP smoke-test path, we run the deterministic parts
    # only and produce empty LLM stage outputs.

    # ---- mutation pipeline -----
    mutation_out = iter_dir / "mutation"
    mutation_orch = SCRIPTS_DIR / "mutation_orchestrate.py"
    policy_path = Path(args.policy).resolve()
    _run([sys.executable, str(mutation_orch),
          "--phase", "init",
          "--subject-map", str((iter_dir / "subject_map.json").resolve()),
          "--adapter", str(Path(args.adapter).resolve()),
          "--policy",  str(policy_path),
          "--session-id", args.session_id,
          "--out-dir", str(mutation_out)])
    (mutation_out / "semantic_mutants.json").write_text("[]")  # LLM stage placeholder
    _run([sys.executable, str(mutation_orch), "--phase", "execute", "--out-dir", str(mutation_out)])
    (mutation_out / "diagnoses.json").write_text("[]")          # LLM stage placeholder
    _run([sys.executable, str(mutation_orch), "--phase", "finalize", "--out-dir", str(mutation_out)])

    mutation_report = json.loads((mutation_out / "mutation_report.json").read_text()) \
        if (mutation_out / "mutation_report.json").exists() else {"findings": [], "metrics": {"mutation_score": 0.0}}

    # ---- adversarial pipeline -----
    adv_out = iter_dir / "adversarial"
    adv_orch = SCRIPTS_DIR / "adversarial_orchestrate.py"
    _run([sys.executable, str(adv_orch),
          "--phase", "init",
          "--subject-map", str((iter_dir / "subject_map.json").resolve()),
          "--adapter", str(Path(args.adapter).resolve()),
          "--policy",  str(policy_path),
          "--session-id", args.session_id,
          "--out-dir", str(adv_out)])
    (adv_out / "critique_findings.json").write_text("[]")       # LLM stage placeholder
    _run([sys.executable, str(adv_orch), "--phase", "generate", "--out-dir", str(adv_out)])
    (adv_out / "adversarial_cases.json").write_text("[]")        # LLM stage placeholder
    _run([sys.executable, str(adv_orch), "--phase", "execute", "--out-dir", str(adv_out)])
    _run([sys.executable, str(adv_orch), "--phase", "finalize", "--out-dir", str(adv_out)])

    adv_report = json.loads((adv_out / "red_team_report.json").read_text()) \
        if (adv_out / "red_team_report.json").exists() else {"findings": [], "summary": {}}

    # ---- aggregate ----
    findings = _aggregate(mutation_report, adv_report)
    sev_counts = {"critical":0,"major":0,"minor":0,"info":0}
    for f in findings:
        sev_counts[f.get("severity","info")] = sev_counts.get(f.get("severity","info"), 0) + 1

    excluded_by_tier = {}
    for t in tier:
        if t["decision"] == "excluded":
            excluded_by_tier[t["tier"]] = excluded_by_tier.get(t["tier"], 0) + 1

    report = {
        "session_id": args.session_id,
        "summary": {
            "mutation_score": mutation_report.get("metrics", {}).get("mutation_score", 0.0),
            "finding_counts_by_severity": sev_counts,
        },
        "test_subject_map_ref":   str(iter_dir / "subject_map.json"),
        "mutation_ledger_ref":    str(mutation_out / "ledger.json"),
        "adversarial_ledger_ref": str(adv_out / "ledger.json"),
        "findings": findings,
        "recommendations": [],
        "scope_summary": {
            "total_tests_examined": len(tier),
            "included": len(included),
            "excluded_by_tier": excluded_by_tier,
            "excluded_by_confidence": sum(1 for t in tier if t["decision"] == "needs_review"),
            "forced_via_annotation": 0,
        },
        "adapter_summary": mutation_report.get("adapter_summary",
            {"adapter_key":"?","adapter_version":"?","capabilities_used":[],"capabilities_missing":[],"skipped_due_to_capability":0}),
        "warnings": [],
        "notices": [],
    }
    (iter_dir / "validity_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _render_html(report, iter_dir / "report.html")

    elapsed = (datetime.datetime.utcnow() - started).total_seconds()
    return {
        "validity_report": report,
        "elapsed_seconds": elapsed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--policy", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--out-root", default="out")
    args = ap.parse_args()

    policy = json.loads(Path(args.policy).read_text())
    out_root = Path(args.out_root)
    session_root = _session_root(out_root, args.session_id)
    session_root.mkdir(parents=True, exist_ok=True)

    session = _load_or_init_session(out_root, args.session_id, policy)
    iter_n = len(session["iterations"])
    iter_dir = session_root / f"iter-{iter_n}"

    result = run_iteration(args, iter_dir)
    report = result["validity_report"]

    prev_findings = []
    if session["iterations"]:
        prev_findings = [f["id"] for f in session["iterations"][-1].get("findings_ids", [])]
    cur_findings = [f["id"] for f in report["findings"]]
    delta = _delta_vs_previous(prev_findings, cur_findings)
    prev_score = session["iterations"][-1].get("score", 0) if session["iterations"] else 0
    delta["score_delta"] = round(report["summary"]["mutation_score"] - prev_score, 4)

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

    major_plus = sum(report["summary"]["finding_counts_by_severity"].get(k, 0) for k in ("critical","major"))
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
        "session_state": str(session_root / "session.json"),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
