# Test Validity Evaluator — Shared Contracts

> **이 파일은 모든 스킬이 인용하는 단일 source of truth입니다.**
> 각 SKILL.md는 자기가 사용하는 타입을 발췌 인용하고, 상세 셰입은 이 파일을 참조합니다. JSON Schema 사본은 같은 디렉터리의 `*.schema.json`에 있습니다.

---

## 1. 입력 (test target & subject discovery)

### `TestTarget`

```jsonc
{
  "language": "python" | "typescript" | ...,
  "framework": "pytest" | "jest" | ...,
  "source_files": ["src/foo.py", ...],
  "test_files": ["tests/test_foo.py", ...],
  "symbol_map": {                 // optional precomputed import/AST map
    "tests/test_foo.py::test_x": ["src/foo.py:apply_discount"]
  },
  "diff": {                       // optional PR/working-tree diff
    "kind": "unified",
    "patch": "diff --git a/... b/...\n..."
  }
}
```

### `TestTierClassification`

`test-tier-classifier`가 테스트마다 산출. 비유닛 테스트는 파이프라인 진입 전 제외.

```jsonc
{
  "test_file": "tests/test_foo.py",
  "test_id":   "tests/test_foo.py::test_apply_discount_vip",
  "tier": "unit" | "integration" | "e2e" | "unknown",
  "confidence": 0.0..1.0,
  "signals": [
    {
      "kind":     "path_unit" | "path_non_unit" | "db_import" | "driver_import"
                  | "mock_lib" | "framework_marker" | "fixture" | "annotation"
                  | "runtime_slow",
      "weight":   "high" | "medium" | "low" | "categorical" | "overrides",
      "evidence": "tests/test_foo.py:1  import psycopg"
    }
  ],
  "decision": "included" | "excluded" | "needs_review"
}
```

### `TestSubjectMap`

`subject-under-test-locator`가 테스트마다 산출. 두 메소 스킬의 공통 입력.

```jsonc
{
  "test_id": "tests/test_foo.py::test_apply_discount_vip",
  "primary_subjects": [
    {
      "file": "src/pricing.py",
      "symbol": "apply_discount",
      "kind": "function" | "method" | "class",
      "span": [40, 72],            // [start_line, end_line]
      "evidence": "tests/test_foo.py:18  apply_discount(User(tier='VIP'), 100)"
    }
  ],
  "secondary_subjects": [...],
  "mocked_collaborators": [
    { "file": "src/repo.py", "symbol": "Repo.find" }
  ],
  "confidence": 0.0..1.0           // 의무. <0.4 비율이 50%+면 오케스트레이터 abort
}
```

---

## 2. Mutation 트랙

### `Mutant`

```jsonc
{
  "id": "m_042",
  "operator": "AOR" | "ROR" | "COR" | "LCR"
              | "BoundaryShift" | "ConstantReplace"
              | "semantic",
  "file": "src/pricing.py",
  "span": [55, 58],
  "original":  "if user.tier == 'VIP':",
  "mutated":   "if user.tier in ('VIP', 'STANDARD'):",
  "intent":    "VIP 전용 할인 조건을 STANDARD에도 적용하도록 완화",   // semantic만 필수
  "equivalence_status": "definitely_distinct" | "likely_equivalent"
                       | "force_equivalent" | "unknown",
  "mutant_signature_hash": "sha256:..."   // 캐시 키의 일부
}
```

### `MutationResult`

```jsonc
{
  "mutant_id": "m_042",
  "status": "killed" | "survived" | "timeout" | "error"
            | "filtered_equivalent" | "budget_dropped",
  "killing_tests": ["tests/test_pricing.py::test_standard_no_bonus"],
  "wall_clock_seconds": 2.3,
  "cache_status": "hit" | "miss" | "invalidated"
}
```

### `MutationLedger`

모든 변이(필터된 것, budget drop 포함)를 시간순으로 보유.

