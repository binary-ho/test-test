---
name: test-tier-classifier
description: Use BEFORE any test-validity work to filter out non-unit tests. Given a list of test files, classifies each as unit / integration / e2e / unknown via path conventions, imports, fixtures, decorators, and annotations. Run this whenever the user wants to evaluate test quality, mutation-score a test suite, or scope a validity pipeline — integration/e2e tests must be excluded before the mutation or adversarial trakcs start.
---

# test-tier-classifier

> **Spec doc (internal)** — 이 문서는 `test-validity-evaluator` 패키지 내부 sub-skill의 설계 사양입니다.
> Claude Code 자동 트리거 대상은 패키지 루트의 `SKILL.md`(`test-validity-evaluator`) 하나뿐. 이 sub-skill은 그 오케스트레이터에서 subprocess로 호출됩니다.
> 직접 실행: `python3 ~/.claude/skills/test-validity-evaluator/scripts/classify.py ...`

신호 가중 분류기로 각 테스트의 tier를 결정. 비유닛 테스트를 파이프라인 진입 전에 걸러내는 것이 핵심.

## When to use

- 사용자가 "이 프로젝트의 테스트 품질을 보고 싶다" / "mutation testing을 돌려달라"고 했을 때 **가장 먼저**.
- `test-validity-evaluator` 오케스트레이터가 시작 시 자동 호출 (사용자가 직접 부를 일은 거의 없음).

## How to invoke

이 스킬은 한 줄의 셸 호출로 끝납니다. LLM이 절차를 시뮬레이션할 필요 없음.

```bash
python3 ~/.claude/skills/test-validity-evaluator/scripts/classify.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
    --out tier_classification.json
```

출력은 `TestTierClassification[]`(아래 컨트랙트 참조). 후속 스킬은 `decision == "included"`인 항목만 사용합니다.

## Input

```jsonc
{
  "root": "프로젝트 루트 디렉터리",
  "adapter": "어댑터 매니페스트 yaml — tier_heuristic_dictionary를 제공",
  "test_files": ["옵션 — 명시적 파일 목록. 비우면 root에서 자동 발견"]
}
```

## Output — `TestTierClassification[]`

자세한 셰입은 `contracts/types.md` §1 참조. 각 레코드:

```jsonc
{
  "test_file": "tests/test_foo.py",
  "test_id":   "tests/test_foo.py::test_apply_discount_vip",
  "tier":      "unit" | "integration" | "e2e" | "unknown",
  "confidence": 0.0..1.0,
  "signals":   [ { "kind", "weight", "evidence" } ],
  "decision":  "included" | "excluded" | "needs_review"
}
```

## Routing rules (스크립트 내장)

| 조건 | decision |
|---|---|
| 명시적 주석 `# @validity:skip` | excluded (override) |
| 명시적 주석 `# @validity:tier=unit` | included (override) |
| tier=unit ∧ confidence ≥ 0.7 | included |
| tier=unit ∧ confidence < 0.7 | needs_review |
| tier∈{integration,e2e} | excluded |
| tier=unknown | excluded (warn) |

## Quality bar

- 모든 분류 결정은 `signals[]`에 적어도 한 개의 인용 가능한 evidence를 동반해야 합니다(파일:라인).
- `needs_review`는 사용자에게 보여주고 한 번 더 묻는 것이 원칙. 자동 included 금지.
- 어댑터에 없는 언어/프레임워크의 경우 분류기는 경로 컨벤션만으로 동작 — confidence가 자연 하락하여 `needs_review`로 흐름.

## Anti-patterns

- mock 사용을 unit의 충분조건으로 보기 — mock은 medium 가중. 단독으로 unit 결정 금지.
- "TODO: integration"이라는 주석을 신호로 사용 — 오탐 천국.
- 과거 실행 시간을 hard threshold로 사용 — flaky에 취약. low 가중까지만.

## Examples

```bash
$ python3 ~/.claude/skills/test-validity-evaluator/scripts/classify.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml
```

```jsonc
[
  {
    "test_file": "tests/unit/test_pricing.py",
    "test_id":   "tests/unit/test_pricing.py::test_apply_discount_vip",
    "tier": "unit", "confidence": 0.92,
    "signals": [
      { "kind": "path_unit",      "weight": "high",   "evidence": "tests/unit/test_pricing.py" },
      { "kind": "mock_lib",       "weight": "medium", "evidence": "tests/unit/test_pricing.py:3  from unittest.mock import patch" }
    ],
    "decision": "included"
  },
  {
    "test_file": "tests/integration/test_db.py",
    "test_id":   "tests/integration/test_db.py::test_connect",
    "tier": "integration", "confidence": 0.95,
    "signals": [
      { "kind": "path_non_unit", "weight": "high", "evidence": "tests/integration/test_db.py" },
      { "kind": "db_import",     "weight": "high", "evidence": "tests/integration/test_db.py:2  import psycopg" }
    ],
    "decision": "excluded"
  }
]
```
