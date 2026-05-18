---
name: test-validity-evaluator
description: Use whenever the user asks to evaluate test quality, run mutation testing, find weak tests, check if tests actually catch bugs, score a test suite, run a red-team review of tests, 테스트 품질 평가, 변이 테스팅, mutation score, 테스트가 버그를 잡는지 검증, 약한 테스트 찾기. Pipeline — tier-classification → subject-location → mutation + adversarial → aggregated ValidityReport JSON + HTML. Bounded by 6 termination conditions so evaluate→fix→re-evaluate loops can't go infinite.
---

# test-validity-evaluator

테스트 유효성 평가의 단일 진입점. Claude Code skill로 글로벌 설치 시 어떤 레포에서든 자동 트리거.

## When to use

- 사용자가 테스트 품질 평가·mutation 점수·red team analysis 요청.
- PR 리뷰 시 변경된 코드의 테스트가 충분한지 검증.
- 새 테스트를 작성한 후 보강 루프 (`evaluate → fix → re-evaluate`).

## When NOT to use

- 단일 primitive 작업만 필요한 경우 (sub-orchestrator를 직접 호출하는 게 빠름).
- e2e/integration 테스트 전용 프로젝트 — tier-classifier가 다 걸러내 빈 결과를 줌.

## How to invoke

이 skill이 설치되어 있다면 (`~/.claude/skills/test-validity-evaluator/`), 어떤 레포에서든 Bash tool로 다음을 실행:

```bash
# 어댑터는 패키지 안에 동봉됨. 현재 프로젝트의 언어/프레임워크에 맞춰 선택.
ADAPTER=~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml
# Kotlin 프로젝트라면: contracts/adapters/kotlin.junit.yaml

# 정책 파일은 타겟 레포의 루트에 두는 게 표준 (또는 skill 안에서 디폴트 사용)
POLICY=./evaluation_policy.json

python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . \
    --adapter "$ADAPTER" \
    --policy "$POLICY" \
    --session-id "evs_$(date +%Y%m%d_%H%M%S)"
```

`evaluation_policy.json`이 타겟 레포에 없으면 다음을 생성:

```jsonc
{
  "session": {
    "session_id": "evs_…",
    "max_iterations": 5,
    "target_mutation_score": 0.85,
    "budget": { "max_tokens": 600000, "max_seconds": 1800 },
    "cache_enabled": true,
    "equivalent_mutants_yml": ".testvalidity/equivalent_mutants.yml",
    "suppressions_yml":       ".testvalidity/suppressions.yml"
  },
  "operators": {
    "syntactic_enabled": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
    "semantic_enabled": true
  },
  "budget": { "max_mutants": 200 },
  "mutation":    {},
  "adversarial": {}
}
```

결과:

- `out/<session>/iter-N/validity_report.json` — 각 iteration의 ValidityReport
- `out/<session>/iter-N/report.html` — 사람용 HTML
- `out/<session>/session.json` — EvaluationSession 상태

orchestrate.py는 다음을 순서대로 수행:

1. tier classifier → included 테스트만 골라냄 (`scripts/classify.py`)
2. subject locator → TestSubjectMap 생산 (adapter의 `subject_resolver`)
3. Mutation 트랙 (`scripts/mutation_orchestrate.py`) → MutationLedger + findings
4. Adversarial 트랙 (`scripts/adversarial_orchestrate.py`) → AdversarialLedger + findings
5. Aggregation — dedupe·우선순위·클러스터링 → ValidityReport
6. HTML 렌더 (자체 포함 단일 파일)
7. EvaluationSession 상태 업데이트, 종료 조건 평가

## Supported adapters

패키지에 동봉:

- `contracts/adapters/python.pytest.yaml` — Python + pytest (완성)
- `contracts/adapters/kotlin.junit.yaml` — Kotlin + JUnit 5 (분류·subject location 완성, mutation은 stub)

새 어댑터 추가는 `contracts/LanguageAdapter.schema.json` 참조.

## Input

```jsonc
{
  "root": "프로젝트 루트",
  "adapter": "어댑터 매니페스트 yaml 경로",
  "policy": "evaluation_policy.json 경로"
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
  "test_subject_map_ref":   "./subject_map.json",
  "mutation_ledger_ref":    "./mutation/ledger.json",
  "adversarial_ledger_ref": "./adversarial/ledger.json",
  "findings": [ /* dedupe+클러스터된 TestFinding[] */ ],
  "recommendations": [ ... ],
  "scope_summary":   { ... },
  "adapter_summary": { ... },
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

## Examples

### 첫 실행 (단발)

```bash
$ python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
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
[session ] iteration 1: score=0.91, status=converged (target_reached)
```

### recurrence_blocked

```bash
[session ] iteration 2: recurring findings detected: [f_a1b2c3, f_d4e5f6]
[session ] terminating: recurrence_blocked (same finding survives 2+ fixed claims)
```

## Anti-patterns

- 모든 PR에 max 모드를 강제 — 비용 폭발.
- `recurrence_blocked`를 끄는 환경변수 추가 — 무한 루프의 마지막 방지선.
- iteration 결과를 합치지 않고 마지막 것만 보고 — `IterationRecord[]`가 보강 효과의 증거.
- LLM 진단/생성 단계 실패 시 침묵 — `degradations`/`warnings`에 정직하게.

## Notes

- 사용자별 override(`equivalent_mutants.yml`, `suppressions.yml`)는 **타겟 레포의 `.testvalidity/` 디렉터리**에서 읽힘. policy.json의 path 필드가 cwd 기준 상대경로로 해석.
- 모든 iteration의 산출물은 `out/<session>/iter-N/` 아래 시간순. `out/`은 타겟 레포 cwd 기준이라 `.gitignore`에 추가 권장.
- Sub-skill spec 문서: `docs/spec/{tier-classifier,mutation,adversarial,subject-locator}.md`.
