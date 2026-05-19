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

설치 확인:
```bash
ls -la ~/.claude/skills/test-validity-evaluator/SKILL.md
# → SKILL.md가 보이면 Claude Code가 다음 세션부터 자동 디스커버
```

## Updating

위의 install 명령어는 **clone 시점의 main을 한 번 가져올 뿐** 입니다. 자동 동기화는 없습니다. 다시 같은 블록을 통째로 돌리면 `git clone` 이 "destination 'chennai' already exists" 로 fail 합니다.

최신화 방법은 셋 중 하나:

```bash
# A. 평소 — 가장 가벼움. symlink는 그대로니까 install.sh 재실행 불필요.
cd chennai && git pull

# B. 강제 클린 재설치 — chennai/ 손상됐을 때만.
rm -rf chennai
git clone https://github.com/binary-ho/test-test.git chennai
cd chennai && ./install.sh

# C. 주기적 자동 sync — cron / launchd 직접 설정.
#    예) crontab -e
#    0 * * * * cd ~/chennai && git pull -q
```

`./install.sh` 는 idempotent (이미 같은 symlink가 있으면 `✓ symlink already points at ...` 만 출력) 이라 다시 실행해도 안전합니다. 단 코드 업데이트 효과는 없습니다 — symlink만 다시 만들 뿐.

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

`./gradlew` (또는 system `gradle`)가 필요합니다.

### 수동 호출 (TypeScript+Jest 프로젝트)

```bash
cd <your-typescript-repo>
python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \
    --root . \
    --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/typescript.jest.yaml \
    --policy ./evaluation_policy.json \
    --session-id "evs_$(date +%Y%m%d_%H%M%S)"
```

`npx jest`가 동작해야 합니다 (pnpm/yarn lockfile 감지 시 자동으로 `pnpm exec jest`/`yarn jest` 사용).

### LLM 단계 (pause·resume)

orchestrate.py 는 LLM 작업이 필요한 4 지점(`mutation.semantic_operator`, `mutation.survivor_diagnose`, `adversarial.critique`, `adversarial.generate_cases`)에서 `__PAUSE__` 시그널과 함께 종료합니다. `<iter_dir>/_pending_llm.json` 에 prompt·context·output 경로가 적혀 있으니, LLM 작업 후 output JSON 을 쓰고:

```bash
python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py --resume <iter_dir>
```

로 재호출하면 다음 단계가 진행됩니다. 4번의 resume 후 자동 aggregation·HTML 렌더로 종료. Claude Code 세션 안이면 자연 발화 → 자동 루프.

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
| `No unit tests passed tier classification` 경고만 출력 | 어댑터 미스매치 — Python 어댑터로 Kotlin 레포 분석 등 | `--adapter` 인자를 프로젝트 언어에 맞게 (`python.pytest.yaml`, `kotlin.junit.yaml`, `typescript.jest.yaml`) |
| `ModuleNotFoundError: No module named 'yaml'` | PyYAML 미설치 + minimal parser 경로 문제 | `pip3 install --user pyyaml` 또는 `./install.sh` 재실행 |
| `__PAUSE__` 출력 후 멈춤 | 정상 — LLM 작업 대기 중 | `<iter_dir>/_pending_llm.json` 읽고 output 채운 뒤 `orchestrate.py --resume <iter_dir>` |
| Kotlin: `[run] no ./gradlew nor system gradle found` | Gradle 미설치 | 타겟 레포에 `./gradlew` 추가하거나 `gradle` 시스템 설치 |
| TS: `npx`/`jest` 못 찾음 | Node toolchain 부재 | Node + jest 설치, 또는 lockfile (pnpm-lock.yaml/yarn.lock) 일치하는 매니저 설치 |
| 리졸버: subject_map.confidence 평균이 낮음 | resolver heuristic 한계 (`ClassName.staticMethod()` 호출 패턴 등 미감지) | 알려진 제한사항. mutation 결과에는 영향 있으나 분류/실행은 정상 |
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
│       ├── kotlin.junit.yaml  + kotlin_junit/{discover,resolve,mutate,inject,run}.py
│       ├── typescript.jest.yaml + typescript_jest/{discover,resolve,mutate,inject,run}.py
│       └── _operator_tables.py   # shared Mutant + AOR/ROR tables + regex emitters
├── docs/
│   ├── ADR-1-*.md, ADR-2-*.md, IMPLEMENTATION-REPORT.md
│   └── spec/                  # 4개 sub-skill 설계 사양 (디스커버리에서 제외용으로 .md rename)
├── install.sh
├── requirements.txt
└── README.md
```

## Supported adapters

- **Python + pytest** — 완성 (discover, resolve, mutate via AST, inject, run)
- **Kotlin + JUnit 5** — 완성. regex 기반 6 operators, `./gradlew test --tests` 호출, JUnit XML 파싱
- **TypeScript + Jest** — 완성. regex 기반 6 operators, `npx jest --json` (pnpm/yarn lockfile 자동 감지), Jest JSON 출력 파싱

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

- Kotlin/TS mutation은 regex 기반이라 토큰 경계가 애매한 케이스(예: 문자열 인터폴레이션 안의 표현식)에서 false positive 가능. equivalence-judge 프롬프트가 2차 가드.
- Resolver heuristic이 `ClassName.staticMethod()` 호출 패턴은 여전히 미감지 (Kotlin/TS 공통). Same-package 클래스는 v0.1 부터 지원.
- TS adapter v1 은 Jest 만 지원. Vitest 는 동일 패턴으로 별도 manifest+5 파일 추가하면 됨.
- `validity_report.json`의 ledger ref가 cwd-relative — 보고서를 다른 cwd로 옮기면 ref 깨짐 (planned: iter 디렉터리 상대로 변경).
- PiTest 통합은 `capabilities.uses_pitest` 매니페스트 플래그로 옵트인 추가 가능 (현재 미구현).
- sub-orchestrator subprocess 실패가 silent — graceful이지만 진단 어려움 (planned: returncode 검사).
