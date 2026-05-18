# test-validity-evaluator (chennai)

Claude Code skill for evaluating test suite quality via mutation testing and
adversarial red-teaming. Pluggable language adapters (Python+pytest,
Kotlin+JUnit included).

## Install (글로벌, 한 번)

```bash
git clone https://github.com/binary-ho/test-test.git chennai
cd chennai
./install.sh
```

`install.sh` 는 다음을 수행합니다:

1. `~/.claude/skills/test-validity-evaluator` → `chennai/` symlink 생성
2. PyYAML 자동 설치 (없을 때만)
3. tiny Python fixture로 smoke 분류 실행 (✓ 출력으로 검증)

업데이트는 `cd chennai && git pull` 만 — symlink라 재설치 불필요.

## Use from any repo

설치 후 Claude Code 세션에서 자연 발화하면 자동 트리거:

> "이 프로젝트의 테스트 품질 평가해줘"
> "mutation testing 돌려줘"
> "테스트가 진짜 버그를 잡는지 확인해줘"

또는 수동 Bash 호출:

```bash
python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
    --policy ./evaluation_policy.json \
    --session-id "evs_$(date +%Y%m%d_%H%M%S)"
```

자세한 사용은 `SKILL.md` 참조.

## Package layout

```
chennai/                       (= ~/.claude/skills/test-validity-evaluator/)
├── SKILL.md                   # Claude Code-discoverable entry point
├── scripts/
│   ├── _common.py             # SKILL_ROOT, load_yaml
│   ├── orchestrate.py         # top-level
│   ├── classify.py            # tier classifier
│   ├── mutation_orchestrate.py
│   └── adversarial_orchestrate.py
├── prompts/                   # 13개 LLM 프롬프트 (semantic/critique/adversarial)
├── contracts/
│   ├── *.schema.json          # JSON Schema (ValidityReport, MutationLedger, ...)
│   ├── types.md
│   └── adapters/
│       ├── python.pytest.yaml + python_pytest/{discover,resolve,mutate,inject,run}.py
│       └── kotlin.junit.yaml  + kotlin_junit/{...}
├── docs/
│   ├── ADR-1-*.md, ADR-2-*.md, IMPLEMENTATION-REPORT.md
│   └── spec/                  # 4개 sub-skill spec doc (디스커버리 제외용으로 SKILL.md → .md)
├── install.sh
├── requirements.txt
└── README.md
```

## Supported adapters

- **Python + pytest** — 완성 (discover, resolve, mutate, inject, run)
- **Kotlin + JUnit 5** — discover, resolve, tier classification 완성. mutation은 Gradle 호출 미구현 (안전한 stub).

새 어댑터: `contracts/LanguageAdapter.schema.json` 참조 + `contracts/adapters/<lang>_<framework>/` 디렉터리 생성.

## Architecture

- ADR-1: `docs/ADR-1-test-validity-evaluator-architecture.md`
- ADR-2: `docs/ADR-2-iterative-evaluation-adapter-registry-and-unit-test-scoping.md`
- Implementation report: `docs/IMPLEMENTATION-REPORT.md`
- Sub-skill spec (디스커버리에서 제외된 reference 문서):
  - `docs/spec/tier-classifier.md`
  - `docs/spec/subject-locator.md`
  - `docs/spec/mutation.md`
  - `docs/spec/adversarial.md`
