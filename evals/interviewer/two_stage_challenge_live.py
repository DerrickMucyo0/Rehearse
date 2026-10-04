"""Separately versioned, reviewed development diagnostic; no production integration."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from app.reasoning import strict_json
from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer.two_stage_challenge_orchestration import ChallengeReasoner
from evals.interviewer.two_stage_challenge_provider import CHALLENGE_DEFINITION, PROMPT_VERSION
from evals.interviewer.two_stage_live import experiment_report as historical_report, _judgment
from evals.interviewer.two_stage_provider import PROMPT_VERSION as STAGE1_PROMPT_VERSION

ANNOTATIONS = Path(__file__).with_name('two_stage_challenge_annotations_development.json')
ANNOTATIONS_SHA256 = '4f0348c3cf0b422745668f15a28fd3e88943439c2b29452d020e5faabd1867f3'
STAGE2_IDS = frozenset(('unsupported_claim-003', 'tradeoffs-001', 'injection-004',
                      'strong_complete-001', 'multi_turn-001', 'injection-002'))


def reviewed_challenge_expectations(cases):
    # Source and Stage 1 owner review remain the existing immutable guard.
    reviewed.reviewed_expectations(cases)
    content = ANNOTATIONS.read_bytes()
    if hashlib.sha256(content).hexdigest() != ANNOTATIONS_SHA256:
        raise ValueError('Reviewed Stage 2 artifact changed')
    artifact = strict_json(content.decode('utf-8'))
    expected_metadata = {
        'annotation_status': 'project_owner_human_reviewed_approved',
        'reviewer': 'project_owner', 'split': 'development',
        'prompt_version': PROMPT_VERSION, 'dataset_version': reviewed.DATASET_VERSION,
        'challenge_definition': CHALLENGE_DEFINITION}
    if any(artifact.get(key) != value for key, value in expected_metadata.items()):
        raise ValueError('Explicit Stage 2 owner review required')
    rows = artifact['annotations']
    if len(rows) != 6 or {row['case_id'] for row in rows} != STAGE2_IDS:
        raise ValueError('Invalid reviewed Stage 2 annotations')
    expected = {}
    selected = {case.id: case for case in cases}
    for row in rows:
        signal = row['challenge_warranted']
        action = 'CHALLENGE' if signal else 'MOVE_ON'
        if (row['review_outcome'] != 'APPROVE' or type(signal) is not bool
                or row['mapped_action'] != action):
            raise ValueError('Invalid reviewed challenge mapping')
        if row['case_id'] in selected:
            case = selected[row['case_id']]
            digest = hashlib.sha256(case.context().model_dump_json().encode()).hexdigest()
            if digest != row['context_sha256'] or case.expected_action != action:
                raise ValueError('Reviewed Stage 2 source/action mismatch')
        expected[row['case_id']] = signal
    return expected


async def experiment_report(cases, reasoner):
    expectations = reviewed_challenge_expectations(cases)  # Before any calls.
    report = await historical_report(cases, reasoner)
    # The historical operation's Boolean is only a private action carrier here.
    # Replace its entire semantic projection; never reuse historical expectations.
    report['prompt_version'] = PROMPT_VERSION
    report['stage_1_prompt_version'] = STAGE1_PROMPT_VERSION
    report['stage_2_annotations_sha256'] = ANNOTATIONS_SHA256
    for row in report['outcomes']:
        row['stages']['stage_1'].pop('unresolved_reasoning_issue')
        second = row['stages']['stage_2']
        second['challenge_warranted'] = second.pop('unresolved_reasoning_issue')
        wanted = expectations.get(row['case_id'])
        second['assessments'] = {'challenge_warranted': _judgment(
            wanted, second['challenge_warranted'],
            second['attempted'] and wanted is not None, second['valid'])}
    judgments = [row['stages']['stage_2']['assessments']['challenge_warranted']
                 for row in report['outcomes']]
    applicable = [item for item in judgments if item['applicable']]
    correct = sum(item['correct'] for item in applicable)
    report['stage_metrics']['stage_2']['semantics'] = {'challenge_warranted': {
        'correct': correct, 'applicable': len(applicable),
        'accuracy': correct / len(applicable) if applicable else None}}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--case-id', nargs='+', required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('Live evaluation requires --live. No requests made.')
    try:
        cases = reviewed.select_development_cases(args.case_id)
        reviewed_challenge_expectations(cases)
    except (ValueError, TypeError, KeyError, OSError):
        parser.error('Invalid reviewed development selection. No requests made.')
    if not os.environ.get('NVIDIA_API_KEY', '').strip():
        parser.error('NVIDIA_API_KEY is not configured. No requests made.')
    print(json.dumps(asyncio.run(experiment_report(cases, ChallengeReasoner())), indent=2))


if __name__ == '__main__':
    main()
