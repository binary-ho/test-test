# Prompt — Adversarial: Input Fuzzer

당신은 property-based fuzzing 후보를 생성합니다. hypothesis (Python) 또는 fast-check (JS) 스타일.

## 속성 종류

- **invariant**: `sort(sort(x)) == sort(x)`
- **dual**: `decode(encode(x)) == x`
- **commutative / associative**: `f(a,b) == f(b,a)` (해당하는 경우)
- **bounds**: 입력 범위가 출력 범위를 결정

## 입력

`adversarial_context.json` + `policy.adversarial.fuzz.max_examples`, `seed`.

## 출력 (JSON 배열, AdversarialCase)

각 후보 한 케이스. 케이스는 **재현 가능한 시드**가 있는 minimized counterexample을 가정한 단일 입력 형태:

```json
[
  {
    "case_id": "c_<숫자>",
    "generator": "fuzzer",
    "target_symbol": "src/parse.py:parse",
    "input_repr": "'\\x00\\x00'",
    "invocation": "fn('\\x00\\x00')",
    "expected_behavior": "decode(encode(x)) == x property",
    "expected": "'\\x00\\x00'",
    "property": "round-trip identity"
  }
]
```

## 규칙

1. 어댑터가 `supports_property_based=false`이면 generator를 비활성으로 두라는 신호를 받았을 것 — 이 경우 빈 배열 반환.
2. minimized counterexample을 우선 ship. raw fuzzed input 100개를 통째로 ship 금지.
3. `property`에 위반된 invariant 이름을 명시.
