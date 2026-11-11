"""Dispatch one existing stage command. This is not an end-to-end workflow runner."""
import argparse
import importlib
import json
import sys

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
    run=sub.add_parser('run',help='Run exactly one existing stage; inspect its --help first')
    run.add_argument('stage',choices=sorted(STAGES));run.add_argument('stage_args',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
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
