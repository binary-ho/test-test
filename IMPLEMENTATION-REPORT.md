# 구현 보고서 — Test Validity Evaluator: 두 메소 스킬

- 작성일: 2026-05-18
- 작업자: binary-ho
- 컨텍스트 입력: ADR-1 (테스트 유효성 평가 스킬 아키텍처), ADR-2 (반복 평가·어댑터·유닛 스코핑)
- 작업 브랜치: `binary-ho/indianapolis-v5`
- 상태: **명세 완료(specification-complete), 실행 가능 상태 아님(not-yet-runnable)**

---

## 1. 작업 목적

ADR-1 §5 Layer 2가 정의한 두 메소 스킬 — `llm-based-semantic-mutation-testing`, `agentic-adversarial-testing` — 의 `SKILL.md`를 ADR-1 §6 공통 골격에 따라 작성하고, ADR-2의 amend(반복 평가 모델, 어댑터 레지스트리, 유닛 테스트 스코핑)를 모두 반영한다. 또한 두 스킬을 사람이 한 페이지로 이해할 수 있도록 소개용 정적 HTML을 산출한다.

작업 범위는 **두 메소 스킬의 명세 작성**과 **소개 페이지**로 한정. ADR-2 §10의 다음 우선순위(컨트랙트 JSON 스키마, `test-tier-classifier` SKILL.md, `python.pytest` 어댑터 매니페스트, `EvaluationSession` 오케스트레이터 통합)는 본 작업의 범위 외.

---

## 2. 산출물

| 경로 | 라인 | 크기 | 종류 |
|---|---:|---:|---|
| `skills/llm-based-semantic-mutation-testing/SKILL.md` | 260 | 13.7 KB | 명세 |
| `skills/agentic-adversarial-testing/SKILL.md` | 272 | 15.3 KB | 명세 |
| `skills.html` | 926 | 40.1 KB | 사람용 소개 페이지 (자체 포함 단일 파일) |
| `IMPLEMENTATION-REPORT.md` | — | — | 본 보고서 |

산출 총 **1,458 lines**. 외부 의존성 없음.

---

## 3. 산출물 상세

### 3.1 `skills/llm-based-semantic-mutation-testing/SKILL.md`

**책임**: 테스트가 비즈니스 로직 변형을 잡아내는지 검증하고, 사람 검토용 ledger를 함께 산출.

ADR-1 §6 골격을 따른 섹션 구성:

| 섹션 | 핵심 내용 |
|---|---|
| **When to use** | diff가 비즈니스 분기 포함, `primary_subjects.confidence ≥ 0.7`, 정책 `mutation: enabled`일 때 호출. 비유닛·subject 부재·어댑터 부재 시 호출 금지. |
| **Input contract** | `TestTarget`, `TestSubjectMap`, `LanguageAdapter`, `policy{mode, budget, scope, operators, equivalence, session}`. |
| **Output contract** | `mutation_report{metrics, findings, mutation_ledger_ref, degradations, adapter_summary}` + `mutation_ledger`. `adapter_summary`에 `adapter_version`까지 포함. |
| **Procedure** | 7단계: 스코핑 → 변이 생성(병렬) → 등가성 필터 → 주입·실행(직렬, 매 mutant 후 롤백) → 진단(survivor만) → 집계 → 반환. ledger는 각 단계가 부르는 기록기. |
| **Quality bar** | 증거 인용 필수, 결정론 우선, 점수 분모 정직성, 토큰·시간 ROI 기록, 롤백 검증, silent skip 금지, 캐시 무효화는 mutant span 단위. |
| **Anti-patterns** | 시그니처만 보는 semantic 변이, finding 중복 비클러스터링, LLM 단독 등가성 판정, `killing_tests` 추측, diff 외 자동 확장, 점수만 보고. |
| **Examples** | Python 가격 할인 (semantic 변이 → survivor → finding 전체), TS off-by-one (syntactic AOR), 어댑터 capability gap. |
| **Notes for orchestrator integration** | `EvaluationSession`·`FindingId`·사람 환류 산출물·HTML 분리 책임. |

