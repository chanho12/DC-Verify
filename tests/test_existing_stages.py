"""Reuse existing tests; private artifact dependent cases explicitly skip, never fabricate originals."""
import unittest

MODULES = {
    3:['test_analysis'],4:['test_source_verifier'],5:['test_assess_evidence'],6:['test_controller_replan'],
    7:['test_search_planner','test_search_executor','test_document_preparer'],
    8:['test_source_admission','test_external_analysis','test_external_verification','test_cross_source'],
    9:['test_controller'],10:['test_report_builder']}


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    for number, names in MODULES.items():
        for name in names:suite.addTests(loader.loadTestsFromName(f'dc_verify.step.step{number}.code.{name}'))
    return suite
