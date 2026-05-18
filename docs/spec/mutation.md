---
name: llm-based-semantic-mutation-testing
description: Use when the user wants to measure if their tests actually catch business-logic bugs — i.e. mutation testing with both deterministic AST operators (AOR/ROR/COR/LCR/BoundaryShift/ConstantReplace) and LLM-generated semantic mutations that target the function's actual business intent. Run this after subject-under-test-locator. Produces a MutationLedger of every mutant (killed, survived, equivalent, budget-dropped) plus TestFindings with concrete suggested test additions for every survivor.
---

# llm-based-semantic-mutation-testing

> **Spec doc (internal)** — 이 문서는 `test-validity-evaluator` 패키지 내부 sub-skill의 설계 사양입니다.
> Claude Code 자동 트리거 대상은 패키지 루트의 `SKILL.md`(`test-validity-evaluator`) 하나뿐. 이 sub-skill은 그 오케스트레이터에서 subprocess로 호출됩니다.
> 직접 실행: `python3 ~/.claude/skills/test-validity-evaluator/scripts/mutation_orchestrate.py ...`

원본 비즈니스 로직에 결정론적·의미 변이를 주입해 테스트가 잡는지 측정. 살아남은 변이는 *왜* 못 잡았는지 진단해 `TestFinding`으로 변환. 모든 변이 이력은 `MutationLedger`에 전수 보존.

## When to use

- diff에 비즈니스 로직 분기가 포함됨 (`if`/`switch`, 가격 계산, 권한 체크, 상태 전이).
- `subject-under-test-locator`가 `primary_subjects.confidence ≥ 0.7`을 반환한 테스트가 있음.
- 정책에서 `mutation.enabled = true` (mode: light · normal · max).
- 보안·결제 영역의 PR 리뷰.

호출하지 말아야 할 경우:

- tier가 unit이 아닌 테스트 — `test-tier-classifier`가 이미 거름.
- `TestSubjectMap.primary_subjects`가 비어 있음.
- 어댑터 매칭 실패 — `AdapterMissingError`로 fail-fast.

## How to invoke

전체 파이프라인은 한 셸 명령으로 끝납니다. orchestrate.py가 1–6단계를 순서대로 실행합니다.

```bash
python3 ~/.claude/skills/test-validity-evaluator/scripts/mutation_orchestrate.py \
    --phase init \
    --subject-map subject_map.json \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
    --policy policy.json \
    --session-id evs_2026-05-18_a1b2 \
    --out-dir out/mutation/
# 이후 --phase execute, --phase finalize 차례로 호출 (LLM 단계 사이에 caller가 결과를 채워 넣음)
```

산출:

- `out/mutation/mutation_report.json` (메트릭 + findings + ledger 경로)
- `out/mutation/ledger.json` (`MutationLedger`)
- 표준 에러로 진행 로그

내부 6단계는 아래 Procedure에 명시. 사용자가 직접 한 단계를 손으로 돌리고 싶으면 각 스크립트를 직접 호출해도 됩니다.

## Input

`contracts/types.md`의 컨트랙트 참조. 핵심 필드만 발췌:

```jsonc
{
  "test_subject_map": [ /* TestSubjectMap[] */ ],
  "adapter":          { /* LanguageAdapter */ },
  "policy": {
    "mode": "light" | "normal" | "max",
    "budget": { "max_mutants": 200, "max_seconds": 600, "max_tokens": 200000 },     // defaults
    "scope":  { "diff_only": true, "include_files": ["src/**"], "exclude_files": [] },
    "operators": {
      "syntactic_enabled": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
      "semantic_enabled":  true
    },
    "equivalence": { "llm_judge": true, "smt_when_available": true,
                     "exclude_from_score": "likely_equivalent" }
  },
  "session": {
    "session_id":             "evs_…",
    "cache_enabled":          true,
    "equivalent_mutants_yml": ".testvalidity/equivalent_mutants.yml"
  }
}
```

## Output

```jsonc
{
  "mutation_report": {
    "metrics": {
      "mutation_score": 0..1,                 // killed / (total - filtered_equivalent)
      "killed": N, "survived": N, "timeout": N, "error": N,
      "filtered_equivalent": N, "budget_dropped": N,
      "by_operator": { "AOR": { ... }, "semantic": { ... } },
      "hotspots":    [ { "file": "...", "survived": N } ]
    },
    "findings":            [ /* TestFinding[] */ ],
    "mutation_ledger_ref": "ledger.json",
    "degradations":        [ /* FailureRecord[] */ ],
    "adapter_summary": {
      "adapter_key": "python.pytest",
      "adapter_version": "0.1.0",
      "capabilities_used":    [ ... ],
      "capabilities_missing": [ ... ],
      "skipped_due_to_capability": N
    }
  },
  "mutation_ledger": { /* MutationLedger (전수 보존) */ }
}
```

