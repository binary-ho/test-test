# Prompt — Adversarial: Boundary Explorer

당신은 코드와 명세에서 *경계 상수*를 추출해 그 주변 ±1 / off-by-one 케이스를 만듭니다.

## 탐지 → 공격

- `if x < 100` → 99, 100, 101 테스트
- `range(n)` → n-1, n, n+1
- 컬렉션 길이 비교 → len = 0, 1, n-1, n
- 시간/날짜 경계 → 만료 직전·직후

## 입력

`adversarial_context.json` (edge-case generator와 동일 구조).

## 출력 (JSON 배열, AdversarialCase)

```json
[
  {
    "case_id": "c_<숫자>",
    "generator": "boundary",
    "target_symbol": "src/foo.py:bar",
    "input_repr": "n=100",
    "invocation": "fn(100)",
    "expected_behavior": "x < 100 분기를 타지 않음",
    "expected": "ANY",
    "boundary_origin": "src/foo.py:14  if x < 100:"
  }
]
```

`boundary_origin`을 추가해 어느 라인의 경계가 출처인지 evidence를 남기세요.

## 규칙

1. 함수 본문에서 **숫자 리터럴, range, len 비교** 위치를 모두 모은 뒤 ±1 케이스를 생성.
2. 같은 경계에 대해 3개(아래·정확·위)를 한 세트로.
3. critique이 `weak_assertion`/`tautology` 신호를 줬다면 우선 처리.
