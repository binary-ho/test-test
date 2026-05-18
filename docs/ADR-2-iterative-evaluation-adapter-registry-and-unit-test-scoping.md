# ADR-2: 반복 평가 모델, 어댑터 레지스트리, 유닛 테스트 스코핑

- 상태: Accepted
- 날짜: 2026-05-18
- 관계: ADR-1을 amend (§4 컨트랙트, §5 primitive, §7 통합 흐름, §9 위험)
- 컨텍스트 입력: ADR-1 리뷰, 사용 모드 재정의(가끔 돌리는 도구)

---

## 1. 컨텍스트 변경

ADR-1은 사용 빈도를 명시하지 않았습니다. 운영 가정을 다음과 같이 고정합니다.

- **사용 모드**: CI에서 PR마다 자동 실행하는 게이트가 아니라, 개발자가 "지금 한번 돌려본다" 식으로 가끔 호출하는 분석 도구.
- 영향:
  - baseline/regression 추적, 글로벌 메트릭 영속화는 ROI 낮음 → out of scope.
  - 사람 in the loop가 일급 시민. **사람 피드백 환류**와 **iteration 수렴 가드**가 더 중요해짐.
  - 세션 내(evaluate → fix → re-evaluate) 효율은 여전히 중요.

이 컨텍스트 변경을 반영하여 다음 결정을 추가합니다.

---

## 2. 결정 요약

1. **반복 평가 모델 (`EvaluationSession`)** — evaluate → fix → re-evaluate 루프의 무한 반복을 막는 종료 조건과 suppression 환류.
2. **어댑터 레지스트리 (`LanguageAdapter`)** — 언어/프레임워크 단위로 primitive 묶음을 1급 컨트랙트로 등록·선택.
3. **유닛 테스트 스코핑 (`test-tier-classifier`)** — 파이프라인 진입 전에 비유닛 테스트를 걸러내고, 그 결정을 명시적으로 보고.
4. **(보조) 세션 내 캐시 / 부분 실패 처리 / 사람 피드백 환류** — 가끔 돌리지만 한 세션 안에서는 효율과 신뢰가 중요.

---

## 3. 결정: 반복 평가 모델

### 3.1 컨트랙트

```
EvaluationSession {
  session_id, started_at,
  policy_snapshot,
  iterations: IterationRecord[],
  status: in_progress | converged | terminated,
  terminal_reason?:
    target_reached | plateau | max_iterations | budget_exhausted |
    wontfix_only | recurrence_blocked | user_abort
}

IterationRecord {
  iteration_index,
  validity_report_ref,
  delta_vs_previous {
    new_findings:        FindingId[],
    resolved_findings:   FindingId[],
    recurring_findings:  FindingId[],   // ★ 같은 finding이 다시 나타남
    score_delta
  },
  cumulative_tokens, cumulative_seconds
}

FindingId = hash(test_id, category, normalized_evidence_signature)
```

`FindingId`는 iteration을 가로질러 같은 finding을 같다고 인식하는 안정 식별자. evidence는 AST·span 정규화 후 해시.

### 3.2 종료 조건 (먼저 만나는 것 적용)

| 조건 | 정의 | 상태 |
|---|---|---|
| `target_reached` | mutation_score ≥ target **AND** severity≥major == 0 | converged |
| `max_iterations` | 기본 5회 | terminated |
| `plateau` | 연속 2회 동안 major+ 감소 없음 **AND** score 개선 ≤ 0.5%p | terminated |
| `recurrence_blocked` | 같은 finding_id가 "fixed" 주장 후 2회 이상 재등장 | terminated |
| `wontfix_only` | 남은 finding이 전부 suppressed | converged |
| `budget_exhausted` | 누적 토큰/시간 한도 초과 | terminated |

`recurrence_blocked`가 가장 중요한 가드. 무한 루프의 가장 흔한 형태는 "에이전트가 고쳤다고 우기는데 실제로는 안 고친 상태" — finding_id가 같은 채로 재등장하는 것이 시그널.

### 3.3 Suppression

`.testvalidity/suppressions.yml`:

```yaml
- finding_id: f_a1b2c3
  reason: "equivalent mutant — pure refactor"
  owner: jinho.lee16
  expires_at: 2026-08-01
  scope: this_session | persistent
```

ledger에는 사라지지 않고 `suppressed=true`로 항상 표시. 가끔 돌리는 도구의 신뢰는 "같은 false positive를 다시 안 보여주는 것"에서 옴.

### 3.4 ADR-1과의 관계

- §4 컨트랙트에 `EvaluationSession`, `IterationRecord`, `FindingId` 추가.
- §7 상위 에이전트 통합: `evaluate → fix → re-evaluate` 루프는 **반드시** `EvaluationSession`을 통해 호출. 단발 호출은 1-iteration 세션의 특수 경우.

---

## 4. 결정: 어댑터 레지스트리

### 4.1 컨트랙트

