# ADR-1: 테스트 유효성 평가 스킬 아키텍처

- 상태: Accepted
- 날짜: 2026-05-17
- 컨텍스트 입력: 설계 v1, 설계 v2 (요청 7건 반영본)
- 결정 범위: "테스트 작성 에이전트"가 생산한 테스트의 품질을 외부에서 검증하기 위한 Claude Skills 집합의 분해·계약·오케스트레이션

---

## 1. 컨텍스트 (왜 만드는가)

테스트 작성 에이전트가 생성한 테스트는 "통과한다"는 신호만으로 품질을 보증할 수 없습니다. 다음 두 축의 외부 검증이 필요합니다.

- **Mutation Testing** — 원본 코드를 미세하게 흔들었을 때 테스트가 잡아내는지(=테스트의 민감도).
- **Red Teaming** — 테스트 코드 자체를 흔들어 약점·누락을 드러내는지(=테스트의 견고성).

두 축은 상호 보완적이므로 동일한 오케스트레이터 아래에 두되, primitive 단위로 최대한 쪼개어 조합·교체가 자유롭도록 설계합니다.

---

## 2. 설계 원칙

1. **Atomic skills + contract-driven composition.** 각 스킬은 단일 책임을 가지며, 입력/출력 스키마가 명확합니다. 상위 오케스트레이터가 컨트랙트만 보고 자유롭게 조합합니다.
2. **Deterministic-first, LLM-second.** AST 기반 syntactic mutation처럼 결정론적으로 가능한 것은 결정론적으로 처리하고, LLM은 의미·맥락 판단(semantic mutation, equivalence 판정, critique)에만 사용합니다. 비용·재현성·안정성에 직접 영향을 줍니다.
3. **Findings는 항상 actionable.** 모든 출력은 "어떤 테스트의 어떤 약점이고, 어떻게 고치는가"까지 도달합니다. 점수만 내뱉는 스킬은 만들지 않습니다.
4. **Budget-aware.** Mutation testing은 비용이 폭증하기 쉬워, 모든 생성 스킬에 `budget`(개수·시간) 파라미터를 강제합니다.
5. **Human-inspectable by default.** 변이/공격 케이스는 결과 외에 *모든* 이력을 ledger로 남기고, 사람이 한 페이지로 검토할 수 있도록 HTML 보고서를 산출합니다.

---

## 3. 결정 사항 요약 (v1 → v2 변경점)

설계 v1을 기반으로 다음 네 가지 변경을 확정합니다.

- **공유 디스커버리 스텝 신설.** 최상단에 `subject-under-test-locator`를 두고, 이후 모든 스킬은 그 산출물(`TestSubjectMap`)을 공통 입력으로 받습니다. 메소 스킬 내부의 중복 디스커버리를 제거합니다.
- **메소 계층을 두 개의 이름 있는 스킬로 고정.** `llm-based-semantic-mutation-testing`, `agentic-adversarial-testing` 둘만 존재합니다.
- **추적 책임을 별도 primitive로 분리.** `mutation-ledger-recorder`(+ `AdversarialLedger`)가 생성·필터·실행·진단의 각 지점에서 *모든* 이력을 기록합니다. 사람 검수 가능성이 일급 기능입니다.
- **렌더링 책임을 별도 primitive로 분리.** `html-report-renderer`가 보고서 한 파일을 생성합니다. 보고서 양식을 바꿔도 분석 코드는 영향받지 않습니다.

---

## 4. 공유 데이터 컨트랙트

모든 스킬이 합의하는 핵심 타입을 `contracts/` 폴더에 JSON 스키마로 둡니다.