### 3.2 `skills/agentic-adversarial-testing/SKILL.md`

**책임**: 원본 대신 테스트 자체를 공격해 약점·누락을 드러냄.

| 섹션 | 핵심 내용 |
|---|---|
| **When to use** | fast static gate, mutation survivor의 critique 보강, 보안·결제·권한 도메인. |
| **Input contract** | `policy.critique{enabled_analyzers, min_confidence_to_report}` + `policy.adversarial{enabled_generators, execute_dynamic_cases, fuzz{framework, max_examples, seed}, state{allow_nondeterminism}}`. |
| **Output contract** | `red_team_report{summary{by_severity, by_category, case_counts}, findings, adversarial_ledger_ref, degradations, adapter_summary}` + `adversarial_ledger`. |
| **Procedure — Track A (정적 critique)** | 4 analyzer 병렬: `test-assertion-analyzer`, `test-tautology-detector`, `test-coupling-analyzer`, `test-behavior-naming-checker`. confidence 임계 미만은 `info` 강등. |
| **Procedure — Track B (동적 adversarial)** | 5 generator: `edge-case`, `boundary`, `state`, `fuzzer`, `spec-divergence`. Track A 카테고리를 힌트로 가중치 표적화. 케이스마다 verdict 분류, `uncaught`만 finding 후보. |
| **표적화 룰** | weak_assertion·tautology → edge_case·fuzzer +, name_behavior_mismatch → spec_divergence ++, over_coupling → state +. |
| **Quality bar** | 증거 인용, 재현 결정성 게이트(3회 연속 동일 verdict), critique confidence 임계, 케이스 적절성도 ledger, suggested_fix는 코드 조각, silent skip 금지, suppressions 환류. |
| **Anti-patterns** | 모든 테스트를 약하다 단정, 시그니처만 보는 fuzz, 무방비 동시성 시나리오, docstring 맹신, A→B 신호 단절, ledger 없는 finding, suppressed 재보고. |
| **Examples** | 약한 assertion + 누락 경계(A·B 협업), tautology(jest), spec divergence, 어댑터 capability gap. |

### 3.3 `skills.html`

자체 포함된 단일 HTML 파일. CSS·JS 모두 인라인, 외부 CDN·서버 불필요.

기능:

- 다크/라이트 토글 (localStorage 영속화)
- 코드 블록마다 hover 시 표시되는 복사 버튼 (clipboard API + execCommand 폴백)
- sticky nav, 스크롤 시 현재 섹션 하이라이트
- 모바일 반응형 (≤760px 단일 열)
- `<details>`/`<summary>`로 긴 contract 기본 접기

페이지 섹션:

1. Hero — "코드를 흔든다 vs 테스트를 흔든다" 대비
2. 개요 — 두 스킬 카드 비교
3. Skill A 상세 (When/Input/Output/Procedure/Quality bar/예시)
4. Skill B 상세 (동일 + Track A/B 흐름과 표적화 룰)
5. 통합 흐름 ASCII 다이어그램
6. 사용법 (배치 → 호출 → 환류 → 시나리오 3종)
7. 현재 상태와 한계 (§6과 동일 내용 요약)

---

## 4. 의사결정 기록

