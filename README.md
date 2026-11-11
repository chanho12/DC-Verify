# DC-Verify

PDF에 적힌 수치와 주장을 원문 근거와 함께 검토하는 프로젝트입니다. **API 키, PDF, 검토 질문**을 넣으면 OCR부터 분석·원문 대조·증빙 평가·Controller 판단을 거쳐 HTML 리포트까지 만듭니다. 중간 JSON을 직접 옮길 필요는 없습니다.

리포트에는 문서상 주장, 검증 결과, 미확인 항목, 판단 보류 이유와 사람이 확인할 질문이 함께 나옵니다. 근거가 부족하면 `DEFER / INCOMPLETE`로 남깁니다.

## 시작하기

Python 3.9 이상이 필요합니다. 새 환경에는 Python 3.11 이상을 권장합니다. 저장소를 내려받은 폴더에서 실행하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp -n API.env.example API.env
```

`API.env`를 열어 본인의 OpenAI API 키를 입력합니다. 기존 파일이 있다면 복사하지 않고 해당 파일을 수정하세요.

```dotenv
OPENAI_API_KEY=여기에_본인의_API_키를_입력
TAVILY_API_KEY=
DC_VERIFY_DATA_DIR=./data/runtime
```

기본 PDF 검토에는 OpenAI 키만 필요합니다. 외부 웹 검색까지 사용하려면 Tavily 키도 입력합니다. Qdrant는 로컬 저장 모드로 실행되므로 별도의 서버나 API 키가 필요하지 않습니다. 실제 키가 들어간 `API.env`, 원문과 실행 결과는 Git에서 제외됩니다.

## 한 번에 실행하기

```bash
python DC_verify.py \
  --pdf ./input.pdf \
  --request "이 자료의 운영 IT 용량을 확인하고 계획과 실제 운영 정보를 구분해줘"
```

검토 대상과 기준일도 직접 지정할 수 있습니다.

```bash
python DC_verify.py \
  --pdf ./input.pdf \
  --request "현재 운영 IT 용량과 같은 기준으로 비교할 수 있는지 검토해줘" \
  --target "검토할 기업 또는 시설" \
  --as-of 2026-11-12
```

`--target`을 생략하면 Controller가 질문과 OCR에서 대상을 식별합니다. `--as-of`의 기본값은 실행일의 한국 날짜입니다. 발행일을 확인한 문서에는 `--published-at YYYY-MM-DD` 또는 `YYYY-MM`을 추가하세요. 입력하지 않은 발행일은 미확인 상태로 유지합니다.

Mac·Linux에서는 같은 명령을 `./run.sh --pdf ... --request ...`로 실행할 수도 있습니다. `run.sh`는 저장소의 `.venv`, 활성 가상환경, 기존 실험 환경 순서로 Python을 찾습니다.

### 외부 자료 검색

Tavily 키를 설정하고 검색·수집을 허용할 공식 호스트를 지정합니다.

```bash
python DC_verify.py \
  --pdf ./input.pdf \
  --request "후속 공식 자료와 함께 운영 상태를 검토해줘" \
  --official-domain www.company.com
