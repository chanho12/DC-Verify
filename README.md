# DC-Verify

데이터센터 투자 자료에는 수전 용량, IT 용량, 계약률, 입주율처럼 비슷해 보이지만 의미가 다른 정보가 함께 등장합니다. 발표 당시의 계획이 이후에 실제로 이행됐는지도 따로 확인해야 합니다.

DC-Verify는 이런 정보를 원문 근거와 함께 검토하기 위해 만든 Python 프로젝트입니다. PDF에서 필요한 정보를 추출하고, 인용과 수치가 원문에 있는지 확인한 뒤, 다른 자료와 비교합니다. 근거가 부족한 항목은 보류 이유와 추가 확인 질문으로 남깁니다. 마지막에는 검토 내용을 HTML 리포트로 볼 수 있습니다.

## 작업 흐름

기능은 개발 순서에 따라 STEP 1~10으로 나뉘어 있습니다.

| 단계 | 하는 일 |
| --- | --- |
| STEP 1 | PDF를 Vision API로 읽고 페이지별 OCR 결과 저장 |
| STEP 2 | Controller가 검토 목적과 분석할 페이지 선택 |
| STEP 3 | Analysis Agent가 수치·지표·시점·원문 인용 추출 |
| STEP 4 | 추출한 내용을 원본 PDF와 대조 |
| STEP 5 | 주장의 증빙 수준과 부족한 근거 평가 |
| STEP 6 | Controller가 추가 확인할 내용과 다음 행동 결정 |
| STEP 7 | 외부 자료 검색 계획·수집·전처리 |
| STEP 8 | 외부 문서의 분석 허용 여부 판단, Claim 추출·검증·교차 비교 |
| STEP 9 | Controller가 결과를 종합해 추가 조사·보류·종료 결정 |
| STEP 10 | 최종 리포트와 사람의 검토 화면 생성 |

현재는 **각 단계를 따로 실행하는 구조**입니다. 전체 과정을 한 번에 이어서 실행하는 기능은 아직 연결하지 않았습니다. 각 단계는 앞 단계에서 생성한 PDF·JSON·실행 이력을 입력으로 사용합니다.

## 시작하기

Python 3.9 이상이 필요합니다. 현재 실행 검사는 macOS에서 진행했습니다. 기존 실험에서 사용하던 가상환경과 공통 `codex.py`의 API 연결 방식을 유지했습니다.

### 1. 가상환경 준비

저장소를 내려받고 프로젝트 폴더에서 실행합니다.

```bash
git clone https://github.com/chanho12/DC-Verify.git
cd DC-Verify

python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

기존 실험 환경에서 작업 중이라면 해당 가상환경을 그대로 사용해도 됩니다. `run.sh`는 프로젝트의 `.venv`를 우선 사용하고, 활성 가상환경과 기존 `01_experiments/.venv`도 찾습니다.

### 2. API.env에 본인 키 입력

처음 설정할 때 예제 파일을 복사합니다. 이미 `API.env`가 있다면 그 파일을 열어 수정하면 됩니다.

```bash
cp -n API.env.example API.env
```

`API.env`의 `OPENAI_API_KEY=` 뒤에 본인의 OpenAI API 키를 넣어 주세요.

```dotenv
OPENAI_API_KEY=여기에_본인의_OpenAI_API_키를_입력
TAVILY_API_KEY=
DC_VERIFY_DATA_DIR=./data/runtime
```

모델은 **gpt-5.4-mini**로 고정되어 있습니다. Python으로 외부 검색을 실행할 때는 별도의 Tavily 키가 필요합니다. OCR과 Analysis만 사용한다면 OpenAI 키부터 설정하면 됩니다.

```bash
./run.sh check-config
```

이 명령은 설정 파일을 읽을 수 있는지 확인합니다. 실제 API 요청은 보내지 않습니다. 키가 들어간 `API.env`는 Git에서 제외됩니다.

<details>
<summary>Windows에서 설정하기</summary>

PowerShell에서는 다음과 같이 가상환경을 준비합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
Copy-Item API.env.example API.env
```

`API.env`에 키를 넣은 뒤 `dc-verify check-config`로 확인합니다. 아래 실행 예시의 `./run.sh`는 `dc-verify`로 바꾸면 됩니다. Windows에서는 아직 직접 실행 검사를 하지 않았습니다.

</details>

## 실행해보기

### API 없이 데모 실행

먼저 키 없이 실행할 수 있는 예제를 준비했습니다.

```bash
./run.sh offline-demo
```

합성 자료에 미리 작성한 Claim을 넣어 인용·스키마·분석 허용 상태를 검사합니다. 실제 OCR이나 Agent API는 호출하지 않습니다. 결과는 `data/runtime/synthetic_demo/demo_result.json`에 저장됩니다.

