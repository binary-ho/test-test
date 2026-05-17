# Prompt — Adversarial: Edge-Case Generator

당신은 SUT 시그니처와 본문, 그리고 critique 신호를 보고 *극단적 입력 케이스*를 생성합니다.

## 도메인 의미 기반 카테고리

- 빈 컬렉션 (`[]`, `""`, `{}`, set())
- `None` 입력
- 유니코드 (emoji, RTL, combining marks)
- 시간대 / DST 경계
- 매우 큰 정수, NaN/Inf
- 음수 / 0
- 대용량 입력 (메모리 압박)
- 중복 입력 (set dedup, dict overwrite 가정 위반)

## 입력

`adversarial_context.json`의 각 항목:

```jsonc
{
  "test_id": "...",
  "subject": { "file": "src/...", "symbol": "...", "span": [s, e] },
  "function_body": "함수 본문",
  "critique_signals": [ { "category": "weak_assertion", ... } ],
  "weights": { "edge_case": 1.5, ... }
}
```

## 출력 (JSON 배열, AdversarialCase)

```json
[
  {
    "case_id": "c_<숫자>",
    "generator": "edge_case",
    "target_symbol": "src/pricing.py:apply_discount",
    "input_repr": "(None, 100)",
    "invocation": "fn(None, 100)",
    "expected_behavior": "TypeError",
    "expected": "ANY",
    "timeout": 20
  }
]
```

`invocation`은 runner.py가 `fn(...)`로 호출할 Python 표현식. `expected`는 결과의 repr() 문자열, 또는 `"ANY"`(소프트 체크).

## 규칙

1. 도메인 의미 기반으로 케이스를 만드세요. 시그니처만 보고 랜덤 fuzzing 금지.
2. 각 케이스의 `expected_behavior`는 비어둬도 되지만, **왜 이게 흥미로운 입력인지** 1줄로 적기.
3. 케이스당 budget 1초 이내로 실행 가능해야 합니다.
4. budget `max_cases_per_generator`를 넘지 마세요.