| # | 결정 | 근거 | 트레이드오프 |
|---|---|---|---|
| D1 | 두 SKILL.md는 ADR-1 §6 공통 골격에 **정확히** 일치하는 헤더 순서 사용 | "일관성이 곧 조합성을 만든다" (ADR-1 §6) | 상위 헤더 두 단계가 고정되어 향후 재배치 자유도 감소 |
| D2 | LLM 사용 지점의 **증거 인용 필수**를 Quality bar 1번에 못박음 | ADR-1 §6 마지막 문단 — 환각이 급감하는 가장 큰 단일 규칙 | 짧은 finding은 evidence가 과도하게 들어가 보일 수 있음 |
| D3 | Ledger 적재 시점을 "각 단계가 부르는 기록기"로 명시 (단일 sink 아님) | ADR-1 §5 — 후보·필터된 변이·budget drop까지 보존해야 사람이 "왜 빠졌나" 확인 가능 | 코드 흐름이 sink 패턴보다 복잡 |
| D4 | 어댑터 capability gap 시 **자동 skip + notices 한 줄 기록** | ADR-2 §4.4 "사일런트 스킵 금지" | 사용자가 직접 보고 라인을 매번 확인해야 함 |
| D5 | `policy.session`을 두 스킬이 공유하되 자기 트랙 키만 노출 | ADR-2 §3, §6, §7이 셋 다 같은 세션 객체를 가정 | 두 스킬의 `policy.session` 셰입이 비대칭으로 보이는 부작용 (보강 후속 작업 필요) |
| D6 | budget/seed/연속 fatal 횟수 등 **임의 상수**를 명시 | ADR이 숫자를 주지 않아 합리적 default를 제시할 필요 | "default"임을 표기했어야 하나 contract 예시 안에 그대로 들어감 (보강 후속) |
| D7 | 메소 스킬 본문에서 `validity-report-aggregator`, `html-report-renderer`는 **호출하지 않음**을 명시 | ADR-1 §3 책임 분리 결정 | 사용자가 결과를 받자마자 HTML이 나오지 않는다는 점을 별도 설명 필요 |
| D8 | 소개 페이지를 **단일 HTML 파일**로 산출 | `html-report-renderer`(ADR-1 §8)의 자체 포함 철학과 일치, CI 아티팩트로 그대로 사용 가능 | 이미지·코드 하이라이팅 없음 (지금은 단색 모노스페이스) |

---

## 5. 검증 결과

### 5.1 검증 방법

세 채널로 교차 검증.

1. **본인 검증** — Claude Skills 골드 스탠다드(`~/.claude/skills/chrome-cdp/SKILL.md`)와 비교, ADR-1·ADR-2 인용 대비 인라인 누락 여부, 디렉터리 산출물 존재 확인.
2. **독립 agent 리뷰 A (엄격 형식 검증)** — "신선한 세션의 모델이 SKILL.md만 읽고 실행 가능한가?" 관점.
3. **독립 agent 리뷰 B (설계 충실도 검증)** — ADR-1·ADR-2 각 절과 SKILL.md 라인의 매핑, 누락·발명·셰입 불일치 검사.

### 5.2 Blocking — 현재 상태로 실행 불가능

| ID | 위치 | 설명 |
|---|---|---|
| B1 | 두 SKILL.md `:3` | `description`이 트리거(when)가 아니라 기능 소개(what). `chrome-cdp`는 "after being asked to inspect…"처럼 트리거 우선. Claude의 description→요청 매칭이 약함. |
| B2 | Skill A `:10`, Skill B `:10–11` | 17개 primitive(`mutation-operator-*`, `test-*-analyzer`, `adversarial-*-generator` 등)를 "호출"한다 명시하지만 호출 매커니즘 0 — sub-skill/script/MCP 어떤 것도 정의 안 됨. `skills/` 하위에 두 디렉터리만 존재 확인. |
| B3 | 두 SKILL.md 전체 | 핵심 컨트랙트(`TestTarget`, `TestSubjectMap`, `TestFinding`, `Mutant`, `MutationLedger`, `AdversarialLedger`, `LanguageAdapter`, `EvaluationSession`, `FindingId`, `FailureRecord`)가 `// ADR-1 §4` 주석으로만 참조. SKILL만 받은 모델은 셰입을 알 수 없음. ADR 참조 24·23건 중 약 절반이 load-bearing. |
| B4 | 디렉터리 구조 | 두 스킬 디렉터리에 `SKILL.md` 한 파일만. `scripts/`·`contracts/`·`prompts/`·`templates/` 모두 부재. 참조된 `contracts/adapters/<key>.yaml`, `.testvalidity/{equivalent_mutants,suppressions}.yml`, `ledger/{mutation,adversarial}.json` 어디에도 정의·생성 안 됨. |
| B5 | Skill B `:165–168` | ADR에 없는 `runner.run_ad_hoc_case(case)` API와 `A-mode`/`B-mode` 분류를 발명. ADR-2 §4.1 `implementations.runner` 슬롯 정의에는 해당 메서드 없음. |