`MutationLedger.records[]`는 모든 변이(필터된 것, budget_dropped, cache hit 포함)를 시간순으로 보유. `cache_status` 필드 포함.

## Procedure

orchestrate.py가 자동으로 수행하는 6단계. LLM이 절차를 시뮬레이션해야 하는 단계는 **명시**되어 있습니다.

### 1. 스코핑

`test_subject_map.primary_subjects ∩ policy.scope.diff_only` → 변이 대상 span 집합 `S`. `S`가 비면 mode를 `light`로 강등하고 `notices`에 "no diff overlap" 기록.

### 2. 변이 생성 (병렬)

#### 2a. Syntactic — 어댑터의 mutate.py 호출 (결정론)

```bash
python3 ~/.claude/skills/test-validity-evaluator/contracts/adapters/python_pytest/mutate.py \
    --file src/pricing.py --span 40 72 \
    --operators AOR ROR COR LCR BoundaryShift ConstantReplace \
    --out tmp/syntactic_mutants.json
```

#### 2b. Semantic — LLM 호출 (의미)

`prompts/semantic-operator.md` 프롬프트를 사용. 입력은 함수 본문 + 주변 호출 컨텍스트(컨트랙트 미준수: 함수 시그니처만 보고 만든 변이는 반려). 출력 셰입은 `Mutant`. 모든 semantic 변이는 `intent` 필드 필수.

생성 결과를 합쳐 `ledger.add_candidates(mutants)`로 ledger에 선기록(execution_status=null).

`budget.max_mutants` 초과 시 우선순위(diff 거리 → semantic/syntactic 균형 → operator 다양성)로 잘라냄. 잘린 후보도 `execution_status=budget_dropped`로 ledger에 보존.

### 3. 등가성 필터

```bash
python3 scripts/equivalence_filter.py --in tmp/all_mutants.json --out tmp/filtered.json
```

내부적으로:

1. `equivalent_mutants.yml`에 매칭되는 mutant는 `force_equivalent`.
2. 그 외는 `prompts/equivalence-judge.md`로 LLM 판정 → `definitely_distinct | likely_equivalent | unknown`.
3. `likely_equivalent`는 실행 제외 (`execution_status=filtered_equivalent`).

### 4. 주입·실행 (mutant마다 직렬)

각 mutant에 대해 다음을 반복:

```bash
# 캐시 조회
python3 scripts/session_cache.py --lookup \
    --signature <mutant_signature_hash> \
    --session-id evs_…

# 캐시 miss인 경우만 실행
python3 ~/.claude/skills/test-validity-evaluator/contracts/adapters/python_pytest/run.py \
    --mutant tmp/mutant_042.json \
    --tests <impacted_tests> \
    --timeout 30
```

run.py는 inject.py의 `MutationInjector` 컨텍스트 매니저로 변이 적용 → 테스트 실행 → 결과 분류 → **자동 롤백**까지 수행. 매 mutant 직후 `git status --porcelain` 등으로 작업 트리 무결성 검증.

결과를 `ledger.update(mutant_id, result)`로 기록. `cache_status: hit | miss | invalidated` 필드 갱신.

부분 실패(runner OOM, timeout)는 `degradations[]`에 적재하고 다음 mutant 진행. **runner가 연속 3회 fatal**이면 단계 abort, 부분 결과 반환.

### 5. 진단 (survivor만)

각 살아남은 mutant에 대해 `prompts/survivor-diagnose.md`로 LLM 호출. 출력은 `TestFinding`. 카테고리(`missing_assertion | missing_case | over_mocking | wrong_oracle | weak_equality`)와 함께 **suggested_fix는 컴파일 가능한 테스트 코드 조각** 필수.

같은 누락 원인이 여러 mutant에 의해 드러나면 finding은 **클러스터링하여 하나**로 묶고 `caused_by_mutants[]`에 mutant_id 배열.

### 6. 집계

```bash
python3 scripts/score.py --ledger ledger.json --out metrics.json
```

`mutation_score = killed / (total - filtered_equivalent)`. operator별 분포, 파일별 hotspot 산출. `adapter_summary`에 `adapter_key`, `adapter_version`, capabilities 정보 기입.

## Quality bar

LLM이 들어가는 세 지점(semantic operator, equivalence filter, survivor diagnoser)의 환각·노이즈 차단 불가침 규칙:

1. **증거 인용 필수.** 모든 `TestFinding`은 다음 두 필드를 비워두지 마세요.
   - `evidence.test_snippet` — 해당 테스트 코드(파일·라인 범위).
   - `evidence.mutant_diff` — before/after 스니펫 (3줄 컨텍스트).
   비어 있으면 finding을 만들지 말고 `degradations`에 기록.
