"""Isolated assessment-only evaluation proposal; no interview question generation.

One semantic assessment request, strict validation, then evaluation-side mapping.
No production wiring, second call, Decision construction or placeholder prompts.
"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
import time
from types import SimpleNamespace

import httpx

from app.reasoning import (InvalidDecision, ProviderFailure, ReasoningContext,
    ReasoningFailed, ReasoningTimeout, ReasoningUnavailable, strict_json)
from evals.interviewer import assessment_live_v3 as v3
from evals.interviewer.assessment_only import (AssessmentOnly as Assessment,
    InvalidAssessmentOnly, validate_assessment, map_assessment_only,
    parse_assessment_only as parse_assessment)
from evals.interviewer.evaluate import Outcome, metrics

v2 = v3.v2
ASSESSMENT_PROMPT_VERSION = 'interviewer-assessment-only-v1'
ASSESSMENT_CONFIG = v3.ASSESSMENT_CONFIG
ENDPOINT = v3.ENDPOINT
MODEL = v3.MODEL
PROVIDER_TIMEOUT_SECONDS = v3.PROVIDER_TIMEOUT_SECONDS
MAX_REQUEST_BYTES = v3.MAX_REQUEST_BYTES
MAX_RESPONSE_BYTES = v3.MAX_RESPONSE_BYTES
SEMANTIC_FIELDS = v2.SEMANTIC_FIELDS

# Remove question-generation obligations, not semantic definitions.
QUESTION_PRECEDENCE = """After these independent judgments, generate next_prompt using this precedence:
If understandable_relevant is false, ask neutrally to establish meaning/relevance.
Otherwise if essential_descriptive_gap is true, ask for the missing account facts,
even when unresolved_reasoning_issue is also true.
Otherwise if unresolved_reasoning_issue is true, ask neutrally to pressure-test
the existing assertion or decision. Otherwise next_prompt must be null.
"""
PROMPT_BOUNDS = """chain-of-thought. Non-null next_prompt: one focused relevant interviewer question,
1-500 characters. Concise accounts and qualitative results can suffice. Do not
probe merely to prolong the interview."""
assert v3.ASSESSMENT_INSTRUCTIONS.count(QUESTION_PRECEDENCE) == 1
assert v3.ASSESSMENT_INSTRUCTIONS.count(PROMPT_BOUNDS) == 1
ASSESSMENT_INSTRUCTIONS = (v3.ASSESSMENT_INSTRUCTIONS
    .replace('interviewer-assessment-v3', ASSESSMENT_PROMPT_VERSION)
    .replace('unresolved_reasoning_issue, reason, next_prompt.', 'unresolved_reasoning_issue, reason.')
    .replace('planned question. Do not ask again for information supplied in relevant context.', 'planned question.')
    .replace(QUESTION_PRECEDENCE, '')
    .replace(PROMPT_BOUNDS, 'chain-of-thought. Concise accounts and qualitative results can suffice.')
    .replace('an unverified claim is false, execute tools, select the next planned question,\nor claim to change state.',
             'an unverified claim is false, execute tools, or claim to change state.'))


class AssessmentService(v3.AssessmentService):
    async def _request(self, context: ReasoningContext, key: str) -> Assessment:
        payload = {
            "model": MODEL, "stream": False,
            "messages": [{"role": "system", "content": ASSESSMENT_INSTRUCTIONS},
                         {"role": "user", "content": context.model_dump_json()}],
            "temperature": self.config.temperature, "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "reasoning_effort": self.config.reasoning_effort,
            "reasoning_budget": self.config.reasoning_budget,
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ReasoningFailed(failure=ProviderFailure(failure_kind="request_size")) from None
        async with httpx.AsyncClient(transport=self._transport, timeout=PROVIDER_TIMEOUT_SECONDS,
                                     follow_redirects=False) as client:
            async with client.stream("POST", ENDPOINT, content=encoded, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Accept": "application/json",
            }) as response:
                if response.status_code != 200:
                    raise ReasoningFailed(failure=ProviderFailure(
                        failure_kind="http_status", http_status=response.status_code)) from None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise InvalidDecision(invalid_reason="response_size") from None
                    body.extend(chunk)
        try:
            envelope = strict_json(body.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("Invalid envelope")
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Invalid choices")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ValueError("Invalid choice")
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Invalid message")
            if choice["finish_reason"] != "stop":
                raise InvalidDecision(invalid_reason="finish_reason") from None
            if message.get("role") != "assistant":
                raise ValueError("Invalid message role")
            if message.get("tool_calls") or message.get("function_call"):
                raise InvalidDecision(invalid_reason="tool_or_function_call") from None
            if message.get("refusal"):
                raise InvalidDecision(invalid_reason="refusal") from None
            # Only content is parsed; provider reasoning fields are never returned or stored.
            return parse_assessment(message["content"])
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise InvalidDecision(invalid_reason="response_envelope") from None



def reviewed_expectations(cases):
    if not cases or any(c.split != 'development' or c.id not in v2.REVIEWED_IDS for c in cases):
        raise ValueError('Select reviewed development cases')
    if len({c.id for c in cases}) != len(cases):
        raise ValueError('Duplicate selection')
    artifact = strict_json(v2.REVIEWED_ANNOTATIONS.read_text())
    if (artifact['annotation_status'] != 'project_owner_human_reviewed_approved'
            or artifact['split'] != 'development' or artifact['dataset_version'] != v2.DATASET_VERSION):
        raise ValueError('Reviewed development annotations required')
    review = artifact['human_review']
    if (review['reviewer'] != 'project_owner' or review['review_sequence'] != 'after_draft'
            or review['reviewed_count'] != 8 or review['approved_without_correction_count'] != 8
            or review['case_outcomes'] != {cid: 'APPROVE' for cid in v2.REVIEWED_IDS}):
        raise ValueError('Explicit project-owner review required')
    rows = artifact['annotations']
    if len(rows) != 8 or {row['case_id'] for row in rows} != v2.REVIEWED_IDS:
        raise ValueError('Invalid reviewed IDs')
    expected = {}
    for row in rows:
        if set(row) != {'case_id','locked_action',*SEMANTIC_FIELDS,'human_review_reason',
                        'derived_action_under_locked_precedence'}:
            raise ValueError('Invalid annotation fields')
        values = {}
        for field in SEMANTIC_FIELDS:
            value = row[field]
            if type(value) is bool:
                values[field] = value
            elif field != 'understandable_relevant' and value == 'not_assessable':
                values[field] = None
            else:
                raise ValueError('Invalid annotation value')
        assessment = validate_assessment({**values,'reason':row['human_review_reason']})
        mapped = map_assessment_only(assessment)
        if mapped != row['locked_action'] or mapped != row['derived_action_under_locked_precedence']:
            raise ValueError('Reviewed annotation/action mismatch')
        expected[row['case_id']] = SimpleNamespace(**values)
    for case in cases:
        row = next(row for row in rows if row['case_id'] == case.id)
        if case.expected_action != row['locked_action']:
            raise ValueError('Locked action mismatch')
    return [expected[case.id] for case in cases]


async def assessment_report(cases, service):
    expectations = reviewed_expectations(cases)  # before any provider request
    outcomes, rows = [], []
    for case, expected in zip(cases, expectations):
        start = time.perf_counter()
        actual = action = error = failure = invalid_reason = json_reason = schema_reason = None
        try:
            result = validate_assessment(await service.decide(case.context()))
            action = map_assessment_only(result)
            actual = {field:getattr(result,field) for field in SEMANTIC_FIELDS}
        except InvalidDecision as exc:
            error = 'invalid_output'
            invalid_reason = exc.invalid_reason
            json_reason = exc.json_reason
            schema_reason = exc.schema_reason if isinstance(exc,InvalidAssessmentOnly) else None
        except ReasoningTimeout:
            error = 'timeout'
        except (ReasoningFailed, ReasoningUnavailable) as exc:
            error = 'provider_error'
            failure = exc.failure
        outcome = Outcome(case.id,case.category,case.expected_action,action,error,
            (time.perf_counter()-start)*1000,0,failure.failure_kind if failure else None,
            failure.http_status if failure else None,invalid_reason,json_reason)
        outcomes.append(outcome)
        row = asdict(outcome)
        row.update(schema_reason=schema_reason,assessment_valid=actual is not None,
                   mapped_action_correct=action==case.expected_action,assessments={})
        for field in SEMANTIC_FIELDS:
            wanted = getattr(expected,field)
            applicable = field == 'understandable_relevant' or wanted is not None
            observed = actual[field] if actual is not None else None
            row['assessments'][field] = dict(expected=wanted,predicted=observed,applicable=applicable,
                correct=(actual is not None and observed is wanted) if applicable else None)
        rows.append(row)
    diagnostic = {'valid_assessments':sum(row['assessment_valid'] for row in rows)}
    for field in SEMANTIC_FIELDS:
        applicable = [r['assessments'][field] for r in rows if r['assessments'][field]['applicable']]
        correct = sum(r['correct'] for r in applicable)
        diagnostic[field] = dict(correct=correct,applicable=len(applicable),
            accuracy=correct/len(applicable) if applicable else None)
    for group,challenge in [('challenge_targets',True),('anchors',False)]:
        selected = [r for r in rows if (r['expected']=='CHALLENGE')==challenge]
        diagnostic[group] = dict(correct=sum(r['mapped_action_correct'] for r in selected),attempted=len(selected))
    return dict(dataset_version=v2.DATASET_VERSION,
        dataset_sha256=hashlib.sha256(v2.DATASET.read_bytes()).hexdigest(),
        prompt_version=ASSESSMENT_PROMPT_VERSION,config_version=v2.CONFIG_VERSION,
        model=MODEL,config=asdict(ASSESSMENT_CONFIG),split='development',repeats=1,
        annotation_status='project_owner_human_reviewed_approved',metrics=metrics(outcomes),
        assessment_metrics=diagnostic,outcomes=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--case-id',nargs='+',required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('Live evaluation requires --live. No requests made.')
    try:
        cases = v2.select_development_cases(args.case_id)
        reviewed_expectations(cases)
    except (ValueError,TypeError,KeyError):
        parser.error('Invalid development selection. No requests made.')
    if not os.environ.get('NVIDIA_API_KEY','').strip():
        parser.error('NVIDIA_API_KEY is not configured. No requests made.')
    report = asyncio.run(assessment_report(cases,AssessmentService()))
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