### 5.3 Major — 동작은 하더라도 손상됨

| ID | 위치 | 설명 |
|---|---|---|
| M1 | Skill B `:104–108` | `adapter_summary`에 `adapter_version` 누락. ADR-2 §4.5는 명시 요구. Skill A `:96`에는 있음. |
| M2 | Skill A `:79–104`, Skill B `:85–113` | Ledger 레코드 셰입(Mutation의 `before_code/after_code/intent/cache_status/equivalence_status/execution_status/killing_tests/diagnosis`, Adversarial의 `target_symbol/input_repr/finding_id`)이 본문 산문에는 등장하나 Output contract 블록에는 미열거. |
| M3 | Skill A `:60–64`, Skill B `:67–70` | `policy.session` 셰입이 비대칭(A는 `cache_enabled`+`equivalent_mutants_yml`, B는 `suppressions_yml`만). 같은 세션 객체를 가정하는 ADR-2 §3과 충돌 가능. |
| M4 | 두 SKILL.md | `TestFinding.evidence`의 필드 구조(`test_snippet`, `mutant_diff`, `case_repr`)가 어디에도 스키마로 없음. Quality bar 산문에만 존재 — 환각 방지가 컨트랙트로 보장되지 않음. |
| M5 | 두 SKILL.md | `max_mutants:200`, `max_seconds:600`, `seed:1337`, "연속 3회 fatal" 등 임의 상수가 contract 예시에 그대로 들어감. "default" 표기 없음. |

### 5.4 Minor

- `> **계층**: Meso (ADR-1 §5 Layer 2 — Skill A)` 같은 자기소개 인용 블록은 Claude Skill 모델이 사용하지 않는 메타데이터.
- `ValidityReport.warnings[]` 노출 미정(ADR-2 §8).
- Skill A에 50% confidence 하한 abort 가드 누락(ADR-2 §8 — Skill B는 있음).
- Description 길이가 chrome-cdp 대비 길다.

---

## 6. 한계와 솔직한 평가

두 SKILL.md는 **ADR 충실도 높은 설계 명세 문서**다. ADR-1 §5 Layer 2의 두 메소 스킬을 빠짐없이 옮겼고, ADR-2의 네 가지 amend(반복 평가 모델, 어댑터 레지스트리, 유닛 스코핑, 보조 결정 3종)를 모두 반영했다. 설계 충실도 검증 agent의 결과도 "내부 흐름 일치, Quality bar 일치, suppression·degradation 모두 반영"으로 PASS다.

다만 **실행 가능한 Claude Skill의 형식적 요건**은 만족하지 못한다. 핵심 결함은 §5.2의 B1–B5다. 가장 큰 단일 문제는 17개 primitive의 호출 매커니즘 부재(B2)로, 이것은 두 SKILL.md만의 문제가 아니라 **ADR-1 §5 Layer 1 자체가 아직 Claude Skill 단위가 아니라 시스템 컴포넌트 단위로 분해되어 있기 때문**이다. 메소 스킬을 "잘 만든다"고 해결되지 않는다.

이 결과는 두 가지 경로로 정리할 수 있다.

