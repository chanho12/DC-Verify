"""STEP10 development prompt only. Report generation does not call an LLM."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
CODEX_PROMPT = r"""
DC-Verify STEP10 — Generalized Evidence Review Report & Human Review UI
동일 계보의 최신 성공 STEP9와 이전 검증 산출물·원문 해시를 읽어 Python Report Builder를 구현하라.
새 Agent·LLM API·검색·Evidence DB·Controller 재판단은 실행하지 않는다.
회사·시설·수치·날짜·Claim ID를 코드에 고정하지 않는다.
기존 기록을 보존하며 다음을 리포트 파생 뷰에서 보완하라:
1. 제공·할당 용량, 설치·운영 가능 용량, 실제 소비 전력을 분리하고 미확인을 대체 수치로 채우지 않는다.
2. DEFER의 후속 작업은 DEFERRED/실행 중지로 표시한다.
3. 사람 검토는 전체 Claim Key·문서·원문 페이지로 연결한다.
4. 추가 검색 정보가치에 관한 이유는 정량 입증된 사실이 아닌 Controller 판단으로 표시한다.
5. 같은 수치와 같은 기준 시점을 구분하고 시점 미확인에 SAME을 사용하지 않는다.
요약·요건별 결과·Evidence Table·Source Trace·보류 이유·실사 질문을 포함한 독립 HTML과 JSON/CSV를 생성하라.
원본 PDF와 HTML 스냅샷에 연결하고 인용·잠정/격리 상태·공개일 불확실성을 보존하라.
로컬 검토 메모는 원래 검증 상태를 바꾸지 않는 별도 annotation으로 저장·내보내기만 허용하라.
실제 데이터 실행, 원본 불변 검사, 경계·회귀 테스트와 브라우저 화면 검수를 수행하라.
""".strip()

if __name__ == '__main__':
    print(CODEX_PROMPT)
