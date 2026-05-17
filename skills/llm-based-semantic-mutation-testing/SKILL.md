---
name: llm-based-semantic-mutation-testing
description: 원본 비즈니스 로직에 결정론적(AST) 변이와 LLM 의미 변이를 주입해, 기존 테스트가 그 변이를 잡아내는지(=민감도) 측정하고 살아남은 변이를 actionable한 TestFinding으로 변환하는 메소 스킬. 모든 변이 이력은 MutationLedger에 전수 보존됩니다.
---

# llm-based-semantic-mutation-testing

> **계층**: Meso (ADR-1 §5 Layer 2 — Skill A)
> **상위 오케스트레이터**: `test-validity-evaluator`
> **하위 primitive**: `mutation-operator-syntactic`, `mutation-operator-semantic`, `mutation-equivalence-filter`, `mutation-injector`, `mutation-test-runner`, `mutation-survivor-diagnoser`, `mutation-score-calculator`, `mutation-ledger-recorder`
> **언어 의존성**: `LanguageAdapter`(ADR-2 §4) 슬롯을 통해 주입·실행을 위임

---

## When to use

오케스트레이터가 다음 조건 중 하나라도 만족할 때 호출합니다.

- 변경(diff)이 **비즈니스 로직 분기**를 포함(if/switch, 가격 계산, 권한 체크, 상태 전이 등).
- `subject-under-test-locator`가 `primary_subjects.confidence ≥ 0.7`인 테스트를 반환.
- 정책에서 `mutation: enabled` (light | normal | max).
- `agentic-adversarial-testing`의 critique 결과 "weak assertion / tautology가 적음" → 정량 신호로 보강이 필요한 경우.

호출하지 말아야 할 경우:

- `test-tier-classifier`가 unit이 아니라고 판정한 테스트 (이미 파이프라인 진입 전 차단됨, 방어적 가드만).
- `TestSubjectMap.primary_subjects`가 비어 있는 테스트(공격할 대상 자체가 불명확).
- 어댑터 선택이 실패한 언어/프레임워크(`AdapterMissingError`).

---

## Input contract

```jsonc
{
  "test_target": TestTarget,             // ADR-1 §4
  "test_subject_map": TestSubjectMap,    // ADR-1 §4, 공유 디스커버리 산출물
  "language_adapter": LanguageAdapter,   // ADR-2 §4, 오케스트레이터가 선택해 주입
  "policy": {
    "mode": "light" | "normal" | "max",
    "budget": {
      "max_mutants": 200,
      "max_seconds": 600,
      "max_tokens": 200000
    },
    "scope": {
      "diff_only": true,                 // diff에 닿은 span만 변이
      "include_files": ["src/**"],
      "exclude_files": ["**/__generated__/**"]
    },
    "operators": {
      "syntactic_enabled": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
      "semantic_enabled": true
    },
    "equivalence": {
      "llm_judge": true,
      "smt_when_available": true,
      "exclude_from_score": "likely_equivalent"   // ADR-1 §9 트레이드오프
    },
    "session": {                                  // ADR-2 §3
      "session_id": "evs_…",
      "cache_enabled": true,
      "equivalent_mutants_yml": ".testvalidity/equivalent_mutants.yml"
    }
  }
}
```

필드 보증:

- `test_subject_map.primary_subjects`만 변이 대상으로 삼습니다. secondary는 ledger에 “out of scope”로만 기록.
- `language_adapter.capabilities`가 지원하지 않는 operator/feature(예: `supports_async=false`)는 자동 skip하되, `ValidityReport.notices`에 한 줄 기록(ADR-2 §4.4 — 사일런트 스킵 금지).

---

## Output contract