- **경로 A (실행 가능한 스킬화)** — 두 SKILL.md를 진화시켜 (1) description을 트리거로 재작성, (2) 공유 컨트랙트를 `references/types.md`로 인라인, (3) 17개 primitive를 그 자리에서 LLM 프롬프트 또는 단일 스크립트로 흡수, (4) `adapter_version` 등 누락 필드 보강, (5) 발명 API 정식화 또는 제거.
- **경로 B (명세 문서로 재명명)** — `SKILL.md` → `SPEC.md`로 개명하고 `skills/` → `specs/` 또는 `docs/skills/`로 이동. Claude Skill로서의 의무를 면제받고, 후속 작업에서 별도의 runnable 스킬이 이 명세를 참조하도록 한다.

본 보고서 시점에서는 **경로 A를 권장**한다. 이유:

1. ADR-2 §10의 다음 우선순위가 컨트랙트 JSON 스키마 → `test-tier-classifier` SKILL.md → `python.pytest` 어댑터 순으로 잡혀 있어, 그 과정에서 두 메소 스킬에 필요한 부품이 자연스럽게 만들어진다.
2. Description·primitive 매커니즘은 ADR 변경 없이 SKILL.md 수정만으로 진행 가능.

---

## 7. 후속 작업 권고

ADR-2 §10의 우선순위에 본 보고서의 보강 항목을 끼워 넣은 통합 백로그.

### 7.1 즉시 수정 가능한 보강 (1–2일)

- [ ] **R1** — Skill B `adapter_summary`에 `adapter_version` 추가 (M1, 한 줄).
- [ ] **R2** — 두 SKILL.md의 `description`을 트리거 형태로 재작성 (B1).
- [ ] **R3** — Skill B의 `A-mode`/`B-mode`, `run_ad_hoc_case` 제거 또는 ADR-1·ADR-2 amend로 정식화 (B5).
- [ ] **R4** — `policy.session` 공통 셰입 정의를 한 곳에 두고 두 스킬이 동일 참조 (M3).
- [ ] **R5** — 임의 상수에 "(default)" 표기 추가 (M5).

### 7.2 컨트랙트 정리 (ADR-2 §10 #1)

- [ ] **C1** — `contracts/` 디렉터리 생성, 12개 JSON 스키마 작성:
  - `TestTarget`, `TestSubjectMap`, `Mutant`, `MutationResult`, `TestFinding`, `MutationLedger`, `AdversarialLedger`, `ValidityReport`, `EvaluationSession`, `IterationRecord`, `LanguageAdapter`, `TestTierClassification`.
- [ ] **C2** — 두 SKILL.md의 Input/Output contract 블록에서 `// ADR-1 §4` 주석을 `// see contracts/TestTarget.schema.json`으로 교체.
- [ ] **C3** — Ledger 레코드 셰입을 contract에 인라인하고 두 SKILL.md Output contract에 요약 인용 (M2).
- [ ] **C4** — `TestFinding.evidence` 셰입 스키마화 (M4).

### 7.3 인접 스킬 신설 (ADR-2 §10 #2–#3)

- [ ] **S1** — `skills/test-tier-classifier/SKILL.md` 작성. 본 보고서의 두 스킬보다 단순. tier_heuristic_dictionary는 어댑터가 공급.
- [ ] **S2** — `contracts/adapters/python.pytest.yaml` 매니페스트 + `implementations` 스텁.
- [ ] **S3** — `skills/subject-under-test-locator/SKILL.md`. 두 메소 스킬의 공통 입력 생산.

### 7.4 오케스트레이터 통합 (ADR-2 §10 #4)

- [ ] **O1** — `skills/test-validity-evaluator/SKILL.md` 작성. `EvaluationSession` 종료 조건 6종(target_reached, max_iterations, plateau, recurrence_blocked, wontfix_only, budget_exhausted) 구현.
- [ ] **O2** — `validity-report-aggregator` 책임 위치 결정 (오케스트레이터 내장 vs 별도 primitive).
- [ ] **O3** — `html-report-renderer` 분리 산출 (본 보고서의 `skills.html`은 소개용이고, 실제 ValidityReport 렌더러는 별도).

### 7.5 운영 환류 (ADR-2 §7)