```jsonc
{
  "generated_at": "2026-05-18T01:23:45Z",
  "tool_versions": { "adapter": "python.pytest@0.1.0", "skill": "0.1.0" },
  "adapter_key": "python.pytest",
  "records": [
    {
      "mutant_id": "m_042",
      "operator": "semantic",
      "file": "src/pricing.py",
      "span": [55, 58],
      "before_code": "...",
      "after_code":  "...",
      "intent": "VIP 전용 할인을 STANDARD에 적용",
      "equivalence_status": "definitely_distinct",
      "execution_status": "survived",
      "killing_tests": [],
      "wall_clock_seconds": 2.3,
      "cache_status": "miss",
      "diagnosis": {              // survivor일 때만
        "finding_id": "f_a1b2c3",
        "summary":    "STANDARD 등급 케이스 미검증",
        "evidence":   "tests/test_pricing.py:18-29"
      },
      "suppressed": false         // .testvalidity/equivalent_mutants.yml 매칭 시 true
    }
  ]
}
```

---

## 3. Adversarial 트랙

### `AdversarialCase`

```jsonc
{
  "case_id": "c_017",
  "generator": "edge_case" | "boundary" | "state" | "fuzzer" | "spec_divergence"
               | "critique:assertion" | "critique:tautology"
               | "critique:coupling"  | "critique:behavior_naming",
  "target_symbol": "src/pricing.py:apply_discount",
  "input_repr": "(User(tier='VIP'), 0)",
  "expected_behavior": "non-negative result",
  "observed_behavior": "-5",
  "verdict": "caught_by_existing_test" | "uncaught" | "inconclusive",
  "finding_id": "f_002"           // verdict=uncaught일 때만
}
```

### `AdversarialLedger`

```jsonc
{
  "generated_at": "2026-05-18T01:23:45Z",
  "tool_versions": { "adapter": "python.pytest@0.1.0", "skill": "0.1.0" },
  "adapter_key": "python.pytest",
  "records": [ /* AdversarialCase[] */ ]
}
```

---

## 4. Findings & 산출 보고서

### `TestFinding`

```jsonc
{
  "id": "f_a1b2c3",                 // = FindingId, 안정 해시
  "test_id": "tests/test_pricing.py::test_apply_discount_vip",
  "severity": "critical" | "major" | "minor" | "info",
  "category": "missing_assertion" | "missing_case" | "over_mocking"
              | "wrong_oracle"      | "weak_equality"
              | "weak_assertion"    | "tautology"        | "over_coupling"
              | "name_behavior_mismatch" | "missed_edge_case"
              | "off_by_one"        | "state_violation"
              | "fuzz_counterexample" | "spec_divergence",
  "evidence": {
    "test_snippet": "tests/test_pricing.py:18-29",   // critique 필수
    "mutant_diff":  "src/pricing.py:55\n- if user.tier == 'VIP':\n+ if ...",  // mutation finding
    "case_repr":    "input=..., expected=..., observed=..."   // adversarial finding
  },
  "suggested_fix": "assert apply_discount(User(tier='STANDARD'), 100) == 90  # 10% base만",
  "confidence": 0.0..1.0,
  "caused_by_mutants": ["m_042"],   // 또는 caused_by_cases
  "caused_by_cases":   ["c_017"],
  "suppressed": false
}
```

#### `FindingId` 계산

`FindingId = sha256(test_id || "\n" || category || "\n" || normalized_evidence_signature)[:12]`

`normalized_evidence_signature`는 evidence를 AST·span 정규화 후 직렬화한 문자열. iteration을 가로질러 "같은 finding"을 같다고 인식하는 안정 식별자.

### `ValidityReport`

