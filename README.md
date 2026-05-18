# test-validity-evaluator (chennai)

Claude Code skill for evaluating test suite quality via mutation testing and
adversarial red-teaming. Pluggable language adapters — Python+pytest와
Kotlin+JUnit 5 동봉.

```
사용자: "이 프로젝트 테스트 품질 평가해줘"
   ↓ (Claude Code가 SKILL.md frontmatter trigger로 자동 발화)
test-validity-evaluator
   ↓
[tier classifier] → [subject locator] → [mutation + adversarial] → ValidityReport JSON + HTML
```

## Install (글로벌, 한 번)

```bash
git clone https://github.com/binary-ho/test-test.git chennai
cd chennai
./install.sh
```

`install.sh` 가 수행하는 것:

1. `~/.claude/skills/test-validity-evaluator` → `chennai/` symlink 생성
2. PyYAML 자동 설치 (없을 때만)
3. tiny Python fixture로 smoke 분류 실행 (✓ 출력으로 검증)

업데이트는 `cd chennai && git pull` 만 — symlink라 재설치 불필요.

설치 확인:
```bash
ls -la ~/.claude/skills/test-validity-evaluator/SKILL.md
# → SKILL.md가 보이면 Claude Code가 다음 세션부터 자동 디스커버
```

## Use from any repo

설치 후 **어떤 레포에서든** Claude Code 세션에서 자연 발화하면 자동 트리거:

> "이 프로젝트 테스트 품질 평가해줘"
> "mutation testing 돌려줘"
> "테스트가 진짜 버그를 잡는지 확인해줘"
> "find weak tests"
> "run a red-team review of tests"

### 수동 호출 (Python 프로젝트)

```bash
cd <your-python-repo>
python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \
    --policy ./evaluation_policy.json \
    --session-id "evs_$(date +%Y%m%d_%H%M%S)"
```

### 수동 호출 (Kotlin 프로젝트)

```bash
cd <your-kotlin-repo>
python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/kotlin.junit.yaml \
    --policy ./evaluation_policy.json \
    --session-id "evs_$(date +%Y%m%d_%H%M%S)"
```

### `evaluation_policy.json` 생성 (없으면 한 번)

타겟 레포 루트에 다음 파일을 만듭니다:

```jsonc
{
  "session": {
    "session_id": "evs_…",
    "max_iterations": 5,
    "target_mutation_score": 0.85,
    "budget": { "max_tokens": 600000, "max_seconds": 1800 },
    "cache_enabled": true,
    "equivalent_mutants_yml": ".testvalidity/equivalent_mutants.yml",
    "suppressions_yml":       ".testvalidity/suppressions.yml"
  },
  "operators": {
    "syntactic_enabled": ["AOR","ROR","COR","LCR","BoundaryShift","ConstantReplace"],
    "semantic_enabled": true
  },
  "budget": { "max_mutants": 200 },
  "mutation":    {},
  "adversarial": {}
}
```

> 💡 `out/` 디렉터리가 cwd에 생성되니 타겟 레포의 `.gitignore`에 `out/` 추가 권장.

## Output

각 실행 후 `<cwd>/out/<session>/iter-N/` 아래:

| 파일 | 내용 |
|---|---|
| `validity_report.json` | 최종 ValidityReport (summary + findings + scope + adapter) |
| `report.html` | 사람이 보는 self-contained 단일 HTML |
| `tier.json` | tier classification 결과 |
| `subject_map.json` | TestSubjectMap[] (테스트 → SUT 매핑) |
| `mutation/{ledger,mutation_report}.json` | mutation 트랙 결과 |
| `adversarial/{ledger,red_team_report}.json` | adversarial 트랙 결과 |
| `../session.json` | EvaluationSession 누적 상태 (iteration 간 재호출 시 갱신) |

`evs_<id>`를 같은 ID로 재호출하면 iteration이 누적되어 보강 효과를 추적합니다 (6개 종료 조건: `target_reached`, `wontfix_only`, `max_iterations`, `plateau`, `recurrence_blocked`, `budget_exhausted` — 자세한 건 `SKILL.md` 참조).

## Troubleshooting

