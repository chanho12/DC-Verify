# DC-Verify

DC-Verify는 투자·산업 문서에서 추출한 주장을 원문, 의미, 시점, 증빙 수준별로 검토하는 Python 실험 프로젝트입니다. 문서에 수치가 있다는 사실과 실제 운영 실적·독립 증빙·비교 가능성을 구분합니다. 특정 회사의 미확인 수치를 확정하거나 지표를 임의 환산하지 않습니다.

기존 `experiment`에서 사용하던 **Python 가상환경 + 공통 `codex.py` API 연결 방식**을 유지했습니다. 시작은 두 단계입니다. **① 가상환경을 준비하고 ② `API.env`에 본인의 OpenAI API 키를 입력하면 API 연결 준비가 완료됩니다.** 오프라인 테스트와 합성 데모는 키 없이도 실행할 수 있습니다. 각 검증 단계에는 해당 PDF/JSON 입력이 별도로 필요합니다.

이 저장소는 기존 STEP 1~10 코드를 별도로 패키징한 프로젝트입니다. manifests/와 docs/의 조사·검증 기록은 GitHub 업로드 전 로컬 준비 단계의 결과입니다. 원본 문서·실제 환경 파일·모델 응답·실행별 원문 인용은 배포하지 않습니다. 라이선스는 아직 선택되지 않았습니다.

## 핵심 기능과 Workflow

```text
PDF → Vision OCR → Controller 초기 계획 → Claim 추출
    → 원본 PDF/HTML·의미 검증 → 증빙 수준 평가
    → Controller 재계획 → 검색 계획 → 실제 영수증/검색 어댑터·원문 수집
    → 전처리·Source Admission → 외부 Claim 분석·검증
    → 비교 후보 선별·교차 검증 → Controller 판단 → HTML Human Review
```

구현된 기능은 페이지 인덱스, 선택 페이지 추출, 원문 인용·숫자·단위·범위 검사, Source Admission/Claim Guard, 격리·잠정 상태 보존, 검색 예산·실행 이력 검사, Pair 선별, 문서 간 비교, Controller 종료 결정, 최종 HTML·JSON·CSV와 검토 메모 UI입니다. Prompt는 API 실행 코드와 별도 파일에 있습니다.

**각 단계의 실행 코드는 있지만, 위 전체 흐름을 자동으로 연결하는 End-to-End 실행기와 자율 반복 루프는 없습니다.** CLI의 `run`은 지정한 한 단계의 기존 `main()`만 호출합니다. 새로운 Agent나 가짜 통합 Pipeline을 추가하지 않았습니다. [모듈·입출력·의존성](docs/ARCHITECTURE.md), [통합 점검](docs/INTEGRATION_AUDIT.md)을 참고하세요.

## 구현된 Agent·Tool

| 구분 | 실제 기능 |
|---|---|
| Document Analysis | Vision OCR, OCR 페이지 키워드/관련도 인덱스 |
| Analysis Agent | Claim 추출·스키마·인용 검증, 외부 문서에 재사용 |
| Source/Semantic Verifier | PDF 이미지·HTML·의미·시설 범위·계획/실적·시점 검사 |
| Evidence Assessment | 원문 일치와 검토 목적의 증빙 충분성 분리 |
| Controller | 초기 계획, Observe/Replan, 이력·예산 복원, 보류·종료 |
| Search Planner/Executor | 상황별 계획, 실제 영수증 Import, Tavily HTTP 어댑터, 후보/중복/실패 관리 |
| Source Admission Gate | ALLOW / CONDITIONAL / DENY와 최종 증빙 채택 분리 |
| Cross-source | 비교 후보 선별, 관계 판단, 로컬 진단, 비교 Guard |
| Report Builder | 기존 결과를 HTML·JSON·CSV·검토 질문으로 변환; 새 LLM 없음 |

`CODEX_WEB_SEARCH`는 세션 도구이며 Python SDK가 아닙니다. Python 검색에는 별도 Tavily 키와 현재 환경에 맞는 Registry가 필요합니다. SEC/OpenDART는 후보 도구 정보만 있고 전용 실행 어댑터가 없습니다. Tool Registry의 선택·정책 검사는 구현했지만 선택 결과의 자동 실행 루프는 미완성입니다.