```jsonc
{
  "session_id": "evs_…",
  "summary": {
    "mutation_score": 0.0..1.0,
    "finding_counts_by_severity": { "critical": 0, "major": 0, "minor": 0, "info": 0 }
  },
  "test_subject_map_ref":   "./test_subject_map.json",
  "mutation_ledger_ref":    "./ledger/mutation.json",
  "adversarial_ledger_ref": "./ledger/adversarial.json",
  "findings": [ /* TestFinding[] */ ],
  "recommendations": ["..."],
  "scope_summary": {
    "total_tests_examined": 42,
    "included": 31,
    "excluded_by_tier": { "integration": 6, "e2e": 4, "unknown": 0 },
    "excluded_by_confidence": 1,
    "forced_via_annotation": 0
  },
  "adapter_summary": {
    "adapter_key": "python.pytest",
    "adapter_version": "0.1.0",
    "capabilities_used":    ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace","async"],
    "capabilities_missing": ["property_based"],
    "skipped_due_to_capability": 12
  },
  "warnings": ["mutation runner OOM 1회 발생 후 회복"],
  "notices":  ["fuzzer skipped — adapter capability missing (12 cases)"]
}
```

### `FailureRecord` (부분 실패)

```jsonc
{
  "stage": "mutation.runner" | "adversarial.fuzzer" | ...,
  "error_kind": "timeout" | "oom" | "exception" | "adapter_error",
  "detail": "subprocess timed out after 30s on m_042",
  "occurred_at": "2026-05-18T01:24:00Z"
}
```

모든 primitive는 `Result<T, FailureRecord>` 의미로 반환 — 실패해도 메소 스킬 전체가 멈추지 않고, 메소 스킬은 `degradations[]`에 적재 후 다음 단계로 진행.

---

## 5. 어댑터 (언어/프레임워크 의존부)

### `LanguageAdapter`

```jsonc
{
  "key": "python.pytest" | "typescript.jest",
  "language": "python",
  "framework": "pytest",
  "version": "0.1.0",
  "maintainer": "binary-ho",

  "capabilities": {
    "supports_async":           true,
    "supports_property_based":  true,
    "supports_parameterized":   true,
    "supports_parallel_execution": false,
    "syntactic_operators": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"]
  },

  "implementations": {
    "test_discoverer":    "contracts/adapters/python_pytest/discover.py",
    "subject_resolver":   "contracts/adapters/python_pytest/resolve.py",
    "syntactic_operator": "contracts/adapters/python_pytest/mutate.py",
    "injector":           "contracts/adapters/python_pytest/inject.py",
    "runner":             "contracts/adapters/python_pytest/run.py"
  },

  "tier_heuristic_dictionary": {
    "db_imports":      ["psycopg", "sqlalchemy", "pymongo", "redis"],
    "network_imports": ["requests", "httpx", "aiohttp"],
    "driver_imports":  ["selenium", "playwright", "puppeteer"],
    "mock_libs":       ["unittest.mock", "pytest_mock", "responses"]
  }
}
```

오케스트레이터의 어댑터 선택 알고리즘:

1. `TestTarget.language` 감지 (확장자 + `pyproject.toml`/`package.json`/`tsconfig.json`).
2. `framework` 감지 (dev deps + 테스트 파일 import 헤더).
3. `(language, framework)` 정확 매칭 → 어댑터 선택.
4. fallback: `(language, framework=generic)`.
5. miss → `AdapterMissingError`(actionable 메시지: "어떤 매니페스트가 필요한지").

어댑터의 `capabilities`에 없는 기능은 자동 skip하고 `ValidityReport.notices`에 한 줄 기록 — **silent skip 금지**.

---

## 6. 반복 평가 세션

### `EvaluationSession`

```jsonc
{
  "session_id": "evs_2026-05-18_a1b2",
  "started_at": "2026-05-18T01:20:00Z",
  "policy_snapshot": { /* EvaluationPolicy */ },
  "iterations": [ /* IterationRecord[] */ ],
  "status": "in_progress" | "converged" | "terminated",
  "terminal_reason":
      "target_reached"      // converged: score ≥ target AND major+ == 0
    | "wontfix_only"        // converged: 남은 finding 전부 suppressed
    | "max_iterations"      // terminated: 기본 5회
    | "plateau"             // terminated: 연속 2회 major+ 감소 없음 AND score 개선 ≤0.5%p
    | "recurrence_blocked"  // terminated: 같은 finding_id가 fixed 주장 후 2회+ 재등장 ★ 가장 중요
    | "budget_exhausted"    // terminated: 누적 토큰/시간 한도
    | "user_abort"
}
```

