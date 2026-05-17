# Prompt — Adversarial: Spec-Divergence Prober

당신은 **명세(주석·docstring·README)와 실제 동작이 다르고 테스트가 그 차이를 못 잡는** 케이스를 찾습니다.

## 절차

1. 함수의 docstring/주석에서 약속된 행위를 추출 ("should raise ValueError on negative input").
2. 실제 함수 본문이 그 약속을 지키는지 분석.
3. 어긋난다면, 어긋남이 드러나는 입력을 후보 케이스로 제시.

## 출력 (JSON 배열, AdversarialCase)

```json
[
  {
    "case_id": "c_<숫자>",
    "generator": "spec_divergence",
    "target_symbol": "src/pricing.py:apply_discount",
    "input_repr": "(-1)",
    "invocation": "fn(-1)",
    "expected_behavior": "docstring: 'raises ValueError on negative input'",
    "expected": "ANY",
    "spec_source": "src/pricing.py:38  '''Raises ValueError if amount < 0'''",
    "actual_behavior_summary": "함수는 0을 반환함 (docstring 위반)"
  }
]
```

## 규칙

1. **docstring/주석/README 어느 한 축**에 출처를 두세요. 위치를 인용.
2. **명세·동작·테스트 세 축의 불일치**임을 보여야 finding. 명세가 옳은지 코드가 옳은지는 판단하지 마세요 — 사용자에게 미룹니다. 따라서 severity는 **major까지만**, critical 금지.
3. docstring을 "곧이곧대로 진실"로 취급 금지. docstring이 거짓일 가능성도 evidence로 함께 기록.
