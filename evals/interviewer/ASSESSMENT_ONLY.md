# Assessment-only-v1 — offline-reviewed evaluation proposal

## Current status — completed and stopped

Official observation: valid 3/8, mapped 2/8, CHALLENGE 0/3; four JSON-syntax
failures and one timeout. Removing next_prompt did not establish improved
representation reliability. This path is stopped and may be deleted only after
provenance preservation. See the [experiment ledger](EXPERIMENT_LEDGER.md) and
[research freeze](RESEARCH_FREEZE.md). Held-out remains sealed / not authorized.
Command availability is not authorization for provider inference. The definition
below is the historical proposal; it is not a new execution plan.

This isolated path tests removing question generation from the semantic call.
It is not integrated into production and does not solve generation of the actual
next interviewer question. No second call or placeholder question is created.
Live use requires a separately authorized frozen experiment and DNS precheck.

The required model output has exactly four fields:

- understandable_relevant: strict boolean
- essential_descriptive_gap: strict boolean or null
- unresolved_reasoning_issue: strict boolean or null
- reason: strict string, trimmed to 1–300 characters

Extra fields, including action and next_prompt, are rejected. Null means not
assessable, never false. Both later judgments must be booleans when the answer
is understandable/relevant. Independent judgments can both be true.

The v3 semantic definitions are unchanged. The prompt only changes version
identification/output-field declaration and removes question-generation
obligations. The schema removes only next_prompt and its consistency validator.
The corresponding assessment_prompt_inconsistency diagnostic is removed from
the closed assessment-only allowlist; other diagnostics retain their meanings
and deterministic priority.

Mapping remains CLARIFY for false understandability/relevance, otherwise
FOLLOW_UP for a descriptive gap, otherwise CHALLENGE for a reasoning issue,
otherwise MOVE_ON. Both issues true maps FOLLOW_UP. This maps evaluation action
strings directly, without constructing production Decision objects.

`assessment_only_live.py` proposes one request per explicitly selected,
owner-reviewed development case, using the frozen provider settings and
strict envelope/content/size checks. No retries, JSON repair, scraping or
undocumented request parameters are added. Existing v3 stays reproducible.
Reports omit source interview content, reason text, raw provider content,
hidden reasoning and credentials, and preserve sanitized failure diagnostics.
No application report files are created automatically.

The owner-reviewed expectation file is unchanged. Its not_assessable values
convert explicitly to null, and the independent assessment metrics retain
expected-applicability denominators, including failed attempts as incorrect.
No new pass/fail threshold is defined. No held-out evaluation mode is provided.