### `IterationRecord`

```jsonc
{
  "iteration_index": 0,
  "validity_report_ref": "./iter-0/validity_report.json",
  "delta_vs_previous": {
    "new_findings":       ["f_..."],
    "resolved_findings":  ["f_..."],
    "recurring_findings": ["f_..."],   // ← recurrence_blocked의 시그널
    "score_delta": +0.08
  },
  "cumulative_tokens": 18420,
  "cumulative_seconds": 92.5
}
```

---

## 7. 정책 (policy)

### `EvaluationPolicy` (오케스트레이터가 사용)

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
  "mutation":    { /* MutationPolicy   */ },
  "adversarial": { /* AdversarialPolicy */ }
}
```

### `MutationPolicy` (Skill A가 사용)

```jsonc
{
  "mode": "light" | "normal" | "max",
  "budget": {
    "max_mutants": 200,         // default
    "max_seconds": 600,         // default
    "max_tokens":  200000       // default
  },
  "scope": {
    "diff_only":     true,
    "include_files": ["src/**"],
    "exclude_files": ["**/__generated__/**"]
  },
  "operators": {
    "syntactic_enabled": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
    "semantic_enabled":  true
  },
  "equivalence": {
    "llm_judge":          true,
    "smt_when_available": true,
    "exclude_from_score": "likely_equivalent"
  }
}
```

### `AdversarialPolicy` (Skill B가 사용)

```jsonc
{
  "mode": "light" | "normal" | "max",
  "budget": {
    "max_cases_per_generator": 50,     // default
    "max_seconds": 300,                // default
    "max_tokens":  150000              // default
  },
  "critique": {
    "enabled_analyzers":        ["assertion","tautology","coupling","behavior_naming"],
    "min_confidence_to_report": 0.6
  },
  "adversarial": {
    "enabled_generators": ["edge_case","boundary","state","fuzzer","spec_divergence"],
    "execute_dynamic_cases": true,
    "fuzz": {
      "framework":    "hypothesis" | "fast-check" | "auto",
      "max_examples": 200,           // default
      "seed":         1337           // default
    },
    "state": { "allow_nondeterminism": false }
  }
}
```

---

## 8. 사람 환류 (suppression / equivalent mutants)

### `.testvalidity/suppressions.yml`

```yaml
- finding_id: f_a1b2c3
  reason: "equivalent mutant — pure refactor"
  owner:   jinho.lee16
  expires_at: 2026-08-01
  scope:   this_session | persistent
```

매칭되는 finding은 ledger에는 모두 보존하되 `suppressed=true`로 표시하고 finding 목록에서 제외.

### `.testvalidity/equivalent_mutants.yml`

```yaml
- mutant_signature_hash: sha256:abc123…
  reason:  "behavior-preserving refactor of guard clause"
  owner:   jinho.lee16
```

다음 세션의 generator는 매칭되는 mutant를 **재생성하지 않거나**, equivalence-filter가 `force_equivalent`로 분류.

---

## 9. 한눈에 보는 의존 그래프

```
TestTarget
   │
   ▼
[1a] test-tier-classifier        →  TestTierClassification[]
   │  (included만 통과)
   ▼
[1b] subject-under-test-locator  →  TestSubjectMap (per test)
   │
   ├──────────────────┬──────────────────┐
   ▼                  ▼                  │
[2] Skill A         [3] Skill B          │ (정책에 따라
   (Mutation)         (Adversarial)      │  병렬/순차)
   │                  │                  │
   │ MutationLedger   │ AdversarialLedger│
   │ + findings       │ + findings       │
   ▼                  ▼                  │
[4] aggregator (test-validity-evaluator 내장)
   │
   ▼
ValidityReport (JSON) + report.html
```

LanguageAdapter는 [1a]·[2]·[3]의 모든 언어 의존 작업(parse·inject·run)을 제공.
EvaluationSession은 evaluate→fix→re-evaluate를 가로질러 누적.