| 증상 | 원인 | 해결 |
|---|---|---|
| Claude Code 발화해도 skill이 트리거 안 됨 | 디스커버리 누락 | `ls ~/.claude/skills/test-validity-evaluator/SKILL.md`로 symlink 확인. Claude Code 세션 재시작 |
| `No unit tests passed tier classification` 경고만 출력 | 어댑터 미스매치 — Python 어댑터로 Kotlin 레포 분석 등 | `--adapter` 인자를 프로젝트 언어에 맞게 (`python.pytest.yaml` 또는 `kotlin.junit.yaml`) |
| `ModuleNotFoundError: No module named 'yaml'` | PyYAML 미설치 + minimal parser 경로 문제 | `pip3 install --user pyyaml` 또는 `./install.sh` 재실행 |
| Kotlin: subject_map.confidence 평균이 0.25 정도로 낮음 | resolver heuristic 한계 (동일 패키지 클래스 / `ClassName.staticMethod()` 호출 미감지) | 알려진 제한사항. mutation 결과에는 영향 있으나 분류/실행은 정상 |
| `~/.claude/skills/...` 가 못 찾아진다 | symlink가 다른 경로로 이전 또는 chennai 디렉터리 삭제 | `cd chennai && ./install.sh` 재실행. `readlink ~/.claude/skills/test-validity-evaluator` 로 link target 확인 |
| 다른 cwd에서 `out/` 위치 헷갈림 | `out/`은 invoke 시점 cwd 기준 생성 | 항상 타겟 레포 루트에서 호출. `out/`은 거기 생김 |

### CHENNAI_SKILL_ROOT env 사용

테스트나 개발 중 특정 디렉터리를 SKILL_ROOT로 강제하고 싶을 때:

```bash
CHENNAI_SKILL_ROOT=/path/to/local/chennai python3 .../scripts/orchestrate.py ...
```

설치 위치(`~/.claude/skills/test-validity-evaluator`)와 다른 경로의 코드를 가지고 임시로 돌릴 때 유용.

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
│   ├── types.md               # types source-of-truth
│   └── adapters/
│       ├── python.pytest.yaml + python_pytest/{discover,resolve,mutate,inject,run}.py
│       └── kotlin.junit.yaml  + kotlin_junit/{discover,resolve,mutate,inject,run}.py
├── docs/
│   ├── ADR-1-*.md, ADR-2-*.md, IMPLEMENTATION-REPORT.md
│   └── spec/                  # 4개 sub-skill 설계 사양 (디스커버리에서 제외용으로 .md rename)
├── install.sh
├── requirements.txt
└── README.md
```

## Supported adapters

- **Python + pytest** — 완성 (discover, resolve, mutate, inject, run)
- **Kotlin + JUnit 5** — discover, resolve, tier classification 완성. mutation은 Gradle 호출 미구현 (안전한 stub — 0 mutants 반환)

새 어댑터 추가:
1. `contracts/LanguageAdapter.schema.json` 참조해 manifest yaml 작성
2. `contracts/adapters/<lang>_<framework>/` 디렉터리에 5개 구현체 (`discover.py`, `resolve.py`, `mutate.py`, `inject.py`, `run.py`)
3. manifest의 `implementations.*`가 SKILL_ROOT 기준 상대경로로 5개 모두 가리키도록

## Architecture

- ADR-1: `docs/ADR-1-test-validity-evaluator-architecture.md`
- ADR-2: `docs/ADR-2-iterative-evaluation-adapter-registry-and-unit-test-scoping.md`
- Implementation report: `docs/IMPLEMENTATION-REPORT.md` (평탄화 이전 시점 기준)
- Sub-skill 설계 사양 (디스커버리에서 제외된 reference 문서):
  - `docs/spec/tier-classifier.md`
  - `docs/spec/subject-locator.md`
  - `docs/spec/mutation.md`
  - `docs/spec/adversarial.md`

## Known limitations (follow-up)

- Kotlin mutation은 stub. Gradle 호출 + Kotlin AST parser 구현 필요.
- Resolver heuristic이 동일 패키지 클래스 + `ClassName.staticMethod()` 호출 패턴 미감지 (Kotlin에서 ~15% mapping rate).
- `validity_report.json`의 ledger ref가 cwd-relative — 보고서를 다른 cwd로 옮기면 ref 깨짐 (planned: iter 디렉터리 상대로 변경).
- sub-orchestrator subprocess 실패가 silent — graceful이지만 진단 어려움 (planned: returncode 검사).
