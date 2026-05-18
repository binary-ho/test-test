"""Adversarial testing meso skill orchestrator.

Four phases:
  init     — emits Track A critique prompt context (per test file).
  generate — consumes critique_findings.json, emits Track B adversarial context.
  execute  — runs each adversarial case via the adapter runner, classifies verdict.
  finalize — clusters findings, attaches suppressions, scores summary, writes report.

LLM stages (critique analyzers, adversarial generators) read prompts under
../prompts/ and write JSON outputs that this script consumes.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Optional


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SKILL_ROOT, load_yaml  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_ROOT


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_yaml(p: Path) -> dict:
    return load_yaml(p)


def _load_suppressions(yml_path: Path) -> set[str]:
    if not yml_path.exists():
        return set()
    out: set[str] = set()
    for line in yml_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("- finding_id:") or line.startswith("finding_id:"):
            out.add(line.split(":", 1)[1].strip())
    return out


def _empty_ledger(adapter_key: str) -> dict:
    return {
        "generated_at": _now(),
        "tool_versions": {"adapter": f"{adapter_key}@0.1.0", "skill": "0.1.0"},
        "adapter_key": adapter_key,
        "records": [],
    }


def _save_run_cfg(out_dir: Path, args) -> None:
    (out_dir / "_run.json").write_text(json.dumps({
        "subject_map": str(Path(args.subject_map).resolve()),
        "adapter":     str(Path(args.adapter).resolve()),
        "policy":      str(Path(args.policy).resolve()),
        "session_id":  args.session_id,
    }, indent=2))


def _read_run_cfg(out_dir: Path) -> dict:
    return json.loads((out_dir / "_run.json").read_text())


# ---------- phase init ----------

def phase_init(args) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _save_run_cfg(out_dir, args)

    subject_map = json.loads(Path(args.subject_map).read_text())
    policy = json.loads(Path(args.policy).read_text())
    adapter = _load_yaml(Path(args.adapter))

    ledger = _empty_ledger(adapter["key"])
    (out_dir / "ledger.json").write_text(json.dumps(ledger, indent=2))

    enabled = policy.get("critique", {}).get(
        "enabled_analyzers", ["assertion","tautology","coupling","behavior_naming"])

    critique_ctx = []
    for entry in subject_map if isinstance(subject_map, list) else [subject_map]:
        test_id = entry.get("test_id", "")
        test_file = test_id.split("::")[0] if test_id else ""
        try:
            test_content = Path(test_file).read_text() if test_file else ""
        except OSError:
            test_content = ""
        critique_ctx.append({
            "test_id": test_id,
            "test_file": test_file,
            "test_content": test_content,
            "primary_subjects": entry.get("primary_subjects", []),
            "analyzers_requested": enabled,
        })

    (out_dir / "critique_context.json").write_text(json.dumps(critique_ctx, indent=2, ensure_ascii=False))
    print(json.dumps({
        "phase": "init",
        "tests_to_critique": len(critique_ctx),
        "analyzers": enabled,
        "next_action": (
            f"LLM runs each enabled analyzer per prompts/critique-*.md "
            f"against {out_dir}/critique_context.json, writes findings to "
            f"{out_dir}/critique_findings.json, then re-invoke --phase generate"
        ),
    }, indent=2, ensure_ascii=False))


# ---------- phase generate ----------

def phase_generate(args) -> None:
    out_dir = Path(args.out_dir)
    run_cfg = _read_run_cfg(out_dir)
    subject_map = json.loads(Path(run_cfg["subject_map"]).read_text())
    policy = json.loads(Path(run_cfg["policy"]).read_text())

    crit_path = out_dir / "critique_findings.json"
    critique_findings = json.loads(crit_path.read_text()) if crit_path.exists() else []

    # Targeting weights from Track A categories
    cat_counter = Counter(f.get("category") for f in critique_findings)
    weights = {"edge_case": 1.0, "boundary": 1.0, "state": 1.0,
               "fuzzer": 1.0, "spec_divergence": 1.0}
    if cat_counter.get("weak_assertion", 0) + cat_counter.get("tautology", 0) > 0:
        weights["edge_case"] += 0.5
        weights["fuzzer"] += 0.5
    if cat_counter.get("name_behavior_mismatch", 0) > 0:
        weights["spec_divergence"] += 1.0
    if cat_counter.get("over_coupling", 0) > 0:
        weights["state"] += 0.5

    enabled_gens = policy.get("adversarial", {}).get(
        "enabled_generators",
        ["edge_case","boundary","state","fuzzer","spec_divergence"])

    adv_ctx = []
    for entry in subject_map if isinstance(subject_map, list) else [subject_map]:
        for subj in entry.get("primary_subjects", []):
            try:
                src = Path(subj["file"]).read_text().splitlines(keepends=True)
                body = "".join(src[subj["span"][0]-1:subj["span"][1]])
            except OSError:
                body = ""
            adv_ctx.append({
                "test_id": entry.get("test_id"),
                "subject": subj,
                "function_body": body,
                "critique_signals": [
                    {"category": f.get("category"), "test_id": f.get("test_id")}
                    for f in critique_findings
                    if f.get("test_id") == entry.get("test_id")
                ],
                "weights": weights,
                "generators_requested": enabled_gens,
            })

    (out_dir / "adversarial_context.json").write_text(
        json.dumps(adv_ctx, indent=2, ensure_ascii=False))

    # Append critique findings to ledger.
    ledger = json.loads((out_dir / "ledger.json").read_text())
    for f in critique_findings:
        case = {
            "case_id": f"crit_{f.get('id','??')[:8]}",
            "generator": f"critique:{f.get('category','?')}",
            "target_symbol": f.get("test_id",""),
            "input_repr": "",
            "expected_behavior": "",
            "observed_behavior": f.get("evidence", {}).get("test_snippet",""),
            "verdict": "uncaught",
            "finding_id": f.get("id"),
        }
        ledger["records"].append(case)
    (out_dir / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False))

    print(json.dumps({
        "phase": "generate",
        "critique_findings_consumed": len(critique_findings),
        "subjects_to_attack": len(adv_ctx),
        "weights": weights,
        "next_action": (
            f"LLM runs each enabled generator per prompts/adversarial-*.md "
            f"against {out_dir}/adversarial_context.json, writes cases to "
            f"{out_dir}/adversarial_cases.json, then re-invoke --phase execute"
        ),
    }, indent=2, ensure_ascii=False))


# ---------- phase execute ----------

def phase_execute(args) -> None:
    out_dir = Path(args.out_dir)
    run_cfg = _read_run_cfg(out_dir)
    adapter = _load_yaml(Path(run_cfg["adapter"]))
    policy = json.loads(Path(run_cfg["policy"]).read_text())

    cases_path = out_dir / "adversarial_cases.json"
    cases = json.loads(cases_path.read_text()) if cases_path.exists() else []

    ledger = json.loads((out_dir / "ledger.json").read_text())

    run_path = REPO_ROOT / adapter["implementations"]["runner"]
    degradations: list[dict] = []
    state_counts: dict[str, list[str]] = {}  # case_id -> [verdict, verdict, ...]
    allow_nondet = policy.get("adversarial", {}).get("state", {}).get("allow_nondeterminism", False)
    execute_cases = policy.get("adversarial", {}).get("execute_dynamic_cases", True)

    for case in cases:
        ledger_rec = {
            "case_id": case["case_id"],
            "generator": case.get("generator", "edge_case"),
            "target_symbol": case.get("target_symbol", ""),
            "input_repr": case.get("input_repr", ""),
            "expected_behavior": case.get("expected_behavior", ""),
            "observed_behavior": "",
            "verdict": "inconclusive",
        }

        if not execute_cases:
            ledger["records"].append(ledger_rec)
            continue

        is_state = case.get("generator") == "state"
        runs = 3 if (is_state and allow_nondet) else 1
        verdicts: list[str] = []
        last_obs = ""
        for _ in range(runs):
            tmp = out_dir / f"_case_{case['case_id']}.json"
            tmp.write_text(json.dumps(case))
            try:
                proc = subprocess.run(
                    [sys.executable, str(run_path), "--case", str(tmp)],
                    capture_output=True, text=True, timeout=30,
                )
                tmp.unlink(missing_ok=True)
                try:
                    out = json.loads(proc.stdout)
                    verdicts.append(out.get("verdict", "inconclusive"))
                    last_obs = out.get("observed_behavior", "")[:400]
                except json.JSONDecodeError:
                    verdicts.append("inconclusive")
                    degradations.append({
                        "stage": "adversarial.runner", "error_kind": "json_decode",
                        "detail": proc.stderr[:200], "occurred_at": _now(),
                    })
            except subprocess.TimeoutExpired:
                verdicts.append("inconclusive")
                degradations.append({
                    "stage": "adversarial.runner", "error_kind": "timeout",
                    "detail": f"timeout on case {case['case_id']}", "occurred_at": _now(),
                })

        # Determinism gate for state-attacker (Quality bar #2).
        if is_state and allow_nondet:
            if len(set(verdicts)) == 1 and verdicts[0] != "inconclusive":
                ledger_rec["verdict"] = verdicts[0]
            else:
                ledger_rec["verdict"] = "inconclusive"
        else:
            ledger_rec["verdict"] = verdicts[0] if verdicts else "inconclusive"
        ledger_rec["observed_behavior"] = last_obs

        ledger["records"].append(ledger_rec)

    (out_dir / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    (out_dir / "degradations.json").write_text(json.dumps(degradations, indent=2))

    by_v = Counter(r["verdict"] for r in ledger["records"])
    print(json.dumps({
        "phase": "execute",
        "cases_in_ledger": len(ledger["records"]),
        "by_verdict": dict(by_v),
        "degradations": len(degradations),
        "next_action": "Re-invoke --phase finalize to cluster findings and write the report.",
    }, indent=2, ensure_ascii=False))


# ---------- phase finalize ----------

def phase_finalize(args) -> None:
    out_dir = Path(args.out_dir)
    run_cfg = _read_run_cfg(out_dir)
    adapter = _load_yaml(Path(run_cfg["adapter"]))
    policy = json.loads(Path(run_cfg["policy"]).read_text())
    ledger = json.loads((out_dir / "ledger.json").read_text())

    # Carry over critique findings, build new ones from uncaught adversarial cases.
    critique_path = out_dir / "critique_findings.json"
    findings = json.loads(critique_path.read_text()) if critique_path.exists() else []

    # Cluster uncaught dynamic cases by (test_id || target_symbol, category).
    cluster: dict[tuple, dict] = {}
    for r in ledger["records"]:
        if r.get("verdict") != "uncaught" or r.get("generator", "").startswith("critique:"):
            continue
        gen = r.get("generator", "edge_case")
        cat_map = {
            "edge_case": "missed_edge_case", "boundary": "off_by_one",
            "state": "state_violation",      "fuzzer": "fuzz_counterexample",
            "spec_divergence": "spec_divergence",
        }
        cat = cat_map.get(gen, "missed_edge_case")
        key = (r.get("target_symbol", ""), cat)
        if key not in cluster:
            sig = f"{r.get('target_symbol','')}\n{cat}\n{r.get('input_repr','')}"
            fid = hashlib.sha256(sig.encode()).hexdigest()[:12]
            cluster[key] = {
                "id": fid,
                "test_id": r.get("target_symbol", ""),
                "severity": "major",
                "category": cat,
                "evidence": {
                    "case_repr": f"input={r.get('input_repr','')}, "
                                 f"expected={r.get('expected_behavior','')}, "
                                 f"observed={r.get('observed_behavior','')[:120]}",
                },
                "suggested_fix": "# add a test that asserts the expected behavior for this case",
                "confidence": 0.75,
                "caused_by_cases": [],
            }
        cluster[key]["caused_by_cases"].append(r["case_id"])
    findings.extend(cluster.values())

    # Apply suppressions.
    supp_yml = policy.get("session", {}).get("suppressions_yml", "")
    suppressed = _load_suppressions(Path(supp_yml)) if supp_yml else set()
    final_findings: list[dict] = []
    for f in findings:
        if f.get("id") in suppressed:
            for r in ledger["records"]:
                if r.get("finding_id") == f.get("id"):
                    r["suppressed"] = True
            continue
        final_findings.append(f)

    # Confidence demotion per Quality bar #3.
    min_conf = policy.get("critique", {}).get("min_confidence_to_report", 0.6)
    for f in final_findings:
        if f.get("confidence", 1.0) < min_conf:
            f["severity"] = "info"

    sev_counts = Counter(f.get("severity", "info") for f in final_findings)
    cat_counts = Counter(f.get("category", "?") for f in final_findings)

    by_v = Counter(r["verdict"] for r in ledger["records"])

    report = {
        "summary": {
            "findings_by_severity": {
                "critical": sev_counts.get("critical", 0),
                "major":    sev_counts.get("major", 0),
                "minor":    sev_counts.get("minor", 0),
                "info":     sev_counts.get("info", 0),
            },
            "findings_by_category": dict(cat_counts),
            "cases_generated": len(ledger["records"]),
            "cases_executed":  len([r for r in ledger["records"] if r.get("verdict") != "inconclusive"]),
            "cases_caught_by_existing_test": by_v.get("caught_by_existing_test", 0),
            "cases_uncaught":                by_v.get("uncaught", 0),
        },
        "findings": final_findings,
        "adversarial_ledger_ref": "ledger.json",
        "degradations": json.loads((out_dir / "degradations.json").read_text())
                        if (out_dir / "degradations.json").exists() else [],
        "adapter_summary": {
            "adapter_key":     adapter["key"],
            "adapter_version": adapter.get("version", "0.0.0"),
            "capabilities_used":    [],
            "capabilities_missing": [],
            "skipped_due_to_capability": 0,
        },
    }
    (out_dir / "red_team_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out_dir / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False))

    print(json.dumps({
        "phase": "finalize",
        "findings": len(final_findings),
        "by_severity": dict(sev_counts),
        "by_category": dict(cat_counts),
        "suppressed": len(suppressed),
        "report": str(out_dir / "red_team_report.json"),
        "ledger": str(out_dir / "ledger.json"),
    }, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["init", "generate", "execute", "finalize"], required=True)
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
    elif args.phase == "generate":
        phase_generate(args)
    elif args.phase == "execute":
        phase_execute(args)
    else:
        phase_finalize(args)


if __name__ == "__main__":
    main()
