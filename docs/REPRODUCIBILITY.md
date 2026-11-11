# 재현 범위와 검사

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
dc-verify stages
dc-verify offline-demo --output-dir data/runtime/demo
python -m unittest discover -s tests -v
```

설치에 필요한 직접 라이브러리는 setup.cfg에 실제 Import 기준으로 명시했다. requirements-tested.txt는 확인한 직접 의존성 버전이며 완전한 잠금 파일은 아니다. 네트워크 격리 검증에서는 기존 설치의 제3자 의존성만 독립 임시 환경에 복사하고, 로컬에서 빌드한 wheel을 --no-deps --no-index로 설치했다. 원본 STEP 코드 경로나 PYTHONPATH를 참조하지 않고 임시 작업 디렉터리에서 실행한다. 온라인 새 의존성 해석·다운로드는 수행하지 않았다.

manifests/release_validation.json에 실제 Import·설치·문법·CLI·합성 데모·Schema/Guard·원본 불변·공개 스캔 결과와 테스트 성공/실패/skip 수를 기록한다. raw 임시 경로가 나오는 빌드/테스트 로그는 배포 폴더 밖에 보관하고 공개 보고에는 상대 경로/모듈 이름과 요약만 둔다.

원문이 필요한 기존 테스트는 private artifact가 없으므로 명시적으로 skip한다. skip은 통과 수에 더하지 않는다. 합성 Guard 데모는 미리 작성한 Claim의 원문 인용·스키마·Admission 상태·DEFER 표시를 검사하며 실제 모델 추출/원본 이미지 검증/검색/Controller 판단 실행은 아니다. 새로운 실사에 기존 회사 데이터가 자동 적재되거나 Resident Store에 들어가지 않는다.

실제 사례를 재현하려면 사용자가 합법적으로 보유한 원문·각 단계의 정상 입력과 전체 계보를 로컬 runtime에 제공해야 한다. 과거 산출물의 해시와 절대 경로를 무작정 바꾸면 Guard가 차단할 수 있다. 현재 패키지가 임의 자료로 전체 Pipeline을 자동 구성하는 기능은 없다.

## 실제 독립 검사 결과

공개 데이터만 사용하는 독립 환경에서는 234개 테스트가 성공했고 실패·오류는 0개다. tests_run=325이며 skip 기록 94건(원문 의존 테스트 91개·setUpClass 3건)은 성공 수와 별도로 집계한다. 실제 보유 데이터를 로컬에서 연결한 재검사에서는 356개 모두 성공했고 skip은 0개다. run.sh 29건과 이동된 폴더의 실행도 통과했다. 새 API/검색은 하지 않았다. 자세한 범위와 수정은 [RECHECK_REPORT.md](RECHECK_REPORT.md)를 참고한다.
