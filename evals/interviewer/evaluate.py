"""Offline-testable action metrics; live requests require an explicit CLI flag."""
import argparse
import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.nemotron import CONFIG_VERSION, MODEL, PROMPT_VERSION, InferenceConfig, NemotronInterviewerService
from app.reasoning import (ACTIONS, Action, Decision, FailureKind, InvalidDecision, InvalidReason, JsonReason, PriorTurn, ReasoningContext,
                           ReasoningFailed, ReasoningTimeout, ReasoningUnavailable, strict_json)

DATASET_VERSION = 'interviewer-cases-v2'
DATASET = Path(__file__).with_name('cases.jsonl')
CATEGORIES = ('strong_complete', 'vague', 'incomplete', 'ambiguous', 'irrelevant', 'unsupported_claim',
              'missing_contribution', 'missing_outcome', 'tradeoffs', 'concise_complete', 'multi_turn', 'injection')
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]


class Case(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    id: Annotated[str, StringConstraints(pattern=r'^[a-z_]+-\d{3}$')]
    split: Literal['development', 'held_out']
    category: Text
    question: Text
    current_prompt: Text
    prior_turns: Annotated[list[PriorTurn], Field(max_length=2)]
    answer: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    expected_action: Action
    label_reason: Text

    def context(self):
        return ReasoningContext(question=self.question, current_prompt=self.current_prompt,
                                prior_turns=tuple(self.prior_turns), answer=self.answer)


def load_cases(path=DATASET):
    cases = [Case.model_validate(strict_json(line)) for line in path.read_text().splitlines()]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError('Empty dataset or duplicate IDs')
    signatures = set()
    for case in cases:
        context = case.context()  # Apply the exact application input constraints.
        signature = context.model_dump_json()
        if signature in signatures or case.category not in CATEGORIES:
            raise ValueError('Duplicate scenario or unknown category')
        signatures.add(signature)
    return cases


@dataclass(frozen=True)
class Outcome:
    case_id: str
    category: str
    expected: str
    predicted: str | None
    error: str | None
    latency_ms: float
    repeat: int = 0
    failure_kind: FailureKind | None = None
    http_status: int | None = None
    invalid_reason: InvalidReason | None = None
    json_reason: JsonReason | None = None


def metrics(outcomes):
    count = len(outcomes)
    matrix = {a: {p: 0 for p in (*ACTIONS, 'ERROR')} for a in ACTIONS}
    for row in outcomes:
        matrix[row.expected][row.predicted or 'ERROR'] += 1
    per_action = {}
    for action in ACTIONS:
        support = sum(matrix[action].values())
        true_positive = matrix[action][action]
        predicted = sum(matrix[a][action] for a in ACTIONS)
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / support if support else 0.0
        per_action[action] = {'support': support, 'precision': precision, 'recall': recall,
                              'f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0}
    categories = {}
    for category in sorted({r.category for r in outcomes}):
        rows = [r for r in outcomes if r.category == category]
        categories[category] = {'attempted': len(rows),
                               'action_match_rate': sum(r.expected == r.predicted for r in rows) / len(rows)}
    latencies = sorted(r.latency_ms for r in outcomes)
    errors = Counter(r.error for r in outcomes if r.error)
    repeats = defaultdict(list)
    for row in outcomes:
        repeats[row.case_id].append(row.predicted)
    repeated = [values for values in repeats.values() if len(values) > 1]
    return {
        'attempted': count,
        'action_match_rate': sum(r.predicted == r.expected for r in outcomes) / count if count else 0.0,
        'per_action': per_action,
        'macro_f1': sum(v['f1'] for v in per_action.values()) / len(ACTIONS),
        'confusion_matrix': matrix, 'by_category': categories,
        **{f'{kind}_rate': errors[kind] / count if count else 0.0
           for kind in ('invalid_output', 'provider_error', 'timeout')},
        'median_latency_ms': statistics.median(latencies) if latencies else None,
        'p95_latency_ms': latencies[math.ceil(.95 * count) - 1] if count else None,
        'repeat_agreement': (sum(None not in v and len(set(v)) == 1 for v in repeated) / len(repeated)
                             if repeated else None),
    }


async def evaluate(cases, service, repeats=1, *, on_decision: Callable[[dict], None] | None = None):
    if not 1 <= repeats <= 10:
        raise ValueError('Repeats must be between 1 and 10')
    if on_decision is not None and (len(cases) != 1 or cases[0].split != 'development'):
        raise ValueError('Decision inspection requires exactly one development case')
    outcomes = []
    for repeat in range(repeats):
        for case in cases:
            start = time.perf_counter()
            action = error = None
            failure = None
            invalid_reason = json_reason = None
            try:
                decision = await service.decide(case.context())
                if on_decision is not None:
                    try:
                        decision = Decision.model_validate(decision)
                    except ValueError:
                        raise InvalidDecision(invalid_reason='schema_validation') from None
                action = decision.action
            except InvalidDecision as exc:
                error = 'invalid_output'
                invalid_reason = exc.invalid_reason
                json_reason = exc.json_reason
            except ReasoningTimeout:
                error = 'timeout'
            except (ReasoningFailed, ReasoningUnavailable) as exc:
                error = 'provider_error'
                failure = exc.failure
            if on_decision is not None and error is None:
                on_decision({'case_id': case.id, 'expected': case.expected_action,
                             'predicted': decision.action, 'reason': decision.reason,
                             'next_prompt': decision.next_prompt, 'repeat': repeat})
            outcomes.append(Outcome(case.id, case.category, case.expected_action, action, error,
                                    (time.perf_counter() - start) * 1000, repeat,
                                    failure.failure_kind if failure else None,
                                    failure.http_status if failure else None, invalid_reason, json_reason))
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow NVIDIA requests')
    parser.add_argument('--split', choices=['development', 'held_out'],
                        help='Split to evaluate (defaults to development without --case-id)')
    parser.add_argument('--case-id', metavar='CASE_ID', help='Evaluate exactly one case by ID')
    parser.add_argument('--show-decisions', action='store_true',
                        help='Inspect application decision text for one synthetic development case')
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=1)
    parser.add_argument('--reasoning-effort', choices=['none', 'low', 'high'], default='low')
    parser.add_argument('--reasoning-budget', type=int, default=256)
    parser.add_argument('--temperature', type=float, default=1.0)
    parser.add_argument('--top-p', type=float, default=.95)
    parser.add_argument('--max-tokens', type=int, default=1024)
    args = parser.parse_args()
    if not args.live:
        parser.error('No requests made. Live evaluation requires --live.')
    if args.show_decisions and args.case_id is None:
        parser.error('--show-decisions requires --case-id. No requests made.')
    if args.show_decisions and args.split == 'held_out':
        parser.error('--show-decisions is development-only. No requests made.')
    try:
        cases = load_cases()
    except ValueError:
        parser.error('Invalid evaluation dataset. No requests made.')
    if args.case_id is not None:
        cases = [c for c in cases if c.id == args.case_id]
        if not cases:
            parser.error(f'Unknown case ID: {args.case_id}. No requests made.')
        if args.split is not None and cases[0].split != args.split:
            parser.error(f'Case {args.case_id} belongs to {cases[0].split}, '
                         f'not {args.split}. No requests made.')
        args.split = cases[0].split
    else:
        args.split = args.split or 'development'
        cases = [c for c in cases if c.split == args.split]
    if args.show_decisions and (len(cases) != 1 or cases[0].split != 'development'):
        parser.error('--show-decisions requires exactly one development case. No requests made.')
    import os
    if not os.environ.get('NVIDIA_API_KEY', '').strip():
        parser.error('NVIDIA_API_KEY is not configured. No requests made.')
    try:
        config = InferenceConfig(reasoning_effort=args.reasoning_effort, reasoning_budget=args.reasoning_budget,
                                 temperature=args.temperature, top_p=args.top_p, max_tokens=args.max_tokens)
    except ValueError:
        parser.error('Invalid evaluation configuration or dataset.')
    inspections = []
    outcomes = asyncio.run(evaluate(cases, NemotronInterviewerService(config=config), args.repeats,
                                    on_decision=inspections.append if args.show_decisions else None))
    report = {
        'dataset_version': DATASET_VERSION, 'dataset_sha256': hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        'prompt_version': PROMPT_VERSION, 'config_version': CONFIG_VERSION,
        'model': MODEL, 'config': asdict(config), 'split': args.split, 'repeats': args.repeats,
        'metrics': metrics(outcomes), 'outcomes': [asdict(o) for o in outcomes],
    }
    if args.show_decisions:
        report['decision_inspection'] = inspections
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