```

호스트가 여러 곳이면 `--official-domain`을 반복합니다. Controller가 검색을 선택한 경우에만 검색 계획 → 실제 검색 → 원문 수집 → Admission → 외부 분석·교차 검증으로 이어집니다. 검색 제목이나 Snippet을 증빙으로 확정하지 않습니다.

검색 키나 허용 호스트가 없으면 그 이유를 기록하고 보유 PDF로 리포트를 만듭니다. 이는 외부 검증 성공을 뜻하지 않습니다. 자동 실행은 로컬 분석·원문 재검토 최대 두 차례와 외부 검색·검증 한 회차로 제한되며, 이후 미해결 작업은 후속 확인 사항으로 남습니다.

## 어떤 모델과 구조를 쓰나요?

계획·Controller·반증 검토·최종 Verifier는 **`gpt-6.1-sol`**, OCR·정보 추출·원문 및 의미 대조는 **`gpt-5.4-mini`**를 사용합니다. 해당 모델에 접근할 수 없으면 기술적 실패로 기록하며 다른 모델로 자동 대체하지 않습니다.

Responses API의 Function Calling으로 로컬 근거 검색과 실행 경로 선택을 연결합니다. Pydantic Structured Outputs와 기존 STEP의 검증 규칙으로 결과를 검사합니다. Sol에는 지원되는 `reasoning.effort=low`를 사용합니다. API 형식은 [OpenAI Function Calling 문서](https://developers.openai.com/api/docs/guides/function-calling)와 [모델 문서](https://developers.openai.com/api/docs/models/gpt-6.1-sol)를 따릅니다.

LangGraph가 Controller·분석·원문 재검토·외부 검색·반증·최종 Verifier의 실행 순서와 분기를 관리합니다. 계획, 쟁점, 실제 호출, 근거와 판정은 SQLite에 기록하고 그래프 상태는 SQLite 체크포인트로 저장합니다.

Qdrant에는 원문 구간과 페이지·문자 위치·출처·발행일·원문 해시·버전을 함께 저장합니다. 표와 각주는 OCR 원문 및 페이지 문맥과 함께 보존합니다. 검색은 로컬 BM25와 **`text-embedding-3-large`**의 dense 검색을 Qdrant RRF로 합칩니다. 검색 후보가 발견됐다는 이유로 검증된 근거 목록에 넣지 않습니다. BM25의 한국어 처리는 단어와 겹치는 두 글자 토큰을 사용합니다.

같은 내용의 임베딩과 해시가 일치하는 단계 산출물은 재사용합니다. 원본 PDF 대조는 페이지 이미지·OCR·Claim·검증 코드와 모델이 모두 같을 때 재사용하고, 달라진 페이지를 다시 검증합니다. PDF가 바뀌면 OCR·분석은 다시 수행합니다. 외부 자료의 Admission과 검증 계보도 매 실행에서 확인합니다. 같은 경로의 PDF는 버전 이력을 이어가며, 경로를 옮겨도 같은 문서로 관리하려면 `--document-id`를 지정하세요.

## 결과와 재개

실행이 끝나면 터미널에 표시된 `report.html` 경로를 브라우저에서 열어 주세요.

| 파일·폴더 | 내용 |
| --- | --- |
| `data/runtime/review_실행ID/pipeline.json` | 단계 상태, 실제 모델 요청·응답, 토큰 사용량과 실패 이유 |
| `review_실행ID/graph_manifest.json`, `workflow.mmd` | 입력·코드·모델 식별자, 체크포인트 위치와 실행 그래프 |
| `review_실행ID/stepN/` | 단계별 입력, 원응답, 검증 및 원문 기록 |
| `review_실행ID/step10/` 아래 리포트 폴더 | `report.html`, `report.json`, `evidence.csv`, 원문 사본 |
| `review_실행ID/counterevidence/`, `final-verifier/` | 반증 검토, 규칙 ID별 검사, 최종 Verifier 응답 |
| `data/runtime/.dc_verify/` | `ledger.sqlite`, `checkpoints.sqlite`, Qdrant 로컬 데이터 |

`--output-dir`을 지정하면 실행 폴더와 공유 저장소가 그 아래에 만들어집니다. 실행 성공과 검토 목적 달성은 별개입니다. 최종 Verifier의 `PASS`도 리포트가 근거와 한계를 정확하게 표현했다는 뜻입니다.

중단된 실행은 **같은 PDF·질문·옵션**에 `--resume`을 붙여 재개합니다. 입력·코드·모델·예산이 달라지거나 저장된 산출물의 해시가 바뀌면 재개를 거부합니다.

```bash
python DC_verify.py --pdf ./input.pdf --request "기존 검토 질문" \
  --as-of 2026-11-12 --resume ./data/runtime/review_실행ID
