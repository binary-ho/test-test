---
name: agentic-adversarial-testing
description: 원본 코드를 흔드는 대신 "테스트 자체"를 공격해 약점·누락을 드러내는 메소 스킬. 정적 critique(약한 assertion, tautology, 과결합, 이름·행위 괴리)와 동적 adversarial 생성(경계·상태·fuzz·명세 괴리)을 결합해 actionable한 TestFinding과 AdversarialLedger를 산출합니다.
---

# agentic-adversarial-testing

> **계층**: Meso (ADR-1 §5 Layer 2 — Skill B)
> **상위 오케스트레이터**: `test-validity-evaluator`
> **하위 primitive (정적 critique)**: `test-assertion-analyzer`, `test-tautology-detector`, `test-coupling-analyzer`, `test-behavior-naming-checker`
> **하위 primitive (동적 adversarial)**: `adversarial-edge-case-generator`, `adversarial-boundary-explorer`, `adversarial-state-attacker`, `adversarial-input-fuzzer`, `spec-divergence-prober`
> **재사용**: 동적 케이스 실행은 `mutation-test-runner`를 통해 어댑터의 `runner`로 위임(ADR-1 §5).
> **언어 의존성**: `LanguageAdapter`(ADR-2 §4) 슬롯을 통해 실행을 위임

---

## When to use

오케스트레이터가 다음 중 하나일 때 호출합니다.

- 모든 PR/세션의 **fast static gate**로 critique 계열을 먼저 돌릴 때(ADR-1 §5 Layer 3 “권장 내부 순서” 1단계).
- mutation 단계의 survivor 진단에서 “약한 assertion / 누락 카테고리” 신호가 나와 표적화 보강이 필요할 때.
- 정책에서 `adversarial: enabled` (light = critique only | normal | max = critique+all generators).
- 보안·결제·권한처럼 적대적 입력이 본질적으로 중요한 도메인.

호출하지 말아야 할 경우:

- `test-tier-classifier`가 e2e/integration으로 판정한 테스트(이미 차단됨, 방어적 가드만).
- `TestSubjectMap.confidence < 0.4`인 테스트들이 50% 이상 — 오케스트레이터가 전체 abort(ADR-2 §8).

---

## Input contract

```jsonc
{
  "test_target": TestTarget,             // ADR-1 §4
  "test_subject_map": TestSubjectMap,    // ADR-1 §4
  "language_adapter": LanguageAdapter,   // ADR-2 §4
  "policy": {
    "mode": "light" | "normal" | "max",
    "budget": {
      "max_cases_per_generator": 50,
      "max_seconds": 300,
      "max_tokens": 150000
    },
    "critique": {
      "enabled_analyzers": [
        "assertion","tautology","coupling","behavior_naming"
      ],
      "min_confidence_to_report": 0.6   // 미만은 info로 강등
    },
    "adversarial": {
      "enabled_generators": [
        "edge_case","boundary","state","fuzzer","spec_divergence"
      ],
      "execute_dynamic_cases": true,     // false면 케이스 후보까지만 보고
      "fuzz": {
        "framework": "hypothesis|fast-check|auto",
        "max_examples": 200,
        "seed": 1337
      },
      "state": {
        "allow_nondeterminism": false    // ADR-1 §9 flaky 게이트
      }
    },
    "session": {                         // ADR-2 §3
      "session_id": "evs_…",
      "suppressions_yml": ".testvalidity/suppressions.yml"
    }
  }
}
```

필드 보증:

- `test_subject_map.primary_subjects`가 비어 있어도 critique 계열은 **테스트 코드만 보고** 동작 가능합니다(정적). 동적 generator는 primary가 필요.
- 어댑터가 property-based(`supports_property_based=false`)를 못 지원하면 `fuzzer`는 자동 비활성, `notices`에 “fuzzer skipped — adapter capability missing”.
- 어댑터가 비결정적 케이스 실행을 안전히 지원하지 않으면 `state.allow_nondeterminism`은 강제 false.

---

## Output contract

