---
name: test-validity-evaluator
description: Top-level entry point for evaluating test quality. Use whenever the user asks to "check if my tests really catch bugs", "run mutation testing", "evaluate test coverage of business logic", or "find weak tests". Runs the full pipeline — tier classification → subject location → mutation testing + adversarial testing in parallel → aggregation → HTML report. Manages an EvaluationSession with 6 termination conditions so evaluate→fix→re-evaluate loops can't go infinite.
---

# test-validity-evaluator

테스트 유효성 평가의 단일 진입점. 사용자 입장에서는 도구가 사실상 하나.

## When to use

- 사용자가 테스트 품질 평가·mutation 점수·red team analysis 요청.
- PR 리뷰 시 변경된 코드의 테스트가 충분한지 검증.
- 새 테스트를 작성한 후 보강 루프 (`evaluate → fix → re-evaluate`).

호출하지 말아야 할 경우:

- 단일 primitive 작업만 필요 (해당 스킬을 직접 호출).
- e2e/integration 테스트 전용 프로젝트 (tier-classifier가 다 걸러내 빈 결과).

## How to invoke

```bash
python3 skills/test-validity-evaluator/scripts/orchestrate.py \
    --root .  \
    --adapter contracts/adapters/python.pytest.yaml \
    --policy evaluation_policy.json \
    --session-id evs_$(date +%Y%m%d_%H%M%S)
```

결과:

- `out/<session>/iter-N/validity_report.json` — 각 iteration의 ValidityReport
- `out/<session>/iter-N/report.html` — 사람용 HTML
- `out/<session>/session.json` — EvaluationSession 상태

orchestrate.py는 다음을 순서대로 수행:

1. tier classifier → included 테스트만 골라냄
2. subject locator → TestSubjectMap 생산
3. Mutation 트랙 (`llm-based-semantic-mutation-testing`) 호출 → MutationLedger + findings
4. Adversarial 트랙 (`agentic-adversarial-testing`) 호출 → AdversarialLedger + findings
5. Aggregation — dedupe·우선순위·클러스터링 → ValidityReport
6. HTML 렌더 (자체 포함 단일 파일)
7. EvaluationSession 상태 업데이트, 종료 조건 평가

## Input

```jsonc
{
  "root": "프로젝트 루트",
  "adapter": "어댑터 매니페스트 yaml 경로",
  "policy": {
    "session": {
      "session_id": "evs_…",
      "max_iterations": 5,
      "target_mutation_score": 0.85,
      "budget": { "max_tokens": 600000, "max_seconds": 1800 },
      "cache_enabled": true,
      "equivalent_mutants_yml": ".testvalidity/equivalent_mutants.yml",
      "suppressions_yml":       ".testvalidity/suppressions.yml"
    },
    "mutation":    { /* MutationPolicy   */ },
    "adversarial": { /* AdversarialPolicy */ }
  }
}
```

## Output — `ValidityReport`

`contracts/types.md` §4 참조. 핵심 요약:

```jsonc
{
  "session_id": "evs_…",
  "summary": {
    "mutation_score": 0..1,
    "finding_counts_by_severity": { "critical": 0, "major": 0, "minor": 0, "info": 0 }
  },
  "test_subject_map_ref": "./test_subject_map.json",
  "mutation_ledger_ref":  "./ledger/mutation.json",
  "adversarial_ledger_ref": "./ledger/adversarial.json",
  "findings": [ /* dedupe+클러스터된 TestFinding[] */ ],
  "recommendations": [ ... ],
  "scope_summary":     { ... },
  "adapter_summary":   { ... },
  "warnings": [ ... ],
  "notices":  [ ... ]
}
```

함께 `report.html`이 같은 디렉터리에 떨어집니다.

## EvaluationSession 종료 조건 (6종)

먼저 만나는 것 적용:

| 조건 | 정의 | 상태 |
|---|---|---|
| `target_reached` | mutation_score ≥ target AND major+ == 0 | converged |
| `wontfix_only` | 남은 finding이 전부 suppressed | converged |
| `max_iterations` | 기본 5회 | terminated |
| `plateau` | 연속 2회 동안 major+ 감소 없음 AND score 개선 ≤ 0.5%p | terminated |
| `recurrence_blocked` | 같은 finding_id가 "fixed" 주장 후 2회 이상 재등장 | terminated ★ |
| `budget_exhausted` | 누적 토큰/시간 한도 초과 | terminated |