```
TestTarget        { language, framework, source_files[], test_files[], symbol_map }

TestSubjectMap {
  test_id,
  primary_subjects: [
    { file, symbol, kind: function|method|class, span, evidence }
  ],
  secondary_subjects: [...],
  mocked_collaborators: [...],
  confidence
}

Mutant            { id, operator, file, span, original, mutated, intent, equivalence_status }
MutationResult    { mutant_id, status: killed|survived|timeout|error, killing_tests[] }

MutationLedger {
  records: [
    {
      mutant_id, operator, file, span,
      before_code, after_code,
      intent,                          // LLM이 추론한 "이 변이가 노린 결함"
      equivalence_status,
      execution_status: killed|survived|timeout|error|filtered_equivalent,
      killing_tests[],
      diagnosis?                       // survivor일 때만
    }
  ],
  generated_at, tool_versions
}

AdversarialLedger {
  records: [
    {
      case_id, generator, target_symbol,
      input_repr, expected_behavior, observed_behavior,
      verdict: caught_by_existing_test|uncaught|inconclusive,
      finding_id?
    }
  ]
}

TestFinding       { id, test_id, severity, category, evidence, suggested_fix, confidence }

ValidityReport {
  summary { mutation_score, finding_counts_by_severity, ... },
  test_subject_map_ref,
  mutation_ledger_ref,
  adversarial_ledger_ref,
  findings: TestFinding[],
  recommendations: []
}
```

`*_ref`는 동일 산출 폴더 내 JSON 파일 경로입니다. HTML 렌더러는 `ValidityReport` 하나만 받아 ledger 파일을 읽어 모든 섹션을 그립니다.

---

## 5. 스킬 분해 (3계층)

### Layer 1 — Primitive Skills (단일 책임)

**공유 디스커버리**
- `subject-under-test-locator` — 테스트 코드가 *증명*하는 원본 비즈니스 로직 위치를 파악. import 추적 + mock되지 않은 호출 분석 + assertion 주어(subject) 추론 + 테스트 이름/docstring 정합성을 결합. 출력은 후속 모든 스킬의 입력.

**Mutation generation**
- `mutation-operator-syntactic` — AST 기반의 결정론적 변이(AOR, ROR, COR, LCR, 경계값, 상수 치환 등).
- `mutation-operator-semantic` — LLM이 "이 함수가 의도하는 비즈니스 규칙"을 추론한 뒤 그 의도를 미묘하게 위반하는 변이를 생성. 단순 문법 변이가 잡지 못하는 누락(예: 할인 적용 조건, 권한 체크 우회)을 노림.
- `mutation-equivalence-filter` — 행위적 등가성 판정. LLM 판정 + 가능한 경우 SMT/property 확인.

**Mutation execution**
- `mutation-injector` — `Mutant`를 실제 소스에 적용/롤백. Git worktree 또는 임시 파일시스템 사용.
- `mutation-test-runner` — 주입된 변이에 대해 테스트 스위트를 실행하고 `MutationResult`로 분류. 타임아웃·언어별 러너 추상화 포함.

**Mutation analysis**
- `mutation-survivor-diagnoser` — 살아남은 변이마다 "왜 잡지 못했는가"를 LLM이 진단해 `TestFinding`(누락된 assertion, 누락된 케이스, 잘못된 mock 등)으로 변환.
- `mutation-score-calculator` — score, operator별 분포, 파일별 hotspot 산출.

**Mutation 추적 (신설)**
- `mutation-ledger-recorder` — *모든* 변이(killed/survived/equivalent/error/filtered)에 대해 before/after, operator, intent, 결과, 진단을 시간순으로 정규화된 레코드로 적재. 필터링·삭제 없이 전부 보존. 파이프라인의 sink로서 generation 직후 후보, equivalence-filter가 제거한 변이까지 모두 기록.

**Red team — static critique** (정적, 테스트 코드만 봄)
- `test-assertion-analyzer` — assertion의 강도/구체성. `assertTrue(result)` 같은 두루뭉술한 검증, 누락된 negative assertion, 부동소수점 비교 오류 패턴 탐지.
- `test-tautology-detector` — 항상 통과하는 테스트, 자기 자신을 검증하는 테스트(예: mock 반환값을 그대로 assert), 무의미한 try/except 감지.
- `test-coupling-analyzer` — 구현 결합도. 과도한 mock, private API 접근, 구현 세부에 의존하는 패턴.
- `test-behavior-naming-checker` — 테스트 이름·docstring이 약속하는 행위와 실제 검증하는 행위 간 괴리 탐지(LLM 비교).

