"""Deterministic goal-aware pair selection. No API, inferred metrics or new facts."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import calendar
import hashlib
import itertools
import re
from collections import defaultdict
from datetime import date


EVENTS = {'planned_service_start': 'SERVICE_START', 'service_start': 'SERVICE_START',
          'planned_completion': 'COMPLETION', 'completion': 'COMPLETION'}
STATES = {'contract_ratio': 'CONTRACT', 'contract_status': 'CONTRACT', 'occupancy_rate': 'OCCUPANCY'}


def tokens(text):
    words = re.findall(r'[a-z][a-z0-9]+|[가-힣]{2,}', text.lower())
    result = set(words)
    for w in words:
        if re.fullmatch('[가-힣]+', w):
            result.update(w[i:i + 2] for i in range(len(w) - 1))
    return result - {'현재', '확인', '검토', '관련', '정보', 'data', 'center', 'the', 'and', 'of', 'for'}


def overlap(left, right):
    a, b = tokens(left), tokens(right)
    return len(a & b) / max(1, len(a | b))


def family(item):
    c = item['claim']
    t = c['metric_type']
    if t in EVENTS:
        return EVENTS[t]
    if t in STATES:
        return STATES[t]
    return t


def is_capacity(item):
    c = item['claim']
    return c['metric_type'].endswith('_capacity') or c['metric_type'] in {'expected_power_usage', 'actual_it_power_usage'} or (
        c['metric_type'] == 'other' and bool(re.fullmatch(r'[kmg]?w|[km]?va', c.get('unit') or '', re.I)))


def explicit_time(item):
    """Intervals retain precision. Publication/cover dates never fill metric_as_of."""
    c = item['claim']
    raw = c.get('metric_as_of')
    basis = 'METRIC_AS_OF'
    if not raw and c['metric_type'] in EVENTS:
        raw, basis = c['raw_value'], 'EXPLICIT_EVENT_EXPRESSION'
    if not raw:
        return {'status': 'UNKNOWN', 'raw': None, 'basis': None}
    # Multiple distinct years/months in one event description cannot be collapsed.
    years = set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', raw))
    if len(years) != 1:
        return {'status': 'UNKNOWN', 'raw': raw, 'basis': basis}
    year = int(next(iter(years)))
    q = re.search(r'(?:Q\s*([1-4])|([1-4])\s*분기)', raw, re.I)
    h = re.search(r'([12])\s*H|H\s*([12])|([상하])반기', raw, re.I)
    iso = re.search(r'((?:19|20)\d{2})-(\d{2})(?:-(\d{2}))?', raw)
    months = re.findall(r'(?<!\d)(1[0-2]|[1-9])\s*월', raw)
    if len(set(months)) > 1:
        return {'status': 'UNKNOWN', 'raw': raw, 'basis': basis}
    if q:
        n = int(q.group(1) or q.group(2)); first, last, precision = n * 3 - 2, n * 3, 'QUARTER'
    elif h:
        n = int(h.group(1) or h.group(2) or ('1' if h.group(3) == '상' else '2'))
        first, last, precision = (1, 6, 'HALF_YEAR') if n == 1 else (7, 12, 'HALF_YEAR')
    elif iso:
        first = last = int(iso.group(2)); precision = 'DAY' if iso.group(3) else 'MONTH'
        if iso.group(3):
            d = date(year, first, int(iso.group(3))).isoformat()
            return {'status': 'EXPLICIT', 'start': d, 'end': d, 'precision': precision, 'raw': raw, 'basis': basis}
    elif months:
        first = last = int(months[0]); precision = 'MONTH'
    else:
        first, last, precision = 1, 12, 'YEAR'
    return {'status': 'EXPLICIT', 'start': date(year, first, 1).isoformat(),
        'end': date(year, last, calendar.monthrange(year, last)[1]).isoformat(),
        'precision': precision, 'raw': raw, 'basis': basis}


def time_relation(left, right):
    a, b = explicit_time(left), explicit_time(right)
    if a['status'] == 'UNKNOWN' or b['status'] == 'UNKNOWN':
        relation = 'UNKNOWN'
    elif a['start'] == b['start'] and a['end'] == b['end']:
        relation = 'SAME_INTERVAL'
    elif a['end'] < b['start']:
        relation = 'LEFT_BEFORE_RIGHT'
    elif b['end'] < a['start']:
        relation = 'RIGHT_BEFORE_LEFT'
    else:
        relation = 'OVERLAPPING_DIFFERENT_PRECISION'
    return {'relation': relation, 'left': a, 'right': b}


def candidate_kind(left, right):
    a, b = left['claim'], right['claim']
    fa, fb = family(left), family(right)
    if a['project_name'] != b['project_name']:
        return None
    if fa == fb and fa != 'other':
        if fa in {'SERVICE_START', 'COMPLETION'}:
            return 'EVENT_PROGRESS'
        if fa == 'CONTRACT':
            return 'STATE_PROGRESS'
        return 'SAME_METRIC'
    if {fa, fb} == {'CONTRACT', 'OCCUPANCY'}:
        return 'STATE_BOUNDARY'
    if is_capacity(left) and is_capacity(right):
        return 'METRIC_BOUNDARY'
    if fa == fb == 'other':
        text_a = a['metric_name'] + ' ' + a['raw_value']
        text_b = b['metric_name'] + ' ' + b['raw_value']
        if overlap(text_a, text_b) >= 0.23 or a['evidence_quote'] == b['evidence_quote']:
            return 'DEFINITION_CONTEXT'
    return None


def soft_diagnostic(item):
    c = item['claim']
    return (item['original_group'] != 'QUARANTINED' and item['source_status'] == 'VERIFIED'
        and item['semantic_status'] == 'UNCLEAR' and c['metric_type'] in {'occupancy_rate', 'contract_ratio'} and c.get('value') is None)


def make_pair(left, right, kind, context):
    a, b = left['claim'], right['claim']
    keys = sorted([left['claim_key'], right['claim_key']])
    same_unit = bool(a.get('unit') and b.get('unit') and a['unit'].casefold() == b['unit'].casefold())
    same_number = same_unit and a.get('value') is not None and a['value'] == b.get('value')
    relevant = tokens(' '.join(context['required_metrics'] + context.get('supporting_metrics', [])))
    terms = tokens(' '.join([a['metric_name'], a['raw_value'], b['metric_name'], b['raw_value']]))
    relevance = sorted(relevant & terms)
    temporal = time_relation(left, right)
    eligible = all(x['comparison_eligibility'] != 'DIAGNOSTIC_ONLY' for x in [left, right])
    soft = not eligible and any(soft_diagnostic(x) for x in [left, right]) and all(
        x['comparison_eligibility'] != 'DIAGNOSTIC_ONLY' or soft_diagnostic(x) for x in [left, right])
    mode = 'STATEMENT_COMPARISON' if eligible else 'AMBIGUITY_DIAGNOSTIC' if soft else 'LOCAL_DIAGNOSTIC'
    score = (8 if left['origin'] != right['origin'] else 0) + (4 if kind in {'SAME_METRIC', 'STATE_PROGRESS', 'EVENT_PROGRESS'} else 0)
    score += min(3, len(relevance)) + int(same_number) * 2 + int(temporal['relation'] != 'UNKNOWN')
    return {'pair_id': 'pair_' + hashlib.sha256('\0'.join(keys).encode()).hexdigest()[:18],
        'left_key': left['claim_key'], 'right_key': right['claim_key'], 'kind': kind, 'mode': mode,
        'score': score, 'goal_relevance_terms': relevance, 'same_explicit_number_and_unit': same_number,
        'metric_types': [a['metric_type'], b['metric_type']], 'families': [family(left), family(right)],
        'time_comparison': temporal, 'final_evidence_allowed': False,
        'selection_reason': 'Controller-selected original Claim or overlapping goal terms and typed metric/state relationship'}


def select_pairs(data, max_pairs=24, max_diagnostics=12):
    if max_pairs < 1 or max_diagnostics < 0:
        raise ValueError('Pair budgets must be positive/nonnegative')
    inventory = data['claims']
    candidates = []
    for a, b in itertools.combinations(inventory, 2):
        if a['claim']['source_document_id'] == b['claim']['source_document_id'] or a['document']['source_sha256'] == b['document']['source_sha256']:
            continue
        kind = candidate_kind(a, b)
        if kind:
            p = make_pair(a, b, kind, data['review_context'])
            # Both inputs already belong to the same validated Controller goal.
            # Typed relationships also work across languages without company keywords.
            typed_lineage = all(t != 'other' for t in p['metric_types'])
            if p['goal_relevance_terms'] or a['origin'] == 'USER' or b['origin'] == 'USER' or typed_lineage:
                p['typed_controller_goal_lineage'] = typed_lineage
                candidates.append(p)
    order = lambda p: (-p['score'], p['pair_id'])
    # One best pair for each ambiguous textual ratio; never treat it as approved evidence.
    soft, soft_keys = [], set()
    lookup = {c['claim_key']: c for c in inventory}
    for p in sorted((p for p in candidates if p['mode'] == 'AMBIGUITY_DIAGNOSTIC' and p['kind'] == 'SAME_METRIC'
                     and any(lookup[k]['comparison_eligibility'] != 'DIAGNOSTIC_ONLY' for k in [p['left_key'], p['right_key']])), key=order):
        unclear = {k for k in [p['left_key'], p['right_key']] if soft_diagnostic(lookup[k])}
        if not unclear <= soft_keys and len(soft) < min(2, max_pairs):
            soft.append(p); soft_keys |= unclear
    # Round robin across comparison types/metric families prevents one common metric consuming the budget.
    groups = defaultdict(list)
    for p in candidates:
        if p['mode'] == 'STATEMENT_COMPARISON':
            groups[(p['kind'], tuple(sorted(p['families'])))].append(p)
    for values in groups.values():
        values.sort(key=order)
    selected = list(soft)
    doc_pairs = defaultdict(int)
    kind_counts = defaultdict(int)
    caps = {'METRIC_BOUNDARY': 6, 'SAME_METRIC': 5, 'EVENT_PROGRESS': 5,
        'STATE_PROGRESS': 4, 'STATE_BOUNDARY': 3, 'DEFINITION_CONTEXT': 2}
    while len(selected) < max_pairs:
        added = False
        for key in sorted(groups, key=lambda k: order(groups[k][0]) if groups[k] else (9999, '')):
            group = groups[key]
            while group:
                p = group.pop(0)
                if kind_counts[p['kind']] >= caps[p['kind']]:
                    continue
                doc_key = tuple(sorted([lookup[p['left_key']]['claim']['source_document_id'], lookup[p['right_key']]['claim']['source_document_id']]))
                if doc_pairs[(doc_key, p['kind'])] < 2:
                    selected.append(p); doc_pairs[(doc_key, p['kind'])] += 1; kind_counts[p['kind']] += 1; added = True
                    break
            if len(selected) >= max_pairs:
                break
        if not added:
            break
    diagnostics, seen = [], set()
    for p in sorted((p for p in candidates if p['mode'] == 'LOCAL_DIAGNOSTIC'), key=order):
        bad = [lookup[k] for k in [p['left_key'], p['right_key']] if lookup[k]['comparison_eligibility'] == 'DIAGNOSTIC_ONLY']
        key = tuple(sorted((c['claim']['metric_type'], c['source_status'], c['semantic_status'], c['original_group']) for c in bad))
        if key not in seen and len(diagnostics) < max_diagnostics:
            diagnostics.append(p); seen.add(key)
    used = {p['pair_id'] for p in selected + diagnostics}
    paired = {k for p in selected + diagnostics for k in [p['left_key'], p['right_key']]}
    return {'candidate_count': len(candidates), 'selected_pairs': selected, 'local_diagnostics': diagnostics,
        'candidates': candidates, 'max_pairs': max_pairs, 'max_diagnostics': max_diagnostics,
        'unselected_pair_ids': [p['pair_id'] for p in candidates if p['pair_id'] not in used],
        'unpaired_claims': [{'claim_key': c['claim_key'], 'comparison_eligibility': c['comparison_eligibility'],
            'reason': 'No cross-document goal-relevant counterpart selected within deterministic budgets; no absence-of-fact inference.'}
            for c in inventory if c['claim_key'] not in paired]}