## 두 Evidence Pool과 현재 보유 데이터

- **RESIDENT_EVIDENCE**: 새 작업 전에 승인·등록되어 검색/재사용 가능한 초기 자료. 이번 조사에서 등록 원본 0개, 등록 Claim 0개이며 Resident DB·작업 간 Local Retrieval 실행기는 찾지 못했습니다.
- **AGENT_ACQUIRED_EVIDENCE**: 특정 실행에서 Agent가 수집한 자료. 기존 실험에는 원문 7종이 있습니다. 다음 작업에서 로컬 재사용하더라도 최초 수집 출처는 유지합니다.
- **USER_PROVIDED**: 사용자 최초 입력 원본 1종. 명시적 Corpus 등록이 없어 Resident로 분류하지 않았습니다.
- **DERIVED_DATA**: Claim·검증·Pair·보고서·캐시. 최신 계보에는 사용자 10개·외부 48개 Claim이 있으며 상주 증빙으로 자동 편입되지 않습니다.

디스크에 파일이 있다는 사실과 상주 데이터로 등록되었다는 사실은 다릅니다. 문서 내 페이지 관련도 검색은 존재하지만 작업 간 Evidence Store 검색과 구분합니다. 공개 배포가 승인되지 않은 실제 문서는 제외하고 명시적인 `SYNTHETIC_TEST_DATA` 예제만 제공합니다. [데이터 조사](docs/DATA_INVENTORY.md), [Resident 상태](docs/RESIDENT_DATA_STATUS.md), [데이터 구조](docs/DATA_ARCHITECTURE.md), [출처](docs/DATA_PROVENANCE.md)를 참고하세요.

## 폴더 구조

```text
DC-Verify/
├── README.md / run.sh / pyproject.toml / setup.cfg / API.env.example / .gitignore
├── src/dc_verify/
│   ├── cli.py / settings.py / codex.py / demo.py
│   ├── report_policy.json
│   └── step/step1~step10/code/     # 기존 기능·Prompt·테스트 유지
├── data/seed/README.md             # 등록 데이터 없음; 계약 설명
├── examples/synthetic/             # 중립 합성 입력
├── tests/                         # 기존 테스트 집계·패키징/Guard 테스트
├── docs/                          # 조사·구조·계보·재현·미구현 항목
└── manifests/                     # 상대 경로/해시 목록과 검사 결과
```

`data/runtime/`은 실행 시 생성하며 Git에서 제외합니다. `API.env`·캐시·빌드 파일도 제외합니다. Runtime은 기본 `data/runtime/stepN/`이며 `DC_VERIFY_DATA_DIR`로 바꿀 수 있습니다.

## 1. 가상환경 준비

Python 3.9 이상이 필요합니다. 확인한 환경은 Python 3.9이며 macOS에서 검사했습니다. Windows용 코드는 pathlib와 환경변수를 사용하지만 Windows에서 직접 실행 검증하지는 않았습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

기존 실험 프로젝트 안에서 이 폴더를 사용하는 경우에는 새 가상환경을 만들 필요 없이 기존 환경을 그대로 활성화할 수 있습니다. 아래 명령은 `github_release/DC-Verify/`에서 실행합니다.

```bash
source ../../01_experiments/.venv/bin/activate
python -m pip install .
```

`codex.py`는 기존 실험의 API 함수와 고정 모델 설정을 재사용하며, 패키지 안에서는 `dc_verify.codex`로 Import합니다. 기존 실험의 코드와 `API.env`는 수정하지 않습니다.

Windows PowerShell에서는 가상환경을 `.venv\Scripts\Activate.ps1`로 활성화합니다. 실행 정책을 변경하는 명령은 포함하지 않습니다. 직접 의존성은 OpenAI SDK, python-dotenv, Pydantic, PyMuPDF, Beautiful Soup, certifi입니다. 실제 확인 버전은 `requirements-tested.txt`에 있으며 완전한 dependency lock은 아닙니다.