**Red team — dynamic adversarial** (동적, 소스+테스트 모두 봄)
- `adversarial-edge-case-generator` — 도메인 의미 기반 극단 케이스 생성(null/empty/유니코드/시간대/음수/오버플로우 등).
- `adversarial-boundary-explorer` — 명시적 경계 주변 ±1 / off-by-one 케이스.
- `adversarial-state-attacker` — 호출 순서·재진입·동시성·idempotency 가정 위반 시나리오.
- `adversarial-input-fuzzer` — property-based fuzzing(hypothesis/fast-check 등). 반례를 `TestFinding`으로.
- `spec-divergence-prober` — 명세(주석·docstring·README)와 실제 동작이 다르고 테스트가 못 잡는 케이스를 LLM이 후보로 제시 → 자동 실행으로 검증.

**Reporting**
- `validity-report-aggregator` — finding dedupe/우선순위/클러스터링만.
- `html-report-renderer` (신설) — 집계된 보고서 + ledger를 자체 포함 정적 HTML 한 파일로 렌더링. CSS/JS 인라인, syntax highlighting, 섹션별 접기/필터링/상태별 색상.

### Layer 2 — Meso Skills (정확히 두 개)

#### Skill A — `llm-based-semantic-mutation-testing`

- **책임**: 테스트가 비즈니스 로직 변형을 잡아내는지 검증하고, 사람 검토용 ledger를 함께 산출.
- **입력**: `TestTarget`, `TestSubjectMap`, `policy`
- **출력**: `MutationReport` (= 점수 메트릭 + findings + ledger 참조), `MutationLedger`

내부 흐름 (각 화살표는 primitive 1개 호출):

```
TestSubjectMap (이미 주어짐)
   │
   ▼ primary_subjects만 범위로 좁힘
mutation-operator-syntactic ──┐
                              ├──► Mutant[]
mutation-operator-semantic ───┘
   │
   ▼
mutation-equivalence-filter   ──► Mutant[] (equivalence_status 부착)
   │
   ▼ 각 mutant마다
mutation-injector → mutation-test-runner → (롤백)
   │
   ▼
MutationResult[]
   │
   ├─► mutation-survivor-diagnoser ──► TestFinding[]
   ├─► mutation-score-calculator   ──► metrics
   └─► mutation-ledger-recorder    ──► MutationLedger (전수)
```

`mutation-ledger-recorder`는 단일 sink가 아니라 각 단계의 산출물을 받아 *기록만* 하는 컴포넌트로 동작합니다. generation 직후 후보 변이도, equivalence-filter가 제거한 변이도 모두 ledger에 들어가야 사람이 "왜 이 변이는 빠졌나"까지 확인 가능합니다.

#### Skill B — `agentic-adversarial-testing`

- **책임**: 테스트 자체를 공격해 약점·누락을 찾음.
- **입력**: `TestTarget`, `TestSubjectMap`, `policy`
- **출력**: `RedTeamReport` (= findings + 적용 케이스 ledger)

내부 흐름:

```
TestSubjectMap
   │
   ├─► critique 계열 (병렬, 정적)
   │     test-assertion-analyzer
   │     test-tautology-detector
   │     test-coupling-analyzer
   │     test-behavior-naming-checker
   │
   ├─► adversarial 계열 (동적, critique 결과를 힌트로 표적화)
   │     adversarial-edge-case-generator
   │     adversarial-boundary-explorer
   │     adversarial-state-attacker
   │     adversarial-input-fuzzer
   │     spec-divergence-prober
   │     ※ 각 generator의 출력 케이스는 mutation-test-runner를 재사용해 실행 가능
   │
   ▼
TestFinding[] + AdversarialLedger
```

`AdversarialLedger`로 생성된 모든 공격 케이스(통과/실패/오류)를 기록해, HTML에서 사람이 "이 공격이 적절했나"를 검토할 수 있게 합니다.

### Layer 3 — Orchestrator

#### `test-validity-evaluator` (top-level, 단일 진입점)

