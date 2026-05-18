---
name: subject-under-test-locator
description: Use AFTER test-tier-classifier and BEFORE the mutation or adversarial skills. For each included unit test, identify which business-logic symbol(s) it actually exercises. Combines deterministic AST analysis (imports, calls, mock decorators) with an LLM pass that reads assertions, test name, and docstring to disambiguate. Output is TestSubjectMap[] — the common input both meso skills require.
---

# subject-under-test-locator

테스트가 *증명*하려 하는 비즈니스 로직 위치를 추론. 이후 모든 메소 스킬의 공통 입력을 생산하므로, 여기서 잘못 잡으면 파이프라인 전체가 오염됩니다.

## When to use

- `test-tier-classifier`가 `decision="included"`를 반환한 직후.
- 단일 테스트에 대한 디버깅 — 어떤 함수가 진짜 SUT인지 알고 싶을 때.

## How to invoke (두 단계: 결정론 → LLM 보강)

### Step 1 — 결정론적 후보 추출

어댑터의 `subject_resolver`를 셸 호출.

```bash
python3 contracts/adapters/python_pytest/resolve.py \
    --test-file tests/unit/test_pricing.py \
    --test-id   "tests/unit/test_pricing.py::test_apply_discount_vip"
```

출력 예:

```jsonc
{
  "test_id": "tests/unit/test_pricing.py::test_apply_discount_vip",
  "primary_subjects": [
    { "file": "src/pricing.py", "symbol": "apply_discount", "kind": "function",
      "span": [18, 21], "evidence": "tests/unit/test_pricing.py:18" }
  ],
  "mocked_collaborators": [],
  "confidence": 0.55
}
```

### Step 2 — LLM 보강

Step 1의 confidence가 < 0.7이거나 primary_subjects가 비어 있으면, 다음 프롬프트로 LLM을 호출. 그 외에는 Step 1 결과를 그대로 채택.

프롬프트 템플릿은 `prompts/locate.md`에 있습니다. LLM 호출 시 `{{...}}` 자리에 값을 채워 넣습니다.

LLM 출력은 동일한 `TestSubjectMap` 셰입의 JSON이어야 하며, `evidence` 필드에 반드시 인용된 코드 라인을 포함해야 합니다. 인용 없는 응답은 반려.

## Input

- `test_file` (path)
- `test_id` (pytest nodeid)
- (선택) `language_adapter` — Step 1 의 resolver 경로

## Output — `TestSubjectMap`

`contracts/types.md` §1 참조.

```jsonc
{
  "test_id": "...",
  "primary_subjects": [
    { "file": "src/...", "symbol": "...", "kind": "function|method|class",
      "span": [start, end], "evidence": "test_file:line  quoted_code" }
  ],
  "secondary_subjects": [...],
  "mocked_collaborators": [...],
  "confidence": 0..1
}
```

`confidence` 의무 필드. 오케스트레이터는 confidence < 0.4인 테스트가 50% 이상이면 전체 abort.

## Quality bar

- 모든 subject는 `evidence` 필드에 **테스트 코드 라인 인용** 필수. `apply_discount(...)` 같은 호출 또는 `result.X` 형태의 assertion 주어.
- mocked 된 collaborator는 절대 primary로 넣지 않음 — mock된 것은 SUT가 아님.
- confidence는 보수적으로. "아마도"는 0.4–0.6, "확실히"는 0.8+.
- LLM 출력이 결정론적 단계와 모순되면 **결정론적 단계를 신뢰**. LLM은 보강이지 덮어쓰기가 아님.

## Anti-patterns

- 테스트 이름의 명사만 보고 subject를 결정 — 이름은 거짓말할 수 있음.
- import된 모든 심볼을 primary로 — fixture·헬퍼·테스트 자체 유틸까지 들어감.
- mock이 사용되었다고 collaborator를 무조건 secondary로 — mock 외에도 partial mock·spy가 있음.

## Examples

`tests/unit/test_pricing.py:18`:

```python
def test_apply_discount_vip():
    result = apply_discount(User(tier='VIP'), 100)
    assert result == 85
```

기대 출력:

```jsonc
{
  "test_id": "tests/unit/test_pricing.py::test_apply_discount_vip",
  "primary_subjects": [
    {
      "file": "src/pricing.py", "symbol": "apply_discount", "kind": "function",
      "span": [40, 72],
      "evidence": "tests/unit/test_pricing.py:19  result = apply_discount(User(tier='VIP'), 100)"
    }
  ],
  "secondary_subjects": [
    { "file": "src/user.py", "symbol": "User", "kind": "class",
      "evidence": "tests/unit/test_pricing.py:19  User(tier='VIP')" }
  ],
  "mocked_collaborators": [],
  "confidence": 0.86
}
```
