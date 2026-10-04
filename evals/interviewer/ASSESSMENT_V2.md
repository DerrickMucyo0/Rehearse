# Independent assessment v2 — evaluation only

`assessment_live_v2.py` is isolated from production and assessment-v1. No v2
provider run has been authorized at implementation time. Explicit `--live` and
reviewed development case IDs are required; selections and owner review are
validated before provider construction. Production sessions import neither path.

The policy version is `interviewer-assessment-v2`. It assesses descriptive gaps
and reasoning issues independently. The model never emits an action. Sampling,
transport, envelope parsing, timeout and sanitized provider diagnostics remain
as in v1, with no retries or undocumented request parameters.

## Contract and mapping

The five required fields are understandable_relevant (strict boolean),
essential_descriptive_gap and unresolved_reasoning_issue (strict boolean or
JSON null), reason (trimmed 1–300 characters), and next_prompt (trimmed 1–500
characters or null). Extra fields are forbidden; instances are immutable and
revalidated at boundaries.

Null explicitly means not assessable; it is never converted to false. When
understandable_relevant=true, both later fields must be determinate booleans.
When false, each later field may independently be true, false or null if
responsible semantic assessment permits it. There are 13 valid semantic
combinations: nine for false understandability/relevance, four for true.

Rehearse maps false understandability/relevance to CLARIFY; otherwise a true
descriptive gap to FOLLOW_UP; otherwise a true reasoning issue to CHALLENGE;
otherwise MOVE_ON. Both issues true maps to FOLLOW_UP. Only MOVE_ON accepts
next_prompt=null. The mapping interprets no semantic text.

## Human review

The existing annotation file retains its original filename for continuity.
Its status is now project_owner_human_reviewed_approved: the project owner
explicitly approved all eight draft annotations without correction after the
draft was produced. This was one reviewer, not multiple independent reviews.
Original dataset action labels and all eight annotation values/reasons are
unchanged. The sheet's not_assessable string converts explicitly to JSON null
for v2 validation/evaluation, including the two later fields for irrelevant-002.
The reviewed false reasoning judgment for missing_outcome-004 is now scored;
it is not masked by the descriptive-gap judgment.

## Sanitized schema diagnostics

V2 outcomes add nullable schema_reason. It is populated only with
invalid_reason=schema_validation. Production and v1 diagnostics are unchanged.
The closed categories, in first-match priority order, are:

1. missing_required_field: Pydantic missing
2. forbidden_extra_field: extra_forbidden
3. strict_type_violation: bool_type or string_type
4. text_bound_violation: string_too_short or string_too_long
5. invalid_assessment_combination: a required later judgment is null
6. assessment_prompt_inconsistency: null/non-null prompt violates mapped action
7. other_schema_violation: remaining schema error types

This classifies existing v2 schema rejection; it does not change acceptance.
Only safe error categories survive. No Pydantic dictionaries, paths, messages,
input values, response content, source interview content, reason/next_prompt
text, hidden reasoning, headers or credentials enter reports. Syntax/duplicate
key/non-JSON constant handling remains strict and unchanged. Successful
outcomes, timeouts and provider errors have null schema_reason.

Expected applicability uses human-reviewed annotations: all cases for the first
field, non-null expectations for later fields. Failures or skipped expected
judgments count as incorrect. No future baseline threshold is defined here.