```

`--refresh`는 단계 산출물과 원문 대조 캐시 대신 다시 실행합니다. 내용이 같은 임베딩은 재사용합니다. 완료된 실행에 `--resume`하면 추가 모델 호출 없이 완료 결과를 반환합니다.

## 실행 제한과 오류 처리

허용 도구·공식 호스트·검토 기준일·예산을 Runtime Guard가 검사합니다. 수치·단위·원문 위치·인용·증빙 상태·규칙 ID는 코드와 Verifier가 대조합니다. 검증되지 않은 정보를 임의 환산하거나 최종 사실로 승격하지 않습니다.

```bash
python DC_verify.py --pdf ./input.pdf --request "검토 질문" \
  --max-calls 64 --max-tokens 2000000 --max-searches 8
```

모델 호출 상한에는 임베딩과 실패 후 재시도도 포함합니다. 토큰 예산은 요청 전 추정 입력·최대 출력 여유량과 응답의 보고 사용량으로 검사합니다. 외부 검색에는 전체 상한과 쟁점별 최대 2회 제한을 함께 적용합니다.

일시적 API 오류는 최대 2회 재시도합니다. 더 작은 예산이 있으면 그 제한을 먼저 적용합니다. 추출·인용 검증 오류는 실패 사유를 전달해 제한적으로 다시 실행하고, 남은 오류는 격리하거나 실행을 중단합니다. 인증·권한·접근·파싱 실패와 정보 미발견은 구분해 기록합니다. 중단 시 종료 코드 1, `BLOCKED` 기록과 실패 안내 HTML을 남깁니다.

## STEP 폴더와 코드

기존 STEP 폴더와 개별 실행 명령을 유지했습니다. `DC_verify.py`가 진입점이며 구현은 재사용할 수 있도록 모듈에 나누었습니다.

| 단계 | 역할과 통합 기능 |
| --- | --- |
| STEP 0 | 입력·API 확인, Runtime Guard·SQLite 초기화 |
| STEP 1 | PDF Vision OCR, 원문 버전·검색 인덱스 저장 |
| STEP 2 | Sol 초기 계획·검토 대상 식별 |
| STEP 3 | Pydantic Claim 추출·인용 검사·오류 재실행 |
| STEP 4 | 원본 PDF 대조·페이지별 변경 검사 |
| STEP 5 | 증빙 수준 평가 |
| STEP 6 | Controller 관찰·재계획, Function Calling·로컬 근거 검색·분기 |
| STEP 7 | 공식 자료 검색·다운로드·전처리·호출 예산 기록 |
| STEP 8 | Admission·외부 분석·원문/의미 검증·교차 비교 |
| STEP 9 | 반증 검토·Controller 종합 판단 |
| STEP 10 | 결과표·Human Review HTML·규칙 검사·최종 Verifier |

통합 그래프는 `src/dc_verify/graph_runner.py`, 저장과 검색은 `evidence_store.py`, Guard와 기록은 `runtime.py`, 통합 검증은 `verification.py`에 있습니다. 단계별 코드와 프롬프트는 `src/dc_verify/step/stepN/code/`에 있습니다.

```bash
./run.sh stages
./run.sh run analysis --help
./run.sh run report --help
./run.sh verify --help
./run.sh offline-demo
```

`offline-demo`는 합성 자료의 Guard만 검사합니다. 실제 모델·검색 실행과 구분됩니다.

## Windows

PowerShell에서 가상환경을 활성화하고 같은 Python 진입점을 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
Copy-Item API.env.example API.env
# API.env에 본인 키를 입력한 후 실행
python DC_verify.py --pdf .\input.pdf --request "검토 질문"
```

Windows용 명령과 경로 처리를 제공하며, 실제 실행 검사는 macOS에서 수행했습니다.
