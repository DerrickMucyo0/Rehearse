"""Fixed six-case development comparison. Live execution needs owner authorization."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
import socket
import time

from app.reasoning import InvalidDecision, ReasoningFailed, ReasoningTimeout, ReasoningUnavailable
from evals.interviewer.assessment_independent import InvalidAssessment
from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer.evaluate import Outcome, metrics
from evals.interviewer.stage2_model_match_provider import (
    CONFIG, EXPERIMENT_VERSION, MODELS, RETRIES, SUPER_MODEL, ULTRA_MODEL,
    MatchedStage2Service, PROVIDER_TIMEOUT_SECONDS)
from evals.interviewer.two_stage_challenge_contract import validate_stage2, map_stage2
from evals.interviewer.two_stage_challenge_live import (
    ANNOTATIONS_SHA256, reviewed_challenge_expectations)
from evals.interviewer.two_stage_challenge_provider import PROMPT_VERSION, STAGE2_INSTRUCTIONS

CASE_IDS = ('unsupported_claim-003', 'tradeoffs-001', 'injection-004',
            'strong_complete-001', 'multi_turn-001', 'injection-002')
CALL_ORDER = (
    (CASE_IDS[0], SUPER_MODEL), (CASE_IDS[0], ULTRA_MODEL),
    (CASE_IDS[1], ULTRA_MODEL), (CASE_IDS[1], SUPER_MODEL),
    (CASE_IDS[2], SUPER_MODEL), (CASE_IDS[2], ULTRA_MODEL),
    (CASE_IDS[3], ULTRA_MODEL), (CASE_IDS[3], SUPER_MODEL),
    (CASE_IDS[4], SUPER_MODEL), (CASE_IDS[4], ULTRA_MODEL),
    (CASE_IDS[5], ULTRA_MODEL), (CASE_IDS[5], SUPER_MODEL))
ROUTING_PREMISE = 'Owner-reviewed understandable/relevant content without a blocking factual prerequisite; no Stage 1 inference or synthesized Stage 1 result.'


def select_cases():
    return reviewed.select_development_cases(CASE_IDS)


def validate_cases(cases):
    if tuple(case.id for case in cases) != CASE_IDS:
        raise ValueError('Select exactly the frozen six development cases')
    first = reviewed.reviewed_expectations(cases)
    if any(not item.understandable_relevant or item.blocking_context_gap is not False
           for item in first):
        raise ValueError('Owner-reviewed Stage 2 premise required')
    return reviewed_challenge_expectations(cases)


async def _attempt(case, service, expected):
    start = time.perf_counter()
    row = {'case_id': case.id, 'model_id': service.model, 'valid': False,
           'expected_challenge_warranted': expected, 'predicted_challenge_warranted': None,
           'correct': False, 'expected_action': case.expected_action, 'mapped_action': None,
           'latency_ms': None, 'error': None, 'invalid_reason': None, 'json_reason': None,
           'schema_reason': None, 'failure_kind': None, 'http_status': None}
    try:
        result = validate_stage2(await asyncio.wait_for(
            service.decide(case.context()), timeout=PROVIDER_TIMEOUT_SECONDS))
        decision = map_stage2(result)
        row.update(valid=True, predicted_challenge_warranted=result.challenge_warranted,
                   correct=result.challenge_warranted is expected, mapped_action=decision.action)
    except InvalidDecision as error:
        row.update(error='invalid_output', invalid_reason=error.invalid_reason,
                   json_reason=error.json_reason,
                   schema_reason=error.schema_reason if isinstance(error, InvalidAssessment) else None)
    except (TimeoutError, ReasoningTimeout):
        row['error'] = 'timeout'
    except (ReasoningFailed, ReasoningUnavailable) as error:
        row['error'] = 'provider_error'
        if error.failure is not None:
            row.update(failure_kind=error.failure.failure_kind, http_status=error.failure.http_status)
    except Exception:
        row.update(error='provider_error', failure_kind='adapter_error')
    row['latency_ms'] = (time.perf_counter() - start) * 1000
    return row


def model_metrics(rows):
    timing = metrics([Outcome(row['case_id'], 'matched_stage_2', row['expected_action'],
        row['mapped_action'], row['error'], row['latency_ms']) for row in rows])
    valid = [row for row in rows if row['valid']]
    return {
        'attempted': len(rows), 'valid': len(valid), 'correct': sum(row['correct'] for row in rows),
        'challenge_true_correct': sum(row['correct'] for row in rows if row['expected_challenge_warranted']),
        'move_on_false_correct': sum(row['correct'] for row in rows if not row['expected_challenge_warranted']),
        'false_positive_challenges': sum(not row['expected_challenge_warranted']
                                        and row['predicted_challenge_warranted'] for row in valid),
        'missed_challenges': sum(row['expected_challenge_warranted']
                                 and not row['predicted_challenge_warranted'] for row in valid),
        'invalid_outputs': sum(row['error'] == 'invalid_output' for row in rows),
        'provider_errors': sum(row['error'] == 'provider_error' for row in rows),
        'timeouts': sum(row['error'] == 'timeout' for row in rows),
        'median_latency_ms': timing['median_latency_ms'], 'p95_latency_ms': timing['p95_latency_ms'],
        **{field + '_counts': dict(Counter(row[field] for row in rows if row[field] is not None))
           for field in ('invalid_reason', 'json_reason', 'schema_reason')}}


def paired_classification(super_row, ultra_row):
    if not super_row['valid'] and not ultra_row['valid']:
        return 'both failure'
    if not super_row['valid']:
        return 'Super failure'
    if not ultra_row['valid']:
        return 'Ultra failure'
    if super_row['correct'] and ultra_row['correct']:
        return 'both correct'
    if super_row['correct']:
        return 'Super only correct'
    if ultra_row['correct']:
        return 'Ultra only correct'
    return 'both wrong'


async def experiment_report(cases, services=None):
    expectations = validate_cases(cases)  # Before constructing providers or making calls.
    if services is None:
        services = {model: MatchedStage2Service(model) for model in MODELS}
    if set(services) != set(MODELS) or any(services[model].model != model for model in MODELS):
        raise ValueError('Matched providers must retain their explicit model identities')
    selected = {case.id: case for case in cases}
    attempts = []
    for case_id, model in CALL_ORDER:
        row = await _attempt(selected[case_id], services[model], expectations[case_id])
        row['attempt_index'] = len(attempts) + 1
        attempts.append(row)
    per_model = {model: [row for row in attempts if row['model_id'] == model] for model in MODELS}
    pairs = []
    for case_id in CASE_IDS:
        one, two = (next(row for row in per_model[model] if row['case_id'] == case_id) for model in MODELS)
        pairs.append({'case_id': case_id, 'expected_challenge_warranted': expectations[case_id],
                      'Super': one, 'Ultra': two, 'comparison': paired_classification(one, two)})
    comparison_counts = Counter(pair['comparison'] for pair in pairs)
    return {
        'experiment_version': EXPERIMENT_VERSION, 'prompt_version': PROMPT_VERSION,
        'prompt_sha256': hashlib.sha256(STAGE2_INSTRUCTIONS.encode()).hexdigest(),
        'dataset_version': reviewed.DATASET_VERSION,
        'dataset_sha256': hashlib.sha256(reviewed.DATASET.read_bytes()).hexdigest(),
        'annotations_sha256': ANNOTATIONS_SHA256,
        'annotation_status': 'project_owner_human_reviewed_approved',
        'split': 'development', 'repeats': 1, 'stage_1_invoked': False,
        'routing_premise': ROUTING_PREMISE, 'models': list(MODELS),
        'config': {**asdict(CONFIG), 'stream': False}, 'reasoning_budget_sent': False,
        'timeout_seconds_per_stage2_call': PROVIDER_TIMEOUT_SECONDS,
        'combined_interview_deadline_tested': False, 'retries': RETRIES,
        'attempted': len(attempts), 'call_order': [{'case_id': cid, 'model_id': model} for cid, model in CALL_ORDER],
        'model_metrics': {model: model_metrics(per_model[model]) for model in MODELS},
        'paired_summary': {'comparison_counts': dict(comparison_counts),
            'valid_pairs': sum(pair['Super']['valid'] and pair['Ultra']['valid'] for pair in pairs),
            'ultra_recoveries': comparison_counts['Ultra only correct'],
            'ultra_regressions': comparison_counts['Super only correct'],
            'unchanged_correct': comparison_counts['both correct'],
            'unchanged_wrong': comparison_counts['both wrong']},
        'pairs': pairs, 'attempts': attempts}


def dns_precheck():
    start = time.perf_counter()
    try:
        addresses = socket.getaddrinfo('integrate.api.nvidia.com', 443, type=socket.SOCK_STREAM)
        count = len({item[4][0] for item in addresses})
    except OSError:
        return {'hostname_resolution': 'failure', 'elapsed_ms': (time.perf_counter() - start) * 1000}
    return {'hostname_resolution': 'success', 'number_of_resolved_addresses': count,
            'elapsed_ms': (time.perf_counter() - start) * 1000}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('No requests made. Live comparison requires --live and owner authorization.')
    try:
        cases = select_cases()
        validate_cases(cases)
    except (ValueError, TypeError, KeyError, OSError):
        parser.error('Invalid frozen development selection/review. No requests made.')
    if not os.environ.get('NVIDIA_API_KEY', '').strip():
        parser.error('NVIDIA_API_KEY is not configured. No requests made.')
    dns = dns_precheck()
    if dns['hostname_resolution'] != 'success':
        print(json.dumps({'dns_precheck': dns, 'error': 'environment_network_restriction',
                          'provider_requests': 0}))
        return
    report = asyncio.run(experiment_report(cases))
    report['dns_precheck'] = dns
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