`recurrence_blocked`가 가장 중요한 가드. orchestrate.py가 iteration마다 `delta_vs_previous.recurring_findings`를 계산하고, 같은 finding_id가 2회+ 재등장하면 종료.

## Procedure (orchestrate.py 내부)

각 iteration N에 대해:

```
[1a] python3 skills/test-tier-classifier/scripts/classify.py \
        --root <root> --adapter <adapter> --out iter-N/tier.json
[1b] for each included test:
        python3 contracts/adapters/python_pytest/resolve.py \
            --test-file ... --test-id ...
     (모아서 iter-N/subject_map.json)
[2]  python3 skills/llm-based-semantic-mutation-testing/scripts/orchestrate.py \
        --phase init --subject-map iter-N/subject_map.json ...
     (LLM이 semantic mutants 작성)
     --phase execute
     (LLM이 survivor diagnoses 작성)
     --phase finalize
[3]  python3 skills/agentic-adversarial-testing/scripts/orchestrate.py \
        --phase init ...
     (LLM critique findings)
     --phase generate / execute / finalize
[4]  aggregate.py — dedupe by FindingId, cluster, prioritize → ValidityReport
[5]  render_html.py — ValidityReport + ledgers → report.html
[6]  session.py — append IterationRecord, check termination
```

## Quality bar

오케스트레이터 책임:

1. **Triage.** diff 크기·복잡도를 보고 mode를 선택 (light·normal·max).
2. **Budget allocation.** 각 메소 스킬에 시간·토큰 분배. 한 메소가 폭주해도 부분 결과 받기.
3. **Aggregation.** 여러 출처의 TestFinding을 안정 `FindingId`로 dedupe → 클러스터링 → severity 우선순위.
4. **Termination.** 6개 종료 조건을 모두 평가. recurrence_blocked는 무한 루프 방지의 마지막 보루.
5. **Honest reporting.** abort된 메소 스킬·skip된 capability는 `warnings`/`notices`에 기록.

## Examples

### 첫 실행 (단발)

```bash
$ python3 skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . --adapter contracts/adapters/python.pytest.yaml \
    --policy evaluation_policy.json --session-id evs_2026-05-18_a1b2

[evaluate] tier classification: 31 included, 10 excluded
[evaluate] subject location: 31 TestSubjectMap entries, avg confidence 0.78
[evaluate] mutation pipeline (8min 12s): 142 mutants → score 0.76, 9 findings
[evaluate] adversarial pipeline (3min 41s): 60 cases → 5 findings
[evaluate] aggregated: 12 findings (3 major, 9 minor)
[session ] iteration 0: score=0.76, status=in_progress
```

### Iteration 2 (보강 후)

```bash
$ python3 ... --session-id evs_2026-05-18_a1b2  # 같은 ID로 재호출

[session ] resuming session evs_2026-05-18_a1b2
[evaluate] ...
[session ] iteration 1: score=0.91, status=converged (target_reached)
```

### recurrence_blocked

```bash
[session ] iteration 2: recurring findings detected: [f_a1b2c3, f_d4e5f6]
[session ] terminating: recurrence_blocked (same finding survives 2+ fixed claims)
```

## Anti-patterns

- 모든 PR에 max 모드를 강제 — 비용 폭발.
- recurrence_blocked를 끄는 환경변수 추가 — 무한 루프의 마지막 방지선.
- iteration 결과를 합치지 않고 마지막 것만 보고 — `IterationRecord[]`가 보강 효과의 증거.
- LLM 진단/생성 단계 실패 시 침묵 — degradations/warnings에 정직하게.

## Notes

- HTML 렌더링은 같은 스크립트 안에서 inline (render_html.py). 자체 포함 단일 파일.
- 모든 iteration의 산출물은 `out/<session>/iter-N/` 아래 시간순.
- 사람 환류는 HTML의 `[mark equivalent]`/`[mark wontfix]`/`[annotate]` 버튼이 YAML로 적재 (다음 세션 자동 적용).
