"""Development-only two-stage evaluator; live runs require separate authorization."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os

from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer.evaluate import Outcome, evaluate, metrics
from evals.interviewer.two_stage_orchestration import TwoStageReasoner
from evals.interviewer.two_stage_provider import PROMPT_VERSION, ASSESSMENT_CONFIG, MODEL


def _judgment(expected, predicted, applicable, valid):
    return {'expected': expected, 'predicted': predicted, 'applicable': applicable,
            'correct': (valid and predicted is expected) if applicable else None}


async def experiment_report(cases, reasoner):
    expectations = reviewed.reviewed_expectations(cases)  # Before any stage calls.
    traces = []

    class CapturingService:
        async def decide(self, context):
            try:
                return await reasoner.decide(context)
            finally:
                trace = reasoner.last_trace
                traces.append({'stage_1': trace.stage_1.safe_dict(),
                               'stage_2': trace.stage_2.safe_dict(),
                               'route': trace.route, 'failure_stage': trace.failure_stage,
                               'latency_ms': trace.latency_ms})

    outcomes = await evaluate(cases, CapturingService(), repeats=1)
    rows = []
    for expected, outcome, trace in zip(expectations, outcomes, traces):
        row = asdict(outcome)
        row['failure_stage'] = trace['failure_stage']
        failed = trace[trace['failure_stage']] if trace['failure_stage'] else None
        row['schema_reason'] = failed['schema_reason'] if failed else None
        row['route'] = trace['route']
        row['expected_route'] = ('CLARIFY' if not expected.understandable_relevant
                                 else 'FOLLOW_UP' if expected.blocking_context_gap
                                 else 'CONTINUE_TO_STAGE_2')
        row['mapped_action_correct'] = outcome.predicted == outcome.expected
        row['expected_stage_2'] = (expected.understandable_relevant
                                   and expected.blocking_context_gap is False)
        row['unexpected_stage_2'] = (trace['stage_2']['attempted'] and not row['expected_stage_2'])
        row['stages'] = {stage: trace[stage] for stage in ('stage_1', 'stage_2')}
        first, second = row['stages']['stage_1'], row['stages']['stage_2']
        first['assessments'] = {
            'understandable_relevant': _judgment(expected.understandable_relevant,
                first['understandable_relevant'], True, first['valid']),
            'blocking_context_gap': _judgment(expected.blocking_context_gap,
                first['blocking_context_gap'], expected.blocking_context_gap is not None,
                first['valid'])}
        second['assessments'] = {'unresolved_reasoning_issue': _judgment(
            expected.unresolved_reasoning_issue, second['unresolved_reasoning_issue'],
            second['attempted'] and expected.unresolved_reasoning_issue is not None,
            second['valid'])}
        rows.append(row)

    stage_metrics = {}
    for stage, fields in (('stage_1', ('understandable_relevant', 'blocking_context_gap')),
                          ('stage_2', ('unresolved_reasoning_issue',))):
        observations = [row['stages'][stage] for row in rows]
        attempted = [item for item in observations if item['attempted']]
        # Existing latency/rate definitions, applied to actual stage attempts.
        stage_outcomes = []
        for row in rows:
            item = row['stages'][stage]
            if item['attempted']:
                stage_outcomes.append(Outcome(
                    row['case_id'], row['category'], row['expected'], None,
                    item['error'], item['latency_ms']))
        timing = metrics(stage_outcomes) if stage_outcomes else None
        stage_metrics[stage] = {
            'attempted': len(attempted), 'valid': sum(item['valid'] for item in attempted),
            'invalid_outputs': sum(item['error'] == 'invalid_output' for item in attempted),
            'provider_errors': sum(item['error'] == 'provider_error' for item in attempted),
            'timeouts': sum(item['error'] == 'timeout' for item in observations),
            'invalid_reason_counts': dict(Counter(item['invalid_reason'] for item in observations
                                                  if item['invalid_reason'] is not None)),
            'json_reason_counts': dict(Counter(item['json_reason'] for item in observations
                                               if item['json_reason'] is not None)),
            'schema_reason_counts': dict(Counter(item['schema_reason'] for item in observations
                                                 if item['schema_reason'] is not None)),
            'median_latency_ms': timing['median_latency_ms'] if timing else None,
            'p95_latency_ms': timing['p95_latency_ms'] if timing else None,
            'semantics': {}}
        for field in fields:
            applicable = [item['assessments'][field] for item in observations
                          if item['assessments'][field]['applicable']]
            correct = sum(item['correct'] for item in applicable)
            stage_metrics[stage]['semantics'][field] = {
                'correct': correct, 'applicable': len(applicable),
                'accuracy': correct / len(applicable) if applicable else None}
    stage_metrics['stage_1']['routing_correct'] = sum(
        row['route'] == row['expected_route'] for row in rows)
    for name, wanted, observed in (('false_blocking_context', False, True),
                                   ('missed_blocking_context', True, False)):
        judgments = [row['stages']['stage_1']['assessments']['blocking_context_gap']
                     for row in rows]
        eligible = [item for item in judgments if item['expected'] is wanted]
        stage_metrics['stage_1'][name] = {
            'observed': sum(item['predicted'] is observed for item in eligible),
            'eligible': len(eligible)}
    required = [row for row in rows if row['expected_stage_2']]
    stage_metrics['stage_2']['expected_route_coverage'] = {
        'attempted': sum(row['stages']['stage_2']['attempted'] for row in required),
        'valid': sum(row['stages']['stage_2']['valid'] for row in required),
        'expected': len(required)}
    stage_metrics['stage_2']['unexpected_visits'] = sum(row['unexpected_stage_2'] for row in rows)
    groups = {}
    for group, selected in (
        ('challenge_targets', [r for r in rows if r['expected'] == 'CHALLENGE']),
        ('original_anchors', [r for r in rows if r['case_id'] in reviewed.ORIGINAL_IDS
                             and r['expected'] != 'CHALLENGE']),
        ('boundaries', [r for r in rows if r['case_id'] in reviewed.BOUNDARY_IDS])):
        groups[group] = {'correct': sum(r['mapped_action_correct'] for r in selected),
                         'attempted': len(selected)}
    return {'dataset_version': reviewed.DATASET_VERSION,
            'dataset_sha256': hashlib.sha256(reviewed.DATASET.read_bytes()).hexdigest(),
            'boundary_cases_sha256': reviewed.BOUNDARIES_SHA256,
            'annotations_sha256': reviewed.ANNOTATIONS_SHA256,
            'annotation_status': 'project_owner_human_reviewed_approved',
            'prompt_version': PROMPT_VERSION, 'config_version': reviewed.CONFIG_VERSION,
            'model': MODEL, 'config': asdict(ASSESSMENT_CONFIG),
            'combined_deadline_seconds': 30, 'split': 'development', 'repeats': 1,
            'stage_call_attempts': sum(item['attempted'] for row in rows
                                       for item in row['stages'].values()),
            'metrics': metrics(outcomes), 'stage_metrics': stage_metrics,
            'groups': groups, 'outcomes': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case-id', nargs='+', required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('Live evaluation requires --live. No requests made.')
    try:
        cases = reviewed.select_development_cases(args.case_id)
        reviewed.reviewed_expectations(cases)
    except (ValueError, TypeError, KeyError, OSError):
        parser.error('Invalid reviewed development selection. No requests made.')
    if not os.environ.get('NVIDIA_API_KEY', '').strip():
        parser.error('NVIDIA_API_KEY is not configured. No requests made.')
    report = asyncio.run(experiment_report(cases, TwoStageReasoner()))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
