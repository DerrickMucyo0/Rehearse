"""Guarded development-only Ultra model comparison; live runs need authorization."""
import argparse
import asyncio
import json
import os

from evals.interviewer import blocking_context_live as reviewed
from evals.interviewer.two_stage_challenge_live import (
    experiment_report as challenge_report, reviewed_challenge_expectations)
from evals.interviewer.two_stage_challenge_ultra_orchestration import UltraReasoner
from evals.interviewer.two_stage_challenge_ultra_provider import (
    EXPERIMENT_VERSION, STAGE1_MODEL, STAGE2_MODEL)


async def experiment_report(cases, reasoner):
    report = await challenge_report(cases, reasoner)
    report['experiment_version'] = EXPERIMENT_VERSION
    # model denotes the Stage 2 candidate; both stage identities are explicit.
    report['model'] = STAGE2_MODEL
    report['stage_1_model'] = STAGE1_MODEL
    report['stage_2_model'] = STAGE2_MODEL
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
    print(json.dumps(asyncio.run(experiment_report(cases, UltraReasoner())), indent=2))


if __name__ == '__main__':
    main()
