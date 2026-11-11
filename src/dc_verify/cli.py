"""Run a review workflow or an individual existing stage."""
import argparse
import importlib
import json
import sys
from datetime import date


def publication_date(value):
    from dc_verify.step.step8.code.source_admission import date_window
    date_window(value)
    return value

STAGES = {
    'ocr':'step1.code.OCR', 'plan':'step2.code.controller', 'analysis':'step3.code.analysis',
    'source-verification':'step4.code.source_verifier', 'evidence':'step5.code.assess_evidence',
    'replan':'step6.code.controller_replan', 'search-plan':'step7.code.search_planner',
    'search-execute':'step7.code.search_executor', 'search-api':'step7.code.search_api_adapter',
    'fetch':'step7.code.search_fetcher', 'prepare-documents':'step7.code.document_preparer',
    'admission':'step8.code.source_admission', 'external-analysis':'step8.code.external_analysis',
    'external-verification':'step8.code.external_verification', 'cross-check':'step8.code.cross_source',
    'controller':'step9.code.controller', 'report':'step10.code.report_builder'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('stages',help='List implemented stage commands; no API request')
    sub.add_parser('check-config',help='Check key configuration only; never calls API')
    demo=sub.add_parser('offline-demo',help='Synthetic data through existing pure guards, no Agent execution')
    demo.add_argument('--output-dir')
    verify=sub.add_parser('verify',help='PDF + request through connected stages to a final review report')
    verify.add_argument('--pdf',required=True)
    verify.add_argument('--request',required=True,help='검토 질문')
    verify.add_argument('--target',dest='targets',action='append',help='검토 대상; 생략 시 Controller가 요청과 OCR에서 식별')
    verify.add_argument('--as-of',dest='as_of',type=lambda s:date.fromisoformat(s).isoformat(),help='검토 기준일 YYYY-MM-DD')
    verify.add_argument('--published-at',dest='published_at',type=publication_date,help='사용자가 확인한 문서 발행일; 미입력 시 미확인')
    verify.add_argument('--official-domain',dest='official_domains',action='append',help='Tavily 검색/수집을 허용할 공식 호스트')
    verify.add_argument('--output-dir')
    verify.add_argument('--document-id')
    verify.add_argument('--pages',type=lambda s:[int(n) for n in s.split(',')],help='Controller가 허용한 분석 페이지 중 선택; 생략 시 전부')
    run=sub.add_parser('run',help='Run exactly one existing stage; inspect its --help first')
    run.add_argument('stage',choices=sorted(STAGES));run.add_argument('stage_args',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    if args.command=='verify':
        from dc_verify.pipeline import execute
        options=vars(args).copy();options.pop('command')
        return 0 if execute(**options)['status'] in {'SUCCESS','SUCCESS_WITH_WARNINGS'} else 1
    if args.command=='stages':print(json.dumps(STAGES,indent=2));return 0
    if args.command=='offline-demo':
        from dc_verify.demo import execute
        from dc_verify.settings import runtime_root
        print(json.dumps(execute(args.output_dir or runtime_root()/'synthetic_demo'),ensure_ascii=False,indent=2));return 0
    if args.command=='check-config':
        from dc_verify.codex import load_config,config_summary,safe_error
        try:result=config_summary(load_config())
        except Exception as error:print(json.dumps(safe_error(error),ensure_ascii=False));return 1
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    module=importlib.import_module('dc_verify.step.'+STAGES[args.stage])
    previous=sys.argv
    tail=args.stage_args[1:] if args.stage_args[:1]==['--'] else args.stage_args
    try:
        sys.argv=['dc-verify run '+args.stage,*tail]
        return module.main() or 0
    finally:sys.argv=previous


if __name__=='__main__':raise SystemExit(main())
