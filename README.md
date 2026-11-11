# DC-Verify

투자 자료에 적힌 수치와 주장을 원문 근거와 함께 검토하는 Python 프로젝트입니다. PDF에서 정보를 추출하고 원문 대조·증빙 평가·Controller 판단을 거쳐 HTML 리포트를 만듭니다.

## 설치

Python 3.9 이상이 필요합니다. 프로젝트 폴더에서 실행하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

기존 실험 가상환경도 사용할 수 있습니다. `run.sh`는 프로젝트의 `.venv`, 활성 가상환경, 기존 `01_experiments/.venv` 순서로 Python을 찾습니다.

## API 키 설정

```bash
cp -n API.env.example API.env
```

`API.env`에 본인의 키를 입력합니다. 기존 파일이 있다면 그대로 수정하세요.

```dotenv
OPENAI_API_KEY=여기에_본인의_OpenAI_API_키를_입력
TAVILY_API_KEY=
DC_VERIFY_DATA_DIR=./data/runtime
```

모델은 **gpt-5.4-mini**입니다. `API.env`와 실행 데이터는 Git에서 제외됩니다.

## PDF에서 최종 리포트까지 실행

API 키를 설정하고 PDF를 준비한 뒤, 검토 질문과 함께 실행합니다.

```bash
./run.sh verify \
  --pdf ./input.pdf \
  --request "이 문서에 기재된 운영 IT 용량과 계획·실적 구분을 검토해줘"
```

입력 파일 전달과 단계별 실행은 실행기가 처리합니다. 중간 JSON을 직접 복사할 필요가 없습니다. 검토 대상은 질문과 OCR에서 Controller가 식별하며, 직접 지정할 수도 있습니다.

```bash
./run.sh verify \
  --pdf ./input.pdf \
  --request "현재 운영 IT 용량을 원문 근거와 함께 확인해줘" \
  --target "검토할 기업 또는 시설 이름" \
  --as-of 2026-11-12
```

문서 발행일을 확인했다면 `--published-at YYYY-MM-DD` 또는 `YYYY-MM`으로 전달할 수 있습니다. 생략하면 발행일 미확인 상태를 유지합니다. 검토 기준일을 생략하면 실행일의 한국 날짜를 사용합니다.

### 외부 자료도 검색하려면

Tavily 키를 설정하고 검색·수집을 허용할 공식 호스트를 지정합니다.

```bash
./run.sh verify \
  --pdf ./input.pdf \
  --request "공식 후속 자료와 함께 운영 상태를 검토해줘" \
  --official-domain 공식사이트의_호스트명
```

호스트는 `www.company.com` 같은 형태로 넣습니다. 여러 곳이면 `--official-domain`을 반복합니다. Controller가 외부 검색을 선택할 때 검색 계획·실제 검색·원문 수집·Admission·외부 분석·교차 검증으로 이어집니다. 검색 횟수는 쟁점별 최대 2회이며, 한 검토에서는 검색·외부 검증을 한 차례 수행합니다.

검색 도구가 없거나 적합한 새 자료를 확보하지 못해도, 해당 이유를 기록하고 현재 PDF의 검토 결과로 리포트를 만듭니다. `SKIPPED` 상태는 검색이나 검증이 성공했다는 뜻이 아닙니다. 추가 로컬 분석·원문 재검토가 필요한 경우에도 최종 리포트의 후속 확인 사항으로 남깁니다.

## 결과 확인

실행마다 `data/runtime/review_실행ID/` 폴더가 생성됩니다. 터미널에 출력되는 `report.html` 경로를 브라우저에서 열어 주세요.

- `pipeline.json`: STEP 0~10의 실행 상태, API 요청·응답 기록, 실패 이유
- `stepN/`: 해당 단계의 입력·출력·원문 위치·검증 기록
- `step10/` 아래 `report.html`, `report.json`, `evidence.csv`, `originals/`: 최종 검토 결과와 원문 사본

실행의 `SUCCESS`와 검토 목적의 달성 여부는 구분합니다. 리포트가 생성됐어도 근거가 부족하면 Controller가 `DEFER`·`INCOMPLETE`로 판단할 수 있습니다. API·입력·검증 오류로 중단되면 종료 코드 1과 `BLOCKED` 기록을 남기고, 완료되지 않은 단계를 성공으로 표시하지 않습니다.

검토 메모는 브라우저에 저장하거나 JSON으로 내보낼 수 있습니다. 기존 검증 결과를 바꾸려면 추가 자료를 다시 검증해야 합니다.

## 단계별 기능

| 단계 | 역할 |
| --- | --- |
| STEP 0 | 입력 PDF·API 설정 확인 |
| STEP 1 | PDF Vision OCR |
| STEP 2 | 검토 대상·초기 계획 선택 |
| STEP 3 | 수치·지표·시점·원문 인용 추출 |
| STEP 4 | 원본 PDF 대조 |
| STEP 5 | 증빙 수준 평가 |
| STEP 6 | Controller 관찰·다음 행동 선택 |
| STEP 7 | 필요한 외부 자료 검색·수집·전처리 |
| STEP 8 | 외부 문서 Admission·분석·검증·교차 비교 |
| STEP 9 | Controller 종합 판단 |
| STEP 10 | 최종 리포트·사람의 검토 화면 |

단계를 따로 실행하는 명령도 유지했습니다.

```bash
./run.sh stages
./run.sh run analysis --help
./run.sh run report --help
./run.sh verify --help
```

API 없이 기본 Guard만 확인하려면 `./run.sh offline-demo`를 사용합니다. 이 데모는 합성 자료를 검사하며 실제 모델 추출이나 검색을 수행하지 않습니다.

## 코드 구성

통합 실행기는 `src/dc_verify/pipeline.py`, 공통 API 연결은 `codex.py`, 환경 설정은 `settings.py`에 있습니다. 단계별 코드와 프롬프트는 `src/dc_verify/step/stepN/code/`에 있습니다. 중간 산출물과 실제 원문은 저장소에 포함하지 않습니다.

현재 통합 실행은 PDF 1개에 대한 검토 한 회차를 지원합니다. 저장된 근거를 다음 검토에서 검색하는 Evidence Store와 Local Retrieval은 아직 구현하지 않았습니다.

## Windows

PowerShell에서는 `.venv\Scripts\Activate.ps1`로 가상환경을 활성화하고 `Copy-Item API.env.example API.env`로 설정 파일을 복사합니다. `./run.sh` 대신 `dc-verify` 또는 `python -m dc_verify.cli`를 사용하세요. Windows에서 직접 실행 검사는 아직 진행하지 않았습니다.