```
입력: TestTarget, diff, policy
   │
   ▼
[1] subject-under-test-locator
      → TestSubjectMap   ★ 이후 모든 단계의 공통 입력
   │
   ├──────────────┬────────────────────┐
   ▼              ▼                    │
[2] llm-based-   [3] agentic-          │ (정책에 따라 병렬/순차)
   semantic-       adversarial-
   mutation-       testing
   testing
   │              │
   │ Mutation     │ RedTeam
   │ Report +     │ Report +
   │ Ledger       │ Ledger
   ▼              ▼
[4] validity-report-aggregator
      → ValidityReport  (dedupe·우선순위·클러스터링)
   │
   ▼
[5] html-report-renderer
      → report.html (자체 포함)
   │
   ▼
출력: ValidityReport (JSON) + report.html (사람용)
```

오케스트레이터의 세 가지 책임:

- **Triage.** 입력된 변경(예: PR diff)을 보고 어느 메소 스킬을 어느 강도로 돌릴지 결정. 작은 변경 → critique + mutation light. 복잡한 비즈니스 로직 → semantic mutation + adversarial 강화. 안전 민감(결제/권한) → 모두 max.
- **Routing & budget.** 각 메소에 시간·토큰 예산을 분배. 한 메소가 폭주해도 전체가 멈추지 않도록 부분 결과를 받습니다.
- **Aggregation.** 여러 출처의 `TestFinding`을 dedupe·우선순위화·중복 원인 클러스터링하여 단일 `ValidityReport`로 반환.

권장 내부 순서 — "빠른 신호로 비싼 단계를 가이드":

1. **Fast static gate** — `agentic-adversarial-testing`의 critique 계열 먼저. 명백한 약점(tautology, weak assertion)을 먼저 잡아 후속 단계의 노이즈 감소.
2. **Mutation pipeline** — diff 주변에 한해 syntactic부터, 남은 예산으로 semantic. equivalence filter가 거른 후 실행.
3. **Adversarial dynamic** — survivor diagnose에서 나온 약점 카테고리를 힌트로 표적화 실행(예: "경계 누락" 신호 → boundary explorer 강하게).
4. **Aggregation & render** — finding 머지, source-level hotspot 도출, 추천 액션 생성, HTML 렌더.

---

## 6. SKILL.md 공통 골격

모든 스킬의 `SKILL.md`는 동일한 골격을 따릅니다. 일관성이 곧 조합성을 만듭니다.

```
# <skill-name>

## When to use
<오케스트레이터가 이 스킬을 부를 트리거 조건>

## Input contract
<공유 컨트랙트 중 어떤 타입을 받는지, 추가 파라미터>

## Output contract
<반환 타입과 필드별 보증 조건>

## Procedure
1. ... (단계별, 도구 호출 포함)
2. ...

## Quality bar
- <false positive 최소화 규칙>
- <증거 없이 finding 만들지 않기 등>

## Anti-patterns
- <이 스킬에서 흔히 발생하는 실수>

## Examples
- 입력·출력 예시 한두 건
```

특히 LLM이 들어가는 스킬(`mutation-operator-semantic`, critique 계열, `mutation-survivor-diagnoser`)은 **"증거 인용 필수"** 규칙을 `Quality bar`에 명시합니다. finding의 evidence 필드에 코드 스니펫·라인 번호를 반드시 채우게 강제하면 환각이 급감합니다.

---

## 7. 상위 에이전트와의 통합

테스트 작성 에이전트 입장에서는 사실상 도구가 하나입니다.

```
tool: evaluate_test_validity
  - 호출 시점: 테스트를 작성/수정한 직후
  - 입력: 변경된 파일들, 정책
  - 출력: ValidityReport (+ report.html)
  - 후속 행동: severity ≥ major인 finding에 대해 테스트 보강 →
              재호출 (regression 방지)
```

내부적으로 더 미세한 제어가 필요한 advanced 에이전트라면 메소 스킬을 직접 부를 수도 있게 두지만, **기본 진입점은 단일하게 유지**하는 것이 운영상 안전합니다.

