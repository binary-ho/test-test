---
name: agentic-adversarial-testing
description: Use when the user wants to find weaknesses INSIDE the tests themselves — weak assertions, tautologies, over-mocking, name-vs-behavior drift, missed edge cases, boundary errors, state/concurrency violations, spec divergence. Runs a fast static critique pass first (4 analyzers), then targeted dynamic attacks (5 generators) using the critique signals as priors. Produces an AdversarialLedger of every attack case and TestFindings with concrete suggested tests.
---

# agentic-adversarial-testing

> **Spec doc (internal)** — 이 문서는 `test-validity-evaluator` 패키지 내부 sub-skill의 설계 사양입니다.
> Claude Code 자동 트리거 대상은 패키지 루트의 `SKILL.md`(`test-validity-evaluator`) 하나뿐. 이 sub-skill은 그 오케스트레이터에서 subprocess로 호출됩니다.
> 직접 실행: `python3 ~/.claude/skills/test-validity-evaluator/scripts/adversarial_orchestrate.py ...`

원본 코드를 흔드는 대신 *테스트 자체*를 공격해 약점·누락을 드러냄. 정적 critique과 동적 adversarial 두 트랙. 모든 공격 케이스(통과/실패/오류/필터링) 전수 보존.

## When to use

- 모든 PR/세션의 fast static gate — critique 트랙을 먼저 돌리면 후속 mutation 노이즈 감소.
- 후속 mutation의 survivor 진단에서 "약한 assertion / 누락 카테고리" 신호가 나옴.
- 정책에서 `adversarial.enabled = true` (mode: light = critique only · normal · max).
- 보안·결제·권한처럼 적대적 입력이 본질인 도메인.

호출하지 말아야 할 경우:

- tier가 unit이 아닌 테스트 (이미 차단).
- `TestSubjectMap.confidence < 0.4`인 테스트가 50%+ — 오케스트레이터가 전체 abort.

## How to invoke

```bash
python3 ~/.claude/skills/test-validity-evaluator/scripts/adversarial_orchestrate.py \
    --phase init \
    --subject-map subject_map.json \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
    --policy policy.json \
    --session-id evs_2026-05-18_a1b2 \
    --out-dir out/adversarial/
# 이후 --phase generate, --phase execute, --phase finalize 차례 호출
```

orchestrate.py는 Skill A와 동일하게 3-phase 모델:

1. `--phase init` — 테스트 파일 컨텐츠와 critique 프롬프트 컨텍스트를 출력.
2. (LLM이 `prompts/critique-*.md`로 critique findings 생성 → `critique_findings.json`)
3. `--phase generate` — critique 결과를 힌트로 adversarial 케이스 컨텍스트 출력.
4. (LLM이 `prompts/adversarial-*.md`로 케이스 생성 → `adversarial_cases.json`)
5. `--phase execute` — 케이스 실행, verdict 분류, ledger 적재.
6. `--phase finalize` — findings 클러스터링 + report.

## Input

```jsonc
{
  "test_subject_map": [ /* TestSubjectMap[] */ ],
  "adapter":          { /* LanguageAdapter */ },
  "policy": {
    "mode": "light" | "normal" | "max",
    "budget": { "max_cases_per_generator": 50, "max_seconds": 300, "max_tokens": 150000 },
    "critique": {
      "enabled_analyzers":        ["assertion","tautology","coupling","behavior_naming"],
      "min_confidence_to_report": 0.6
    },
    "adversarial": {
      "enabled_generators":   ["edge_case","boundary","state","fuzzer","spec_divergence"],
      "execute_dynamic_cases": true,
      "fuzz":  { "framework": "auto", "max_examples": 200, "seed": 1337 },
      "state": { "allow_nondeterminism": false }
    }
  },
  "session": {
    "session_id":        "evs_…",
    "suppressions_yml":  ".testvalidity/suppressions.yml"
  }
}
```

## Output

```jsonc
{
  "red_team_report": {
    "summary": {
      "findings_by_severity": { "critical": N, "major": N, "minor": N, "info": N },
      "findings_by_category": {
        "weak_assertion": N, "tautology": N, "over_coupling": N,
        "name_behavior_mismatch": N, "missed_edge_case": N,
        "off_by_one": N, "state_violation": N,
        "fuzz_counterexample": N, "spec_divergence": N
      },
      "cases_generated": N, "cases_executed": N,
      "cases_caught_by_existing_test": N, "cases_uncaught": N
    },
    "findings":                [ /* TestFinding[] */ ],
    "adversarial_ledger_ref":  "ledger.json",
    "degradations":            [ /* FailureRecord[] */ ],
    "adapter_summary": {
      "adapter_key": "python.pytest",
      "adapter_version": "0.1.0",
      "capabilities_used":    [ ... ],
      "capabilities_missing": [ ... ],
      "skipped_due_to_capability": N
    }
  },
  "adversarial_ledger": { /* AdversarialLedger (전수 보존) */ }
}
```

## Procedure

### Track A — 정적 Critique (4 analyzers, 병렬)

각 analyzer는 `prompts/critique-*.md`의 프롬프트로 LLM 호출. 입력은 테스트 파일 전체 본문. 출력은 `TestFinding[]`.

- `prompts/critique-assertion.md` — 약한 assertion(`is not None`, 빈 try/except), 부동소수점 `==`, 누락된 negative.
- `prompts/critique-tautology.md` — 항상 통과(`assert x == x`), mock 반환을 그대로 assert, 자기 검증.
- `prompts/critique-coupling.md` — 과도한 mock, private API 접근, `call_args` 깊은 조사.
- `prompts/critique-behavior-naming.md` — 이름·docstring과 실제 검증 간 괴리.

