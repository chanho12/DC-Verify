# DC-Verify

투자 자료에 적힌 수치와 주장을 원문 근거와 함께 검토하는 Python 프로젝트입니다.

PDF에서 필요한 정보를 추출하고, 원문과의 일치 여부와 증빙 수준을 확인합니다. 외부 자료와 비교한 뒤 근거가 부족한 항목은 보류 이유와 추가 확인 질문으로 남깁니다. 결과는 HTML 리포트로 볼 수 있습니다.

## 설치

Python 3.9 이상이 필요합니다. 프로젝트 폴더에서 가상환경을 만들고 설치합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

기존 실험 가상환경이 있다면 그대로 사용해도 됩니다. `run.sh`는 프로젝트의 `.venv`, 활성 가상환경, 기존 `01_experiments/.venv` 순서로 Python을 찾습니다.

## API 키 설정

처음 사용할 때 설정 파일을 복사합니다. 이미 `API.env`가 있다면 해당 파일을 수정하면 됩니다.

```bash
cp -n API.env.example API.env
```

`API.env`에 본인의 OpenAI API 키를 입력하세요.

```dotenv
OPENAI_API_KEY=여기에_본인의_OpenAI_API_키를_입력
TAVILY_API_KEY=
DC_VERIFY_DATA_DIR=./data/runtime
```

모델은 **gpt-5.4-mini**를 사용합니다. Python으로 외부 검색을 실행할 때는 별도의 Tavily 키와 검색 도구 설정이 필요합니다. `API.env`와 실행 데이터는 Git에서 제외됩니다.

```bash
./run.sh check-config
```

설정을 읽을 수 있는지 확인하는 명령입니다. 실제 API 요청은 보내지 않습니다.

## 실행

### API 없이 먼저 확인하기

```bash
./run.sh offline-demo
```

합성 자료로 인용·데이터 형식·분석 허용 상태를 검사합니다. 실제 OCR이나 Agent API는 호출하지 않습니다. 결과는 `data/runtime/synthetic_demo/demo_result.json`에 저장됩니다.

### PDF OCR

본인 PDF를 `data/runtime/input.pdf`에 준비한 뒤 실행합니다.

```bash
./run.sh run ocr ./data/runtime/input.pdf \
  --output-dir ./data/runtime/step1/output
```

실제 OpenAI API를 호출하며, 결과 폴더에 `ocr.json`과 전체·페이지별 텍스트가 저장됩니다.

### 단계별 실행

```bash
./run.sh stages
./run.sh run analysis --help
./run.sh run controller --help
./run.sh run report --help
```

현재는 단계를 하나씩 실행하는 구조입니다. 각 단계에 필요한 이전 단계의 입력 파일을 준비해야 하며, 전체 과정을 한 번에 실행하는 기능은 아직 연결하지 않았습니다.

| 단계 | 역할 |
| --- | --- |
| STEP 1 | PDF Vision OCR |
| STEP 2 | Controller 초기 검토 계획 |
| STEP 3 | 수치·지표·시점·원문 인용 추출 |
| STEP 4 | 원본 PDF와 추출 내용 대조 |
| STEP 5 | 증빙 수준 평가 |
| STEP 6 | Controller 관찰·재계획 |
| STEP 7 | 외부 자료 검색 계획·수집·전처리 |
| STEP 8 | 외부 문서 분석·검증·교차 비교 |
| STEP 9 | Controller 종합 판단 |
| STEP 10 | 최종 리포트와 사람의 검토 화면 |

공통 API 연결은 `src/dc_verify/codex.py`, 환경 파일과 데이터 경로 설정은 `settings.py`에 있습니다. 단계별 코드와 프롬프트는 `src/dc_verify/step/stepN/code/`에 모아 두었습니다.

## 결과 확인

입력과 실행 결과의 기본 위치는 `data/runtime/stepN/`입니다. `--output-dir` 또는 `DC_VERIFY_DATA_DIR`로 바꿀 수 있습니다. `run.sh`의 상대 경로는 프로젝트 루트 기준입니다.

STEP 10의 `report.html`을 열면 지표별 결과, 원문 인용, 문서 간 비교, 보류 이유를 확인할 수 있습니다. 같은 폴더에 `report.json`, `evidence.csv`, 원문 사본인 `originals/`도 생성됩니다. 검토 메모는 브라우저에 저장하거나 JSON으로 내보낼 수 있으며, 기존 검증 결과와 별도로 보관됩니다.

실제 원문과 과거 실행 데이터는 저장소에 포함하지 않았습니다. 본인의 자료와 각 단계의 입력 파일을 준비해 사용하세요. 사전 적재된 Evidence Store와 작업 간 Local Retrieval은 아직 구현하지 않았습니다.

## Windows

PowerShell에서는 `.venv\Scripts\Activate.ps1`로 가상환경을 활성화하고, `Copy-Item API.env.example API.env`로 설정 파일을 복사합니다. 위 명령의 `./run.sh` 대신 `dc-verify`를 사용하면 됩니다. Windows 환경에서의 직접 실행 검사는 아직 진행하지 않았습니다.
