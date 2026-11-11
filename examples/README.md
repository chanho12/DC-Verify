# 중립 합성 예제

examples/synthetic의 JSON은 SYNTHETIC_TEST_DATA입니다. 실제 문서·기업·API 응답이 아닙니다. analysis_result는 모델이 생성하지 않습니다.

```bash
dc-verify offline-demo --output-dir data/runtime/demo
dc-verify run analysis --input examples/synthetic/analysis_input.json --request-config examples/synthetic/analysis_request.json --output-dir data/runtime/analysis --dry-run
```

두 번째 명령은 실제 Analysis 코드의 입력 준비만 수행하며 모델을 호출하지 않습니다. --dry-run을 빼면 사용자가 직접 API를 실행합니다. 출력은 data/runtime에만 둡니다.