```jsonc
{
  "red_team_report": {
    "summary": {
      "findings_by_severity": { "critical":0, "major":0, "minor":0, "info":0 },
      "findings_by_category": {
        "weak_assertion":0, "tautology":0, "over_coupling":0,
        "name_behavior_mismatch":0, "missed_edge_case":0,
        "off_by_one":0, "state_violation":0, "fuzz_counterexample":0,
        "spec_divergence":0
      },
      "cases_generated": <int>,
      "cases_executed": <int>,
      "cases_caught_by_existing_test": <int>,
      "cases_uncaught": <int>
    },
    "findings": TestFinding[],                  // ADR-1 §4
    "adversarial_ledger_ref": "ledger/adversarial.json",
    "degradations": [ FailureRecord ],          // ADR-2 §8
    "adapter_summary": {                        // ADR-2 §4.5
      "adapter_key": "...",
      "capabilities_used": [...],
      "capabilities_missing": [...],
      "skipped_due_to_capability": <int>
    }
  },
  "adversarial_ledger": AdversarialLedger       // 전수 보존, ADR-1 §4
}
```

`AdversarialLedger.records[]`는 **생성된 모든 공격 케이스**를 보유합니다. 통과/실패/오류/필터링 무엇이든 전부. 사람이 HTML에서 “이 공격이 적절했나”를 검토하기 위함(ADR-1 §5).

---

## Procedure

두 트랙을 병렬 또는 순차로 실행합니다. mode=`light`이면 **A만**, `normal`이면 A→B 직렬(B는 A의 신호를 힌트로), `max`이면 A 끝나는 대로 B의 모든 generator를 병렬 가동.

### Track A — 정적 Critique (병렬, 결정론 우선)

각 analyzer는 독립 호출. 산출 finding은 모두 `evidence.test_snippet` 필수.

1. `test-assertion-analyzer`
   - 약한 assertion 패턴: `assertTrue(result)`, `expect(x).toBeDefined()`, 부동소수점 `==`, 빈 `try/except` 통과.
   - 누락된 negative assertion(성공 케이스만 있고 실패 케이스가 없음).
2. `test-tautology-detector`
   - 항상 통과: `mock.return_value`를 그대로 assert, `assert x == x`.
   - 자기 검증: 테스트가 stub의 출력을 재배치만 해 비교.
3. `test-coupling-analyzer`
   - 과도한 mock(SUT 외부 90% 이상 mock), private API 접근(`_internal`), 구현 세부 의존(`call_args` 깊은 조사).
4. `test-behavior-naming-checker`
   - 테스트 이름/docstring이 약속한 행위와 실제 assertion이 검증하는 행위 간 괴리. LLM 비교.

각 finding은 `confidence ≥ policy.critique.min_confidence_to_report`만 정식 severity로 보고, 미만은 `info`로 강등(ADR-1 §9 false positive 완화).

### Track B — 동적 Adversarial (critique 결과로 표적화)

A에서 나온 카테고리/대상 함수를 **힌트**로 generator의 우선순위·시드를 조정합니다. 예: A가 “경계 누락” 신호를 주면 `adversarial-boundary-explorer`에 가중치 +.

각 generator의 단일 호출 의미와 ledger 적재 규칙:

1. `adversarial-edge-case-generator`
   - 도메인 의미 기반: null/empty/유니코드/타임존/음수/오버플로우/대용량/중복.
   - 입력 예: 비어 있는 컬렉션, NaN, 매우 큰 정수, 미래 시각.
2. `adversarial-boundary-explorer`
   - 코드/명세에서 추출한 경계 상수 주변 ±1, off-by-one 패턴.
3. `adversarial-state-attacker`
   - 호출 순서·재진입·동시성·idempotency 가정 위반.
   - `policy.state.allow_nondeterminism=false`이면 결정론적 시나리오만(예: 정해진 순서의 두 번 호출). 동시성은 후보만 ledger에 적재하고 실행 안 함.
4. `adversarial-input-fuzzer`
   - 어댑터의 property-based 프레임워크(hypothesis/fast-check)로 실행. 반례 발견 시 minimization 결과를 `TestFinding`으로.
5. `spec-divergence-prober`
   - 주석·docstring·README에서 “기대 동작”을 LLM이 추출 → 그에 반하는 입력 후보 제시 → 자동 실행으로 검증.
   - 후보가 실제로 spec 어긋남을 보였고 기존 테스트가 못 잡았다면 `category=spec_divergence`.

### 공통 실행 & ledger 적재 (동적 트랙)

각 케이스에 대해:

1. ledger에 `verdict=null`, `expected_behavior`, `observed_behavior=null`로 선기록.
2. `mutation-test-runner` 재사용 — 원본 코드는 그대로 두고 *추가 테스트 케이스로* 실행. 어댑터의 `runner.run_ad_hoc_case(case)`를 사용.
   - 기존 테스트만으로 케이스가 “이미 잡히는가”를 확인하기 위한 두 가지 모드:
     - **A-mode**: 새 입력으로 SUT만 호출하고 결과를 `expected`와 비교. 기존 테스트는 이 입력에 노출된 적 없음 → 새 케이스가 정당한 누락 신호인지 검증.
     - **B-mode**: 동일 입력에 대해 기존 테스트 스위트 내 “비슷한 테스트”가 통과·실패하는 패턴을 비교.
3. 결과를 `verdict ∈ {caught_by_existing_test, uncaught, inconclusive}`로 분류해 ledger 갱신.
4. `uncaught`만 finding 후보. 단, suppressions.yml에 매칭되는 `FindingId`는 `suppressed=true`로 ledger에만 남기고 finding 목록에는 미포함(ADR-2 §3.3).

### 표적화 룰(B가 A의 신호를 어떻게 쓰는가)

| A에서 나온 카테고리 | B의 가중치 조정 |
|---|---|
| `weak_assertion`, `tautology` | `edge_case` +, `fuzzer` + (어쨌든 실제로 어긋나는 입력을 만들어 finding을 보강) |
| `name_behavior_mismatch` | `spec_divergence` ++ |
| `over_coupling` | `state` + (mock 가정을 깨는 호출 순서) |
| (없음, 첫 패스) | 균등 + diff 주변에 우선순위 |

### Findings 통합

- 같은 SUT의 동일 약점이 여러 generator에서 나오면 **하나의 finding으로 클러스터링**, `caused_by_cases[]`에 case_id 배열.
- 각 finding은 `suggested_fix`에 그 케이스를 잡을 수 있는 테스트 코드 조각을 포함. 컴파일 가능한 형태여야 함.

### 반환

`red_team_report`와 `adversarial_ledger`를 위 Output contract대로 반환. ledger 파일은 동일 산출 폴더에 JSON으로 떨어뜨리고 `adversarial_ledger_ref`에 상대경로 기록.

---

## Quality bar

LLM이 generator·critique·spec-divergence 세 군데에서 들어옵니다. 환각·노이즈 차단을 위한 **불가침 규칙**:

1. **증거 인용 필수.** 모든 `TestFinding`은 둘 중 하나 이상을 채워야 합니다.
   - `evidence.test_snippet` — critique 계열.
   - `evidence.case_repr` — 동적 계열(입력값, 실행 결과, expected 비교 포함).
   비어 있으면 finding을 만들지 말고 `degradations`에 기록.
2. **재현 결정성 게이트.** `adversarial-state-attacker`가 만든 비결정적 케이스는 `policy.state.allow_nondeterminism=true`인 경우에도 **세 번 연속 동일 verdict**일 때만 finding으로 승격. 그렇지 않으면 `verdict=inconclusive`로 ledger에만 남깁니다(ADR-1 §9 flakiness 게이트).
3. **Critique의 confidence 임계.** `policy.critique.min_confidence_to_report` 미만은 무조건 `severity=info`. critique LLM이 자기 확신만으로 major를 매기는 것을 차단.
4. **공격 케이스의 ‘적절성’도 ledger 항목.** generator가 만든 모든 케이스는 통과/실패와 무관하게 ledger에 들어가야 합니다. 사람이 “이 공격이 합리적인가”를 검토할 수 있어야 합니다.
5. **Suggested fix는 그 케이스를 잡는 코드.** “더 견고한 테스트를 작성하세요” 같은 추상 문구 금지. 입력값과 기대값을 가진 코드 조각이 들어가야 합니다.
6. **부분 결과의 정직성.** 어떤 generator가 실패하면 다른 트랙은 계속. `degradations`에 카운트와 사유. 사일런트 스킵 금지(ADR-2 §4.4).
7. **suppressions 환류 준수.** 같은 false positive가 두 번 보이면 안 됩니다(ADR-2 §3.3, §7).

---

## Anti-patterns