테스트 보강 루프는 자연스럽게 `evaluate → fix → re-evaluate`로 수렴해야 하며, 오케스트레이터는 finding마다 `fix_hint`(예: "이 mutant를 잡는 assertion 예시")를 포함해 상위 에이전트가 곧장 패치를 만들 수 있게 합니다. 사람 리뷰어는 `report.html`로 변이/공격이 적절했는지 함께 확인합니다.

---

## 8. HTML 보고서 구성 (렌더러가 그리는 것)

1. **Summary 카드** — mutation score, severity별 finding 개수, 진단별 hotspot 함수 top N
2. **Subject-Under-Test 매핑 테이블** — 테스트 ↔ 원본 함수, mock 여부, confidence
3. **Mutation Ledger** — 변이별 행, before/after side-by-side(syntax highlight), operator/intent 배지, 상태별 색상, "왜 살아남았는가" 진단 펼치기, 필터(operator·status·file)
4. **Adversarial Ledger** — 공격 케이스 표, generator·target·verdict
5. **Findings** — 카테고리·심각도별 그룹, evidence 코드 인용, suggested_fix
6. **Recommendations**

자체 포함된 단일 HTML 파일로 만들어 CI 아티팩트나 PR 코멘트 첨부로 그대로 사용 가능합니다.

---

## 9. 트레이드오프와 위험

- **비용.** Semantic mutation + adversarial fuzzing은 LLM 호출량이 큽니다. budget 강제와 diff-only 스코핑이 없으면 운영 불가. 처음부터 메트릭(스킬별 토큰·시간·발견 건수)을 수집해 ROI 낮은 스킬은 비활성화할 수 있게 합니다.
- **Critique의 false positive.** LLM이 모든 테스트를 "약하다"고 단정하기 쉽습니다. (1) critique 스킬은 항상 evidence 코드 인용 필수. (2) confidence 점수를 내고 임계값 미만은 `info`로 강등.
- **Equivalent mutant.** 영원한 난제. 100% 자동 해결은 불가능하므로 "likely_equivalent"는 별도 트랙으로 보고만 하고 score 분모에서 제외하는 옵션을 둡니다(ledger에는 모두 보존).
- **Flakiness.** `adversarial-state-attacker`가 만들어내는 동시성/순서 케이스는 flaky 테스트를 만들 수 있습니다. survivor를 통한 finding은 "재현 절차"가 결정론적일 때만 채택하도록 게이트가 필요합니다.
- **언어 의존성.** `mutation-injector`, `mutation-test-runner`는 언어별 어댑터가 필요합니다. 첫 버전은 1~2개 언어(Python/TypeScript 등 주요 스택)로 시작하고, 어댑터 인터페이스만 표준화합니다.
- **Subject 추론 오류.** `subject-under-test-locator`가 잘못된 대상을 잡으면 이후 전 파이프라인이 오염됩니다. `confidence`를 의무 필드로 두고, 낮은 신뢰도 테스트는 오케스트레이터가 syntactic-only 경량 모드로 라우팅합니다.

---

## 10. 확장성

이 설계대로 가면, 새로운 변이 연산자나 adversarial 전략을 추가할 때 다른 스킬을 건드리지 않고 primitive 하나만 추가하면 됩니다. ledger와 렌더러도 분리되어 있어, 보고서 양식 변경이 분석 코드에 영향을 주지 않습니다. 그게 "최대한 쪼개는" 분해의 실익입니다.

---

## 11. Open Questions / 다음 단계

다음 작업 항목 중 하나를 먼저 구체화합니다.

- (a) `subject-under-test-locator`의 `SKILL.md`를 먼저 잡는다 — 후속 모든 스킬의 입력 컨트랙트(`TestSubjectMap`)가 잠깁니다.
- (b) `test-validity-evaluator`의 `SKILL.md`를 먼저 잡는다 — 전체 오케스트레이션의 단계별 호출·예산 정책이 잠깁니다.
- (c) `contracts/`의 JSON 스키마를 먼저 둔다 — 모든 SKILL.md가 같은 단어로 말하기 시작합니다.

권장: (c) → (a) → (b) 순. 컨트랙트가 가장 변경 비용이 큽니다.