- [ ] **F1** — `.testvalidity/{suppressions,equivalent_mutants}.yml` 스키마 정의.
- [ ] **F2** — HTML 보고서의 `[mark equivalent]` / `[mark wontfix]` / `[annotate fix idea]` 액션 버튼 → YAML 적재 워크플로.

---

## 8. 부록

### 8.1 라인 수 메트릭

```
skills/llm-based-semantic-mutation-testing/SKILL.md   260
skills/agentic-adversarial-testing/SKILL.md           272
skills.html                                           926
                                                    -----
                                                    1458
```

### 8.2 ADR 매핑 (감사 추적)

| ADR-1/ADR-2 절 | Skill A 위치 | Skill B 위치 |
|---|---|---|
| ADR-1 §5 Layer 2 — 책임/입력/출력 | 본문 전체 | 본문 전체 |
| ADR-1 §5 — 내부 flow 다이어그램 | Procedure §1–§7 | Procedure Track A/B |
| ADR-1 §6 — SKILL.md 공통 골격 | 헤더 7개 모두 채움 | 헤더 7개 모두 채움 |
| ADR-1 §6 — 증거 인용 필수 | Quality bar #1 | Quality bar #1 |
| ADR-1 §9 — 등가 변이 트레이드오프 | `policy.equivalence.exclude_from_score` | — |
| ADR-1 §9 — flaky 게이트 | — | Quality bar #2 (3회 연속) |
| ADR-2 §3 — EvaluationSession | Notes for orchestrator | Notes for orchestrator |
| ADR-2 §3.3 — Suppression | Procedure §3, §7 | Procedure §4, Quality bar #7 |
| ADR-2 §4 — LanguageAdapter | Input/Procedure 전반 | Input/Procedure 전반 |
| ADR-2 §4.4 — silent skip 금지 | Quality bar #6 | Quality bar #6 |
| ADR-2 §4.5 — adapter_summary | Output contract `:94–100` ✅ | Output contract `:104–108` ❌ `adapter_version` 누락 |
| ADR-2 §6 — 세션 내 캐시 | Procedure §4-1, Quality bar #7 | — |
| ADR-2 §7 — 사람 피드백 환류 | Procedure §3 (equivalent_mutants.yml) | Procedure §4 (suppressions.yml) |
| ADR-2 §8 — 부분 실패 | `degradations[]` + Quality bar #6 | `degradations[]` + Quality bar #6 |

### 8.3 검증에 사용한 채널

- 본인: Read·Grep·Glob로 ADR 참조·디렉터리 산출물·날조 API 직접 확인.
- Agent A (general-purpose, 엄격 형식 검증): chrome-cdp와의 5개 축 비교, BLOCKING/MAJOR/MINOR 분류.
- Agent B (general-purpose, 설계 충실도 검증): ADR §↔SKILL line 매핑, 발명·누락·셰입 불일치 검사.

세 채널이 모두 §5.2 B1–B5를 독립적으로 식별.

### 8.4 본 작업이 건드리지 않은 것

- ADR-1·ADR-2 자체. amend가 필요한 경우(R3) 별도 ADR 회기에서 처리.
- 17개 primitive SKILL.md.
- `test-validity-evaluator` 오케스트레이터 SKILL.md.
- `contracts/` JSON 스키마.
- 어댑터 매니페스트.
- `.testvalidity/` 환류 산출물.

---

## 9. 결론

ADR-1 §5 Layer 2의 두 메소 스킬을 ADR-1 §6 골격에 맞춰 명세화했고, ADR-2의 amend를 모두 반영했다. 설계 충실도 검증은 PASS. 다만 Claude Skill로서 즉시 실행 가능한 단계까지는 도달하지 못했으며, 그 한계는 본 작업의 범위가 아닌 시스템 분해 자체에 뿌리를 둔다(§6). §7의 후속 작업을 ADR-2 §10 우선순위 안에 통합해 진행할 것을 권한다.