```
LanguageAdapter {
  key: "python.pytest" | "typescript.jest" | ...,
  language, framework, version, maintainer,

  capabilities {
    supports_async, supports_property_based,
    supports_parameterized, supports_parallel_execution,
    syntactic_operators: [AOR, ROR, COR, LCR, BoundaryShift, ...]
  },

  implementations {
    test_discoverer,    // 테스트 파일·케이스 enumerate
    subject_resolver,   // import/AST 해석기
    syntactic_operator, // 언어별 AST 변이
    injector,           // 파일 적용/롤백
    runner              // 케이스 선택, 타임아웃
  },

  tier_heuristic_dictionary {  // §5에서 사용
    db_imports:      [...],
    network_imports: [...],
    driver_imports:  [...],
    mock_libs:       [...]
  }
}
```

**(language, framework) 쌍이 같은 어댑터로 묶여야 한다**는 것이 핵심. injector만 Python인데 runner가 generic이면 일관성이 깨짐.

### 4.2 매니페스트와 로딩

- 위치: `contracts/adapters/<key>.yaml`
- 오케스트레이터 시작 시 디렉터리 스캔 → 레지스트리 로딩.
- 첫 번째 어댑터: `python.pytest`, `typescript.jest`.

### 4.3 선택 알고리즘

```
1. TestTarget 언어 감지
   - 파일 확장자 + 프로젝트 파일 (pyproject.toml, package.json, tsconfig.json)
2. 프레임워크 감지
   - dev deps + 테스트 파일 import 헤더
3. 정확 매칭 (language, framework) → 어댑터 선택
4. fallback: (language, framework=generic)
5. miss → AdapterMissingError (어떤 매니페스트가 필요한지 actionable 메시지)
```

### 4.4 Capability 게이팅

어댑터가 `supports_async=false`인데 코드가 async를 포함하면, 해당 영역의 mutation을 자동 skip하고 `ValidityReport.notices`에 "async unsupported — 12 mutations skipped"를 한 줄 남김. 사일런트 스킵 금지.

### 4.5 ValidityReport 추가

```
adapter_summary {
  adapter_key, adapter_version,
  capabilities_used:     [],
  capabilities_missing:  [],
  skipped_due_to_capability: <count>
}
```

### 4.6 ADR-1과의 관계

- §5 Layer 1의 `mutation-injector`, `mutation-test-runner`, `mutation-operator-syntactic`은 어댑터의 `implementations` 슬롯을 채우는 역할로 재정의.
- §9의 "언어 의존성" 항목은 이 결정으로 해소.

---

## 5. 결정: 유닛 테스트 스코핑

### 5.1 새 primitive: `test-tier-classifier`

`subject-under-test-locator` **앞**에 위치. 비유닛 테스트는 처음부터 파이프라인에 안 들어감.

### 5.2 컨트랙트

```
TestTierClassification {
  test_file, test_id,
  tier: unit | integration | e2e | unknown,
  confidence: 0..1,
  signals: [
    { kind, weight, evidence }
  ],
  decision: included | excluded | needs_review
}
```

### 5.3 신호와 가중치

| 신호 | 방향 | 가중치 |
|---|---|---|
| 경로 컨벤션 (+): `tests/unit/`, `__tests__/`, `*.unit.*`, `*_test.py` | +unit | high |
| 경로 컨벤션 (−): `tests/integration/`, `tests/e2e/`, `cypress/`, `playwright/` | −unit | high (categorical) |
| DB/네트워크 import (어댑터 사전): psycopg, sequelize, requests, axios, supertest | −unit | high |
| 드라이버 import: selenium, playwright, puppeteer | −unit | categorical → e2e |
| mock 라이브러리 사용: unittest.mock, jest.mock, vi.mock, sinon | +unit | medium |
| 프레임워크 마커: `@pytest.mark.integration`, jest project 분리 | categorical | overrides |
| fixture: `tmp_path` 허용, `live_server` / `docker_container` → −unit | mixed |
| 명시적 주석: `# @validity:tier=unit`, `# @validity:skip` | categorical | overrides all |
| (옵션) 과거 실행 시간 > 1초 | −unit | low |

### 5.4 라우팅 정책

```
tier == unit AND confidence ≥ 0.7    → included
tier == unit AND confidence < 0.7    → needs_review (사용자 프롬프트 or skip)
tier ∈ {integration, e2e}            → excluded (이유 기록)
tier == unknown                      → excluded (경고)
명시적 주석                            → 가장 높은 우선순위
```

### 5.5 ValidityReport 추가

```
scope_summary {
  total_tests_examined,
  included,
  excluded_by_tier: { integration, e2e, unknown },
  excluded_by_confidence,
  forced_via_annotation
}
```

### 5.6 어댑터 의존

DB 클라이언트·드라이버·mock 라이브러리 목록은 언어마다 다름 → `LanguageAdapter.tier_heuristic_dictionary`가 제공. 어댑터 미존재 언어는 경로 컨벤션만으로 판정(confidence 자연 하락 → needs_review로 흐름).

### 5.7 ADR-1과의 관계