```jsonc
{
  "mutation_report": {
    "metrics": {
      "mutation_score": 0.0..1.0,                 // killed / (total - excluded_equivalent)
      "killed": <int>,
      "survived": <int>,
      "timeout": <int>,
      "error": <int>,
      "filtered_equivalent": <int>,
      "by_operator": { "AOR": {...}, "Semantic": {...} },
      "hotspots": [ { "file": "...", "survived": N } ]
    },
    "findings": TestFinding[],                    // ADR-1 §4
    "mutation_ledger_ref": "ledger/mutation.json",
    "degradations": [ FailureRecord ],            // ADR-2 §8
    "adapter_summary": {                          // ADR-2 §4.5
      "adapter_key": "...",
      "adapter_version": "...",
      "capabilities_used": [...],
      "capabilities_missing": [...],
      "skipped_due_to_capability": <int>
    }
  },
  "mutation_ledger": MutationLedger               // 전수 보존, ADR-1 §4
}
```

`MutationLedger.records[]`는 **모든 변이**를 시간순으로 보유합니다. equivalence-filter가 제거한 변이, 실행 직전 cache hit으로 재사용한 변이까지 전부 기록(ADR-2 §6 `cache_status` 포함).

---

## Procedure

각 단계는 정확히 하나의 primitive 호출이며, 단계 산출물은 즉시 `mutation-ledger-recorder`로 흘려보냅니다. **ledger는 단일 sink가 아니라 각 단계가 부르는 기록기**입니다(ADR-1 §5).

### 1. 스코핑

- `test_subject_map.primary_subjects`를 `policy.scope.diff_only`와 교차해 변이 대상 span 집합 `S`를 만듭니다.
- `S`가 비면 즉시 mode=`light`로 강등하고 `notices`에 “no diff overlap, fell back to subject-wide light scope”.

### 2. 변이 생성 (병렬)

`policy.operators`에 따라 두 generator를 병렬 호출. 각 산출 `Mutant`는 즉시 ledger에 `execution_status=null`로 적재.

- `mutation-operator-syntactic`
  - 어댑터의 `implementations.syntactic_operator` 사용.
  - 어댑터 `capabilities.syntactic_operators`에 없는 연산자는 요청에서 제외하고 `capabilities_missing`에 합산.
- `mutation-operator-semantic`
  - LLM이 함수의 **비즈니스 의도**를 추론한 뒤, 그 의도를 미묘하게 위반하는 변이를 제안.
  - `intent` 필드 필수: “이 변이가 노린 결함(예: 할인 적용 조건 누락, 권한 우회)”.

`budget.max_mutants`를 초과하면 우선순위(diff 거리 → semantic > syntactic 균형 → operator 다양성) 기준으로 잘라냅니다. 잘린 후보도 ledger에 `execution_status=budget_dropped`로 보존.

### 3. 등가성 필터

`mutation-equivalence-filter` 한 번 호출. 입력 `Mutant[]`, 출력은 `equivalence_status ∈ {definitely_distinct, likely_equivalent, unknown}` 부착된 동일 배열.

- `likely_equivalent`는 실행하지 않고 ledger에 `execution_status=filtered_equivalent`.
- `policy.equivalence.exclude_from_score`가 `likely_equivalent`이면 점수 분모에서도 제외.
- 사용자 환류(`equivalent_mutants.yml`, ADR-2 §7)에 등록된 mutant_signature는 generator에서 **재생성하지 않거나** filter가 `force_equivalent`로 분류.

### 4. 주입·실행 (mutant마다 직렬)

각 mutant에 대해 다음을 수행합니다.

1. 캐시 조회 — 키 `(mutant_signature_hash, source_revision_hash_per_span, test_set_hash)`(ADR-2 §6).
   - hit: 결과 재사용, ledger에 `cache_status=hit`.
   - miss: 진행.
2. `mutation-injector` → 어댑터의 `implementations.injector`가 worktree/임시 FS에 적용.
3. `mutation-test-runner` → 어댑터의 `implementations.runner`로 영향 받는 테스트 서브셋만 실행. 타임아웃 = `policy.budget.max_seconds / max_mutants` × α.
4. 결과를 `MutationResult`로 분류 후 **반드시 롤백**.
5. ledger에 `execution_status`, `killing_tests[]`, `cache_status=miss|invalidated` 갱신.