`confidence < policy.critique.min_confidence_to_report` 미만은 `severity=info`로 자동 강등.

### Track B — 동적 Adversarial (5 generators, Track A를 힌트로)

각 generator는 `prompts/adversarial-*.md` 프롬프트로 LLM 호출. 입력은 SUT 본문 + Track A의 critique findings(카테고리 빈도). 출력은 `AdversarialCase[]` — 각 케이스는 어댑터의 `runner.run_adversarial_case`로 실행되어 verdict 분류됨.

- `prompts/adversarial-edge-case.md` — null/empty/유니코드/타임존/오버플로우.
- `prompts/adversarial-boundary.md` — 경계 상수 ±1, off-by-one.
- `prompts/adversarial-state.md` — 호출 순서·재진입·idempotency. `state.allow_nondeterminism=false`이면 결정론적 시나리오만.
- `prompts/adversarial-fuzzer.md` — hypothesis/fast-check property + 반례 minimization.
- `prompts/adversarial-spec-divergence.md` — 주석·docstring vs 실제 동작.

#### 표적화 룰 (Track A → Track B 가중치)

| Track A 카테고리 | Track B 가중치 조정 |
|---|---|
| `weak_assertion`, `tautology` | `edge_case` +, `fuzzer` + |
| `name_behavior_mismatch` | `spec_divergence` ++ |
| `over_coupling` | `state` + |
| (없음, 첫 패스) | 균등 + diff 주변 우선 |

### 케이스 실행

orchestrate.py가 각 `AdversarialCase`에 대해:

```bash
python3 ~/.claude/skills/test-validity-evaluator/contracts/adapters/python_pytest/run.py --case case.json
```

run.py는 ad-hoc 테스트 함수를 임시 파일로 렌더링 → pytest 실행 → 결과로 verdict 결정:

- 통과 = `caught_by_existing_test` (SUT가 case.expected와 일치)
- AssertionError 발생 = `uncaught` (SUT가 expected와 다름, 기존 테스트가 못 잡음)
- 그 외 오류 = `inconclusive`

ledger에 모든 케이스 적재. `uncaught`만 finding 후보로 승격.

### Finding 클러스터링

같은 SUT의 동일 약점이 여러 generator에서 나오면 finding 하나에 `caused_by_cases[]`로 묶음. suppressions.yml에 매칭되는 `FindingId`는 `suppressed=true`로 ledger에만 남기고 finding 목록에서 제외.

## Quality bar

1. **증거 인용 필수.** critique은 `evidence.test_snippet`, 동적은 `evidence.case_repr` — 비어 있으면 finding 만들지 말고 `degradations`에 기록.
2. **재현 결정성 게이트.** state-attacker가 만든 비결정 케이스는 `allow_nondeterminism=true`인 경우에도 **3회 연속 동일 verdict**일 때만 finding 승격. 그 외 `verdict=inconclusive`로 ledger에만 남김.
3. **Critique confidence 임계.** `min_confidence_to_report` 미만은 무조건 `severity=info`.
4. **공격 케이스의 적절성도 ledger 항목.** 통과/실패 무관하게 전수 보존.
5. **Suggested fix는 코드 조각.** 추상 문구 금지. 입력값·기대값 포함.
6. **부분 결과 정직성.** generator 실패 시 다른 트랙 계속, `degradations`에 카운트와 사유. silent skip 금지.
7. **Suppression 환류 준수.** 같은 false positive 두 번 보이면 안 됨.

## Anti-patterns

- 모든 테스트를 "약하다"고 단정 (critique LLM의 최빈 실패) — confidence 임계와 evidence 강제로 차단.
- 시그니처만 보고 fuzz — 도메인 의미 사라짐. 시그니처 + 주석/명세 + 호출처까지 컨텍스트 필수.
- State attacker가 멋대로 동시성 시나리오 → flaky finding — 재현성 게이트 없이 금지.
- `spec-divergence-prober`가 docstring을 곧이곧대로 spec으로 — docstring이 거짓일 수 있음. 명세·동작·테스트 3축의 불일치만 보고하고 진실 판단은 사람에게.
- Critique 결과를 동적 트랙에 안 흘려보냄 — 표적화 의미 사라짐.
- Ledger 없이 finding만 — 사람 검토 불가.
- Suppressed finding 재보고 — 도구 신뢰 잠식.

## Examples

### 약한 assertion + 누락 경계 (Python · pytest)

대상 테스트:

```python
def test_apply_discount_returns_a_number():
    assert apply_discount(User(tier="VIP"), 100) is not None
```

- Track A `critique-assertion`: finding `f_001`, `category=weak_assertion`, confidence 0.93, severity major.
- Track A 결과로 `edge_case` 가중치 증가.
- Track B `adversarial-boundary`: 케이스 `c_017` = `(User(tier='VIP'), 0)` → SUT 음수 반환, 기존 테스트 못 잡음.
  - ledger `verdict=uncaught`, finding `f_002`, `category=off_by_one`, severity major.

### 어댑터 capability gap

TS 프로젝트에 `supports_property_based=false`인 어댑터 — `adversarial-fuzzer`는 자동 skip, `notices`에 "fuzzer skipped — adapter capability missing"; critique 트랙은 정상.

## Notes for orchestrator

- 반복 호출은 `EvaluationSession` 통과. 같은 `FindingId`가 fixed 주장 후 2회+ 재등장 → `recurrence_blocked`.
- `FindingId = sha256(test_id || "\n" || category || "\n" || normalized_evidence_signature)[:12]`.
- 사람 환류 `.testvalidity/suppressions.yml` 매칭 finding은 ledger에는 보존, 출력 목록에서 제외.
- HTML 렌더링은 이 스킬이 하지 않음. orchestrator가 ledger와 report를 받아 그림.