## 2. API.env에 본인 API 키 입력

로컬 준비 폴더에는 값을 비워 둔 `API.env`를 만들어 두었습니다. 해당 파일의 `OPENAI_API_KEY=` 뒤에 본인 키를 입력하면 됩니다. GitHub에서 새로 내려받은 경우에는 배포용 예제를 먼저 복사하세요. 실제 키가 들어간 `API.env`는 Git에서 제외됩니다.

```bash
# macOS / Linux
cp API.env.example API.env
```

Windows PowerShell에서는 `Copy-Item API.env.example API.env`를 사용합니다.

```dotenv
OPENAI_API_KEY=본인의_OpenAI_API_키를_여기에_입력
TAVILY_API_KEY=
DC_VERIFY_DATA_DIR=./data/runtime
# DC_VERIFY_ENV_FILE=./API.env
```

위 키 문자열은 설명용 자리표시자입니다. 실제 `API.env`에는 발급받은 키를 넣습니다. 키 우선순위는 환경변수 → 지정된 환경 파일입니다. 기존 실험처럼 `API=` 형식도 호환되지만 `OPENAI_API_KEY=`를 권장합니다. 모델은 **gpt-5.4-mini**로 고정되어 있습니다. `DC_VERIFY_ENV_FILE`은 별도 파일을 지정하며 기본은 현재 작업 폴더의 `API.env`입니다.

```bash
dc-verify check-config
```

이 명령은 연결이나 모델 호출 없이 설정만 확인합니다. 키가 없으면 실패 상태와 종료 코드 1을 반환하고, 키 값은 출력하지 않습니다.

## run.sh로 실행하기 — macOS / Linux

프로젝트 루트의 `run.sh`로 같은 CLI를 실행할 수 있습니다. 의존성이 설치된 가상환경이 필요합니다. 패키지를 아직 설치하지 않았더라도 옆의 `src/` 코드를 읽어 실행합니다.

```bash
./run.sh                         # 도움말만 표시
./run.sh stages                  # 구현된 단계 목록
./run.sh check-config            # API.env 설정 확인; API 호출 없음
./run.sh offline-demo            # 키 없이 합성 Guard 데모
./run.sh run analysis --help     # Analysis 단계 옵션

# 본인 PDF로 실제 OCR API 실행
./run.sh run ocr ./data/runtime/input.pdf --output-dir ./data/runtime/step1/output
```

Python 선택 순서는 `DC_VERIFY_PYTHON` → 이 폴더의 `.venv` → 활성 가상환경 → 기존 `01_experiments/.venv` → `python3`입니다. 별도 Python을 쓰려면 `DC_VERIFY_PYTHON=/원하는/가상환경/bin/python ./run.sh stages`처럼 지정합니다. 기존 실험 환경의 상대 경로는 이 작업공간에서만 사용하는 선택적 fallback이며, 다른 곳에 옮기면 로컬 `.venv`를 사용하면 됩니다.

스크립트는 프로젝트 루트로 이동한 뒤 실행하므로 입력·출력의 상대 경로도 프로젝트 루트 기준입니다. 인자가 없으면 도움말만 표시하며 전체 Pipeline이나 API 요청을 자동 실행하지 않습니다. Windows에서는 가상환경 활성화 후 앞서 안내한 `dc-verify` 또는 `python -m dc_verify.cli` 명령을 사용합니다.

## 재현 가능한 오프라인 데모·테스트

```bash
dc-verify stages
dc-verify offline-demo --output-dir data/runtime/demo
dc-verify run analysis \
  --input examples/synthetic/analysis_input.json \
  --request-config examples/synthetic/analysis_request.json \
  --output-dir data/runtime/analysis --dry-run
python -m unittest discover -s tests -v
```