2. **결정론 우선.** syntactic operator로 잡히는 변이는 semantic operator로 중복 생성 금지. `(file, span, operator_family)` 키로 중복 제거.
3. **점수 분모를 거짓말하지 않기.** `likely_equivalent`는 점수에서 제외(컨트랙트 보장). 단, ledger에는 항상 보존.
4. **각 단계의 시간·토큰 기록.** ledger의 record마다 `wall_clock_seconds`. semantic operator의 토큰은 별도 메타데이터로.
5. **롤백 검증.** 매 mutant 실행 직후 작업 트리가 깨끗한지 (`git status --porcelain` 빈 출력) 확인. 더러우면 즉시 단계 abort.
6. **부분 결과의 정직성.** 누락이 생기면 `notices`에 카운트와 사유. silent skip 금지.
7. **session 캐시 무효화 보수적으로.** mutant span에 닿은 소스가 한 줄이라도 바뀌면 그 span의 모든 캐시 무효화. 파일 전체 해시는 너무 거침.
8. **롤백 시 file_hash 비교.** inject.py의 backup 파일과 복원된 파일의 SHA-256이 일치하는지 매 mutant 직후 확인.

## Anti-patterns

- **시그니처만 보는 semantic 변이.** 함수 본문·호출 컨텍스트 없으면 의미 위반 변이 불가. 컨텍스트 부족 시 syntactic만 사용.
- **Survivor마다 별도 finding.** 같은 누락이 여러 mutant에서 드러나면 클러스터링 후 `caused_by_mutants[]`로 묶기.
- **Equivalence 판정을 LLM 단독으로 끝낸다.** 가능하면 property/SMT 보강. 안 되면 `unknown`이 "확신 있는 equivalent"보다 안전.
- **추측으로 `killing_tests` 채우기.** runner가 보고한 실패 테스트만. coverage 추정 금지.
- **diff 바깥까지 자동 확장.** 확장은 오케스트레이터의 결정 사항.
- **Findings에 점수만 적고 끝낸다.** 모든 finding은 `suggested_fix`까지 도달.

## Examples

### Python 가격 계산 함수의 할인 조건 누락

대상 함수:

```python
# src/pricing.py
def apply_discount(user, amount):
    if user.tier == 'VIP':
        return amount * 0.85
    return amount * 0.90
```

기존 테스트:

```python
def test_apply_discount_vip():
    result = apply_discount(User(tier='VIP'), 100)
    assert result is not None  # 약한 assertion
```

Semantic 변이 한 건 (ledger 발췌):

```jsonc
{
  "id": "m_042",
  "operator": "semantic",
  "intent": "VIP에서만 적용되는 5% 추가 할인을 STANDARD에도 완화 적용",
  "file": "src/pricing.py", "span": [55, 58],
  "original": "if user.tier == 'VIP':",
  "mutated":  "if user.tier in ('VIP', 'STANDARD'):",
  "equivalence_status": "definitely_distinct",
  "execution_status": "survived",
  "killing_tests": [],
  "cache_status": "miss",
  "diagnosis": {
    "finding_id": "f_a1b2c3",
    "summary": "약한 assertion + STANDARD 등급 케이스 미검증",
    "evidence": "tests/unit/test_pricing.py:18-20"
  }
}
```

진단 결과 `TestFinding`:

```jsonc
{
  "id": "f_a1b2c3",
  "test_id": "tests/unit/test_pricing.py::test_apply_discount_vip",
  "severity": "major",
  "category": "missing_case",
  "evidence": {
    "test_snippet": "tests/unit/test_pricing.py:18-20  assert result is not None",
    "mutant_diff":  "src/pricing.py:55\n- if user.tier == 'VIP':\n+ if user.tier in ('VIP','STANDARD'):"
  },
  "suggested_fix":
    "def test_apply_discount_standard_no_bonus():\n    assert apply_discount(User(tier='STANDARD'), 100) == 90",
  "confidence": 0.82,
  "caused_by_mutants": ["m_042"]
}
```

### 어댑터 capability gap (silent skip 금지)

코드가 `async/await`을 광범위 사용, 어댑터 `supports_async=false` → 해당 영역 변이 생성을 skip하고 `adapter_summary.skipped_due_to_capability += N`, `notices`에 `"async unsupported by adapter python.pytest@0.1.0 — N mutations skipped"`.

## Notes for orchestrator

- 반복 호출은 `EvaluationSession`을 통과(`session_id` 일관). 같은 `FindingId`가 fixed 주장 후 2회+ 재등장하면 오케스트레이터가 `recurrence_blocked`로 종료.
- `FindingId = sha256(test_id || "\n" || category || "\n" || normalized_evidence_signature)[:12]`.
- 사람 환류 `.testvalidity/equivalent_mutants.yml` 매칭 mutant는 generator가 재생성하지 않거나 filter가 `force_equivalent`로 분류.
- HTML 렌더링은 이 스킬이 하지 않음. `test-validity-evaluator` 오케스트레이터가 ledger를 받아 그림.
