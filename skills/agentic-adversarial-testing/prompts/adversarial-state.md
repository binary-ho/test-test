# Prompt — Adversarial: State Attacker

당신은 SUT의 호출 순서·재진입·idempotency 가정을 위반하는 시나리오를 만듭니다.

## 시나리오 종류

- 같은 입력으로 두 번 호출 (idempotency 위반)
- A→B 순서가 의도된 곳에 B→A 시도
- 중간에 예외 발생 후 재진입
- 동시성 (`asyncio` / threading) — **`policy.state.allow_nondeterminism=true`일 때만**

## 입력

`adversarial_context.json` + `policy.state.allow_nondeterminism`.

## 출력 (JSON 배열, AdversarialCase)

```json
[
  {
    "case_id": "c_<숫자>",
    "generator": "state",
    "target_symbol": "src/cart.py:add_item",
    "input_repr": "add_item('x') twice",
    "invocation": "[fn('x'), fn('x')]",
    "expected_behavior": "second call must be idempotent",
    "expected": "ANY",
    "nondeterministic": false
  }
]
```

`nondeterministic=true`인 케이스는 runner가 3회 연속 동일 verdict일 때만 finding 승격 (자동 처리).

## 규칙

1. `allow_nondeterminism=false`이면 결정론적 시나리오만 (정해진 순서의 호출). 동시성 후보는 ledger에 적재하되 실행 안 함.
2. 재진입 시나리오는 명확한 상태(전역, 인스턴스 필드 등)를 evidence로 인용.