합성 데모는 이미 작성한 중립 Claim에 기존 스키마·인용·Source Admission Guard를 적용하고 DEFERRED/UNKNOWN 표시를 확인합니다. 모델 추출·OCR·검색·원본 이미지 검증·Controller 판단을 실제 수행한 데모가 아닙니다. `--dry-run` 명령도 Analysis 입력 준비만 합니다. 결과는 `demo_result.json`과 `analysis_input.json` 등으로 지정한 Runtime 폴더에 저장됩니다.

재검사에서 독립 설치 테스트 234개, 실제 보유 데이터 연결 테스트 356개가 통과했습니다. `run.sh` 실행 점검 29건도 통과했습니다. 검사 범위와 수정 내용은 [재검사 보고서](docs/RECHECK_REPORT.md)에 있습니다.

원문이 필요한 기존 실험 테스트는 원문을 배포하지 않으므로 명시적으로 skip합니다. skip과 테스트 성공은 별도 집계합니다. 실제 독립 환경 검사 수치는 [release_validation.json](manifests/release_validation.json)에 있습니다. [재현 범위](docs/REPRODUCIBILITY.md)를 함께 읽어 주세요.

## 실제 API 실행

다음은 구현된 명령이며 **직접 실행하면 외부 API를 호출할 수 있습니다**. 이번 패키징 작업에서는 호출하지 않았습니다. API 키와 사용 권한이 있는 PDF를 먼저 로컬에 준비합니다.

```bash
# 실제 Vision OCR
dc-verify run ocr ./data/runtime/input.pdf --output-dir data/runtime/step1/output

# 사용자가 준비한 정상 입력에 대한 실제 Analysis; --dry-run 없음
dc-verify run analysis \
  --input ./data/runtime/step3/analysis_input.json \
  --request-config ./data/runtime/step3/analysis_request.json \
  --output-dir ./data/runtime/step3/output

# 단계별 필수 입력/옵션 확인 — API 호출 없음
dc-verify run search-execute --help
dc-verify run search-api --help
dc-verify run controller --help
dc-verify run report --help
```

각 단계의 입력·계보·Tool 설정은 사용자가 제공해야 합니다. 키만 설정한다고 전체 단계가 자동 연결되지는 않습니다. 검색 API는 Executor 실행 디렉터리와 Python Registry가 준비되어야 하며 `dc-verify run search-api --run <실행폴더>`로 호출합니다. 기존 자료의 해시·범위·예산 Guard를 우회하지 않습니다.

## 결과 확인과 한계

정상 Controller 실행 및 이전 원문 계보가 Runtime에 있을 때 다음 명령으로 기존 Report Builder를 실행합니다.

```bash
dc-verify run report --source-run ./data/runtime/step9/output/<controller_cycle_RUN_ID> \
  --output-dir ./data/runtime/step10/output
```

`report.html`, `report.json`, `evidence.csv`, `originals/`가 생성됩니다. HTML은 원문 폴더와 함께 로컬에서 열 수 있습니다. Human Review 메모는 별도 annotation이며 Claim·격리·잠정 상태·Controller 결정을 수정하지 않습니다. 배포 폴더에는 기존 실험의 실제 리포트/원문/모델 응답이 없으므로 바로 이 명령을 실행하면 입력 부족으로 차단됩니다.

원문 일치, 올바른 의미, 독립 증빙, 사용자 목표 충족, 비교 가능성은 각각 다른 판정입니다. 공개일 미확인, 계획/실적 혼동, 제공 용량/실소비 전력 혼동을 유지·표시해야 합니다. 이전 사례의 DEFER/INCOMPLETE를 현재 운영 용량 확인 완료로 해석하지 않습니다.

Resident Store, Local Retrieval 자동 실행, 전체 자동 Pipeline, 과거 절대 경로 실행의 자동 이전, SEC/OpenDART 실행 어댑터는 미구현입니다. Live API 재검사·새 원문 수집은 이번에 수행하지 않았습니다. 코드 라이선스와 실제 문서 재배포 권한은 별도 확정이 필요하며 임의 LICENSE 파일을 만들지 않았습니다. [남은 기능과 공개 제한](docs/MISSING_FEATURES.md), [코드 대응표](docs/CODE_PROVENANCE.md)를 확인하세요.