부분 실패(예: runner OOM)는 `degradations[]`에 적재하고 다음 mutant로 진행(ADR-2 §8). 단, runner 자체가 연속 3회 fatal이면 단계 abort 후 부분 결과 반환.

### 5. 진단 (survivor만)

`mutation-survivor-diagnoser` 호출. 살아남은 변이마다:

- 왜 잡지 못했는지 LLM 진단 → `TestFinding`.
- 카테고리: `missing_assertion | missing_case | over-mocking | wrong_oracle | weak_equality | …`
- `evidence`에는 반드시 **실제 테스트 코드 라인**과 **변이 전/후 비교 스니펫**을 인용(아래 Quality bar).
- `suggested_fix`에는 그 mutant를 잡을 assertion/케이스 예시(컴파일 가능한 코드 조각).

### 6. 집계

- `mutation-score-calculator` → `metrics`.
- ledger의 모든 레코드에 `generated_at`, `tool_versions`, `adapter_key` 메타데이터 봉인.

### 7. 반환

`mutation_report`와 `mutation_ledger`를 위 Output contract대로 반환. ledger 파일은 동일 산출 폴더에 JSON으로 떨어뜨리고 `mutation_ledger_ref`에 상대경로를 기록.

---

## Quality bar

이 스킬은 LLM이 변이 생성·등가성·진단 세 군데에서 들어옵니다. 환각·노이즈를 막기 위한 **불가침 규칙**:

1. **증거 인용 필수.** 모든 `TestFinding`은 다음 두 필드를 비워둘 수 없습니다.
   - `evidence.test_snippet` — 해당 테스트 코드(파일·라인 범위).
   - `evidence.mutant_diff` — before/after 스니펫 (3줄 컨텍스트).
   이 둘 중 하나라도 없으면 finding을 만들지 말고 `degradations`에 기록.
2. **결정론 우선.** syntactic operator로 잡히는 변이는 semantic operator로 중복 생성하지 않습니다. 동일 `(file, span, operator_family)` 키 중복 제거.
3. **점수 분모를 거짓말하지 않기.** equivalence-filter가 `likely_equivalent`로 분류한 변이를 점수에 포함시키지 않습니다. 단, 그 결정은 ledger에 항상 보존.
4. **각 단계의 시간·토큰을 ledger에 기록.** generator의 토큰, runner의 wall-clock — ROI 분석을 위해(ADR-1 §9).
5. **롤백 검증.** 매 mutant 실행 직후 작업 트리가 깨끗한지 (`git status --porcelain` 등) 확인. 더러우면 즉시 단계 abort.
6. **부분 결과의 정직성.** 어떤 사정으로든 누락이 생기면 `notices` 혹은 `degradations`에 카운트와 사유를 한 줄로 남깁니다. 사일런트 스킵 금지(ADR-2 §4.4).
7. **session 캐시 무효화 보수적으로.** mutant span에 닿은 소스가 한 줄이라도 바뀌면 그 span의 모든 캐시 무효화. 파일 전체 해시는 너무 거침(ADR-2 §6).

---

## Anti-patterns

- **Generator 토큰을 아끼려 함수 시그니처만 보고 semantic 변이를 만든다.** 함수 본문·주변 호출 컨텍스트 없이는 “비즈니스 의도 위반”을 만들 수 없습니다. 컨텍스트가 부족하면 차라리 syntactic으로 폴백.
- **Survivor마다 동일 finding을 따로 만든다.** 같은 누락(예: “null 입력 미검증”)이 여러 mutant에 의해 드러나면 ledger엔 모두 보존하되 finding은 **클러스터링하여 하나**로 묶고 `caused_by_mutants[]`에 mutant_id 배열을 둡니다.
- **Equivalence 판정을 LLM 단독으로 끝낸다.** 가능한 경우 property/SMT 보강을 시도하고, 둘 다 불가하면 `unknown`으로 두는 것이 “자신 있게 equivalent라고 우기는 것”보다 안전합니다.
- **추측으로 `killing_tests`를 채운다.** 실제 runner가 보고한 실패 테스트만 넣습니다. coverage 기반 추정은 금지.
- **diff 바깥까지 자동 확장.** `policy.scope.diff_only=true`이면 절대 확장하지 않습니다. 확장은 오케스트레이터의 결정.
- **Findings에 점수만 적고 끝낸다.** ADR-1 §2 원칙 위반. 모든 finding은 `suggested_fix`까지 도달해야 합니다.