### PDF로 OCR 실행

본인의 PDF를 `data/runtime/input.pdf`에 준비한 뒤 실행합니다.

```bash
./run.sh run ocr ./data/runtime/input.pdf \
  --output-dir ./data/runtime/step1/output
```

이 명령부터는 실제 OpenAI API를 사용합니다. 실행 결과 폴더에 `ocr.json`, 전체 텍스트, 페이지별 텍스트가 저장됩니다.

### 다른 단계 실행

실행 가능한 명령과 단계별 입력 옵션은 다음과 같이 확인할 수 있습니다.

```bash
./run.sh stages
./run.sh run analysis --help
./run.sh run controller --help
./run.sh run report --help
```

예를 들어 Analysis 입력이 준비되어 있다면 아래 명령으로 입력 검사를 먼저 할 수 있습니다.

```bash
./run.sh run analysis \
  --input ./data/runtime/step3/analysis_input.json \
  --request-config ./data/runtime/step3/analysis_request.json \
  --output-dir ./data/runtime/step3/output \
  --dry-run
```

`--dry-run`을 빼면 실제 Analysis API를 호출합니다. `run.sh`의 상대 경로는 모두 프로젝트 루트 기준이며, 인자 없이 실행하면 도움말을 보여줍니다.

## 결과는 어디에 저장되나요?

기본 저장 위치는 `data/runtime/stepN/`입니다. `--output-dir`이나 `API.env`의 `DC_VERIFY_DATA_DIR`로 바꿀 수 있습니다.

STEP 10에서는 `report.html`, `report.json`, `evidence.csv`와 원문 사본 폴더인 `originals/`를 만듭니다. HTML을 브라우저에서 열면 지표별 결과, 원문 인용, 문서 간 비교, 보류 이유와 추가 실사 질문을 확인할 수 있습니다. 검토 메모는 브라우저에 저장하거나 JSON으로 내보낼 수 있습니다.

사람이 남긴 메모는 별도로 보관합니다. 기존 Claim의 검증 결과를 바꾸려면 추가 자료를 다시 검증하는 과정이 필요합니다.

## 데이터와 코드 구성

```text
src/dc_verify/
├── codex.py              # 공통 API 연결
├── settings.py           # 환경 파일·데이터 경로 설정
├── cli.py                # 실행 명령
└── step/
    ├── step1/code/
    ├── ...
    └── step10/code/      # 단계별 코드·프롬프트·테스트

examples/synthetic/       # 키 없이 검사할 수 있는 합성 입력
data/seed/                # 승인된 초기 자료를 둘 위치
data/runtime/             # 입력·수집 자료·실행 결과, Git 제외
tests/                    # 테스트 모음
docs/                     # 구조·데이터·검증 기록
manifests/                # 파일 목록·해시·검사 결과
```

이 저장소에는 합성 예제만 포함했습니다. 실제 PDF·수집 원문·모델 응답은 포함하지 않으므로, 실제 검토를 실행할 때는 본인의 자료를 준비해야 합니다.

데이터는 초기 등록 자료와 작업 중 새로 수집한 자료로 구분합니다. 현재 사전 등록된 상주 데이터나 Resident DB는 없으며, 이전 실행의 자료를 새 작업에서 검색하는 기능도 아직 구현하지 않았습니다. 기존 실험 데이터의 출처와 수량은 [데이터 조사 문서](docs/DATA_INVENTORY.md)에 정리했습니다.

## 테스트와 현재 개발 범위

```bash
python -m unittest discover -s tests -v
```

독립 설치 환경에서 234개, 기존 실험 데이터를 연결한 환경에서 356개 테스트가 통과했습니다. `run.sh` 실행도 29건 확인했습니다. 저장소에 포함하지 않은 원문이 필요한 테스트는 건너뜁니다. 자세한 범위는 [재검사 보고서](docs/RECHECK_REPORT.md)에서 볼 수 있습니다.

전체 단계의 자동 실행과 저장된 근거를 재사용하는 Local Retrieval은 남은 작업입니다. 현재 외부 검색은 세션 도구의 결과를 가져오거나 Tavily 어댑터를 사용하는 방식이며, SEC·OpenDART 전용 실행 어댑터는 아직 없습니다. 라이선스도 아직 정하지 않았습니다.

구현 내용을 더 살펴보려면 [모듈 구조](docs/ARCHITECTURE.md), [단계별 코드 대응표](docs/CODE_PROVENANCE.md), [재현 방법](docs/REPRODUCIBILITY.md), [미구현 항목](docs/MISSING_FEATURES.md)을 참고해 주세요.