- §5 Layer 1에 `test-tier-classifier` 추가.
- §7 통합 흐름의 [1] 단계가 둘로 분리됨:
  - [1a] `test-tier-classifier` → `TestTierClassification[]`
  - [1b] `subject-under-test-locator` (included 테스트만) → `TestSubjectMap`

업데이트된 [1]–[2] 구간:

```
입력: TestTarget, diff, policy
   │
   ▼
[1a] test-tier-classifier
       → 비유닛 제외, scope_summary 산출
   │
   ▼ included만 통과
[1b] subject-under-test-locator
       → TestSubjectMap
   │
   ├──────────────┬────────────────────┐
   ▼              ▼                    │
[2] llm-based-   [3] agentic-          │
   semantic-       adversarial-
   mutation-       testing
   testing
   ...
```

---

## 6. 결정 (보조): 세션 내 캐시

- 키: `(mutant_signature_hash, source_revision_hash_per_span, test_set_hash)`
- 범위: 하나의 `EvaluationSession` 내부만.
- 의도: iteration N에서 새 테스트 1개만 추가됐을 때, 변하지 않은 mutant·test 조합 결과를 재사용 → 비용을 `×iteration_count`에서 `×Δ`로 줄임.
- `source_revision_hash`는 **mutant span 단위**로 잘게 끊어 저장(파일 전체 해시는 너무 거침; 작은 수정이 전체를 무효화하면 캐시 무의미).

ADR-1 §4 `MutationLedger.records[]`에 `cache_status: hit | miss | invalidated` 필드 추가.

---

## 7. 결정 (보조): 사람 피드백 환류

HTML 리포트의 액션 버튼:

- `[mark equivalent]` → `.testvalidity/equivalent_mutants.yml`에 mutant_id 기록
- `[mark wontfix]` → `.testvalidity/suppressions.yml`에 finding_id 기록
- `[annotate fix idea]` → 자유 텍스트, ledger의 해당 레코드에 첨부

다음 세션 동작:

- `equivalent_mutants.yml`에 있는 mutant는 generator가 **재생성하지 않거나**, equivalence-filter가 **force_equivalent**로 분류.
- suppressions.yml의 finding_id는 표시되되 `suppressed=true`로 게이트.

가끔 돌리는 도구에서 이 환류가 없으면 사람은 매번 같은 false positive를 본다 → 도구 신뢰가 빠르게 잠식.

ADR-1 §5 `html-report-renderer`의 출력 책임에 "액션 버튼 + 폼 export" 추가.

---

## 8. 결정 (보조): 부분 실패 처리

- 모든 primitive 반환: `Result<T, FailureRecord>`.
- 메소 스킬은 실패를 `degradations[]`에 기록하고 나머지로 진행.
- 오케스트레이터 abort 조건은 **단 하나**:
  - `subject-under-test-locator`가 입력 테스트의 50% 이상에서 `confidence < 0.4` → 이후 단계가 의미 없으므로 전체 abort.
- 그 외는 모두 부분 결과로 진행, `ValidityReport.warnings[]`에 "X 단계 실패, Y개 분석 누락"으로 정직하게 표시.

---

## 9. ADR-1 amend 요약

| ADR-1 섹션 | 변경 |
|---|---|
| §4 컨트랙트 | `EvaluationSession`, `IterationRecord`, `FindingId`, `LanguageAdapter`, `TestTierClassification` 추가. `MutationLedger.records[]`에 `cache_status` 추가. `ValidityReport`에 `adapter_summary`, `scope_summary`, `warnings`, `notices` 추가. |
| §5 Layer 1 | `test-tier-classifier` primitive 추가. `mutation-injector`/`runner`/`syntactic-operator`는 어댑터 슬롯으로 재정의. |
| §5 Layer 3 | 오케스트레이터가 `EvaluationSession`을 1급으로 다룸. |
| §7 통합 흐름 | [1] 단계를 [1a] tier-classifier + [1b] subject-locator로 분리. 반복 호출은 EvaluationSession을 통과. |
| §9 위험 | "언어 의존성"은 §4(어댑터)로 해소. "Critique false positive"는 §7(피드백 환류)로 완화. |

---

## 10. 우선순위 권고

1. 컨트랙트 JSON 스키마 — `EvaluationSession`, `LanguageAdapter`, `TestTierClassification`를 ADR-1 기존 컨트랙트와 함께 한 번에 정리.
2. `test-tier-classifier`의 `SKILL.md` — 가장 단순하고 즉시 효과(파이프라인 입력 줄이는 게 가장 큰 비용 절감).
3. `python.pytest` 어댑터 매니페스트 + 기본 implementations.
4. `EvaluationSession` 오케스트레이터 통합 (수렴 가드 포함).

ADR-1의 다음 단계 (a)/(b)/(c) 중 (c) 컨트랙트 → (a) subject-locator SKILL.md 순서를 따르되, 그 사이에 (1) tier-classifier SKILL.md를 끼우는 것을 권장.