- **모든 테스트를 “약하다”고 단정.** critique LLM의 가장 흔한 실패. evidence 인용 + confidence 임계 + info 강등으로 막습니다.
- **Adversarial generator가 SUT 시그니처만 보고 입력을 뿌린다.** 그러면 도메인 의미가 사라져 의미 없는 fuzz가 됩니다. 함수 시그니처 + 주석/명세 + 호출처 한두 곳까지 컨텍스트를 줘야 합니다.
- **State attacker가 멋대로 동시성 시나리오를 만들고 flaky finding을 토해낸다.** §Quality bar #2의 재현성 게이트 없이는 false positive 폭주.
- **`spec-divergence-prober`가 docstring을 곧이곧대로 spec으로 본다.** docstring이 거짓일 수 있습니다. 따라서 “명세 vs 동작 vs 테스트” 세 축의 불일치 패턴을 찾고, 어느 축이 진실인지 결정은 사람에게 미룹니다(severity는 major까지, critical은 금지).
- **Critique 결과를 동적 트랙에 안 흘려보낸다.** mode=`normal`에서 B가 A의 신호를 무시하면 표적화 의미가 사라집니다.
- **Ledger 없이 finding만 낸다.** 사람이 검토할 수 없게 됩니다. ADR-1 §2.5 원칙 위반.
- **suppressed finding을 또 보고한다.** 도구 신뢰를 가장 빠르게 잠식하는 anti-pattern (ADR-2 §3.3).

---

## Examples

### Example 1 — 약한 assertion + 누락된 경계 (Python, pytest)

테스트:

```python
def test_apply_discount_returns_a_number():
    assert apply_discount(User(tier="VIP"), 100) is not None
```

- A (`test-assertion-analyzer`):
  - finding `f_001`, `category=weak_assertion`, `confidence=0.93`, severity `major`.
  - `evidence.test_snippet=tests/test_pricing.py:18-20`.
  - `suggested_fix`: `assert apply_discount(User(tier='VIP'), 100) == 85`.
- B (`adversarial-boundary-explorer`, A의 신호로 가중치 +):
  - 케이스 `c_017`: `(User(tier='VIP'), 0)` → SUT가 음수 반환. 기존 테스트 못 잡음.
  - ledger `verdict=uncaught`, finding `f_002`, `category=off_by_one|edge_case`, severity `major`.

### Example 2 — Tautology (TS, jest)

테스트:

```ts
const mockUser = { id: 1 };
jest.spyOn(repo, "findById").mockReturnValue(mockUser);
expect(service.getUser(1)).toBe(mockUser);
```

- `test-tautology-detector` → finding `category=tautology`, confidence 0.88, severity major.
- `evidence.test_snippet` 인용, `suggested_fix`로 “repo의 ID 변환·캐시 경로를 검증하는 테스트로 분리”.

### Example 3 — Spec divergence (Python, docstring vs 동작)

`docstring`은 “음수 입력 시 ValueError”라고 적었지만 코드는 0을 반환. 기존 테스트는 음수 경로를 다루지 않음.

- `spec-divergence-prober`가 후보 입력 `-1` 제시 → 자동 실행 → 결과가 spec과 다름, 기존 테스트는 통과.
- ledger `c_044 verdict=uncaught`, finding `f_005`, `category=spec_divergence`, severity `major`.
- `suggested_fix`: “`with pytest.raises(ValueError): apply_discount(...)` 케이스 추가 — 단, 사람 검토 필요: 명세가 옳은지 코드가 옳은지 판단 후 한쪽을 정정.”

### Example 4 — 어댑터 capability gap

- TS 프로젝트인데 어댑터의 `supports_property_based=false`. `fuzzer`는 자동 비활성, `notices`에 “fuzzer skipped — adapter capability missing”. critique 트랙은 정상 동작.

---

## Notes for orchestrator integration

- 반복 호출은 항상 `EvaluationSession`을 통과(ADR-2 §3). 동일 `FindingId`가 “fixed” 주장 후 2회 이상 재등장하면 `recurrence_blocked`로 종료. 이 스킬은 그 판단을 직접 하지 않고, `FindingId = hash(test_id, category, normalized_evidence_signature)`를 안정적으로 부여해 돌려줍니다.
- 사람 환류 산출물(`.testvalidity/suppressions.yml`)이 존재하면 ledger에는 모두 보존하되, finding 목록에서는 `suppressed=true` 항목을 제외합니다(ADR-2 §3.3, §7).
- mutation 트랙과 묶여 호출될 때, 자기 critique 결과의 카테고리·대상 함수를 오케스트레이터가 mutation 측 “survivor 진단 힌트”로 전달할 수 있도록 `summary.findings_by_category`를 안정적인 키로 노출합니다.
- HTML 렌더링은 이 스킬이 하지 않습니다. `html-report-renderer`가 `ValidityReport` + ledger를 받아 그립니다.