---

## Examples

### Example 1 — Python 가격 계산 함수의 할인 조건 누락

입력 요약:

- `TestTarget.language="python"`, `framework="pytest"`, 어댑터 `python.pytest`.
- `test_subject_map.primary_subjects = [{file:"src/pricing.py", symbol:"apply_discount", span:[40,72], confidence:0.86}]`
- `policy.mode="normal"`, `budget.max_mutants=80`.

생성된 변이 중 한 건(요약):

```jsonc
{
  "id": "m_042",
  "operator": "semantic",
  "intent": "VIP 등급에서만 적용되는 추가 5% 할인을 일반 등급에도 적용하도록 조건을 완화",
  "file": "src/pricing.py",
  "span": [55,58],
  "original": "if user.tier == 'VIP':",
  "mutated":  "if user.tier in ('VIP', 'STANDARD'):"
}
```

실행 후 survivor → 진단 결과 `TestFinding`:

```jsonc
{
  "id": "f_a1b2c3",
  "test_id": "tests/test_pricing.py::test_apply_discount_vip",
  "severity": "major",
  "category": "missing_case",
  "evidence": {
    "test_snippet": "tests/test_pricing.py:18-29",
    "mutant_diff": "src/pricing.py:55  - if user.tier == 'VIP':\n             + if user.tier in ('VIP','STANDARD'):"
  },
  "suggested_fix": "STANDARD 사용자에 대해서도 apply_discount가 0.05 보너스를 주지 않음을 검증하는 케이스를 추가하세요:\n\nassert apply_discount(User(tier='STANDARD'), 100) == 90  # 10% base만",
  "confidence": 0.82,
  "caused_by_mutants": ["m_042"]
}
```

### Example 2 — TypeScript boundary off-by-one (syntactic AOR)

- 어댑터 `typescript.jest`. operator `AOR`로 `<` → `<=` 변이.
- 살아남으면 `category=missing_assertion` 혹은 `weak_equality`로 진단되며, `suggested_fix`는 “`length-1`을 정확히 검증하는 테스트 추가” 등을 코드 조각으로 제시.

### Example 3 — 어댑터 capability gap (사일런트 스킵 금지)

- 코드가 `async/await`을 광범위하게 사용하지만 어댑터가 `supports_async=false`.
- 해당 영역의 mutant 생성 자체를 skip하고 `adapter_summary.skipped_due_to_capability += N`, `notices`에 `"async unsupported by adapter <key> — N mutations skipped"`.

---

## Notes for orchestrator integration

- 반복 호출은 항상 `EvaluationSession`을 통과(ADR-2 §3). 같은 `FindingId`가 “fixed” 주장 후 2회 이상 재등장하면 `recurrence_blocked`로 종료. 이 스킬은 그 판단을 직접 하지 않고, 자신의 `findings`에 안정적인 `FindingId`(=`hash(test_id, category, normalized_evidence_signature)`)만 부여해서 돌려줍니다.
- 사람 환류 산출물(`.testvalidity/equivalent_mutants.yml`)이 존재하면 다음 세션의 generator/filter가 반드시 참조합니다(ADR-2 §7).
- HTML 렌더링은 이 스킬이 하지 않습니다. `html-report-renderer`가 `ValidityReport` + ledger를 받아 그립니다.
