# Issue #9 development experiment ledger

**Research frozen. Issue #9 open. No production interviewer approved.**

This ledger preserves owner-recorded development observations available in
execution/conversation history. It does not reconstruct unavailable reports.
Official scores, diagnostic samples and synthetic compatibility requests remain
separate. Failures stay in their original denominators. No statistical
significance, causal prompt effect, model superiority or production readiness is
claimed. See [the freeze policy](RESEARCH_FREEZE.md) and
[provenance manifest](research_provenance.json).

## A. Direct four-action classifier — OFFICIAL RESULT

| Version/run | Action match | Macro F1 | CHALLENGE correct | Invalid | Provider errors | Timeouts |
| --- | --- | --- | --- | --- | --- | --- |
| interviewer-v1 development baseline | 25/48 (52.1%) | 0.5013 | 0/10 | 1 | 6 | 3 |
| Recovered interviewer-v3 development | 32/48 (66.67%) | 0.585101 | 0/10 | 3 | 0 | 0 |
| interviewer-v4 frozen eight | 5/8 | not recorded here | 1/3 | 2 | 0 | 0 |
| interviewer-v5 frozen eight | 3/8 | not recorded here | 0/3 | 2 | 0 | 0 |

Recovered v3 expected-action correct counts: FOLLOW_UP 13/17, CLARIFY 10/10,
CHALLENGE 0/10, MOVE_ON 9/11. Its frozen gate failed. V4 anchors were 4/5;
v5 anchors were 3/5. Both eight-case gates failed. V5 invalid reasons were
`json_syntax` for both failures.

The v3 full-development gate was frozen before results: action match >=28/48,
macro F1 >=0.55, CHALLENGE >=3/10; per-action correct FOLLOW_UP >=11/17,
CLARIFY >=8/10, CHALLENGE >=3/10, MOVE_ON >=6/11; invalid <=1/48,
provider errors <=6/48, timeouts <=3/48. All conditions were required.
These historical thresholds are not changed by the freeze.

An earlier v3 full-development attempt had 48/48 `provider_error`, all
`failure_kind=transport`, no HTTP statuses and zero valid decisions. It formally
failed but was operationally invalid for assessing prompt semantics. A later
single diagnostic established DNS failure for that request, not retroactive proof
of the subtype of all 48 failures. Approved network-enabled execution subsequently
demonstrated recovery before the recovered full-development run.

Disposition: prompt-only direct-classifier iteration stopped. Application source
currently contains interviewer-v5; that is not a production selection.

### Separate direct-classifier diagnostics — DIAGNOSTIC RESULT

V4 `tradeoffs-001` was officially invalid; its later diagnostic timed out around
30 seconds, leaving that diagnostic's semantic result unknown. V4 `irrelevant-002`
was officially invalid; its later diagnostic returned valid CLARIFY, demonstrating
intermittent invalid-output behavior for that case/configuration. Neither sample
replaces the official eight-case gate. No missing raw output is reconstructed.

The historical high/256 eight-case condition was 6/8 correct, one invalid output,
one semantic miss, no provider errors/timeouts. High/512 was 6/8 correct, one
semantic miss, one timeout, no invalid outputs/provider errors. Separate
injection diagnostics and connectivity probes did not replace either result.

## B. Assessment semantic decomposition — OFFICIAL RESULT

| Version | Valid | Mapped correct | CHALLENGE mapped correct |
| --- | --- | --- | --- |
| interviewer-assessment-v1 | 6/8 | 4/8 | 0/3 |
| interviewer-assessment-v2 | 7/8 | 4/8 | 0/3 |
| interviewer-assessment-v3 | 6/8 | 4/8 | 0/3 |

V1 had two invalid outputs (one `json_syntax`, one `schema_validation`);
v2 had one (`tradeoffs-001`, `json_syntax`); v3 had two
(`unsupported_claim-003`, `json_syntax`; `irrelevant-002`,
`schema_validation/missing_required_field`). All three had zero provider errors
and timeouts. V1 intermediate expectations were rubric-derived; v2/v3 used
separately owner-reviewed development annotations. These histories are not
retroactively relabeled.

Disposition: semantic decomposition improved observability but not reliable
action selection. Incorrect descriptive-gap judgments could suppress CHALLENGE
even when a reasoning concern was observed.

### Repeated assessment-v3 — DIAGNOSTIC RESULT

- 18/24 valid; 11/24 mapped correct; CHALLENGE 0/9; anchors 11/15.
- Six invalid outputs; no provider errors or timeouts.
- `unsupported_claim-003` was valid in all three repeats, with gap=true and
  reasoning=true, mapping FOLLOW_UP despite reviewed gap=false.

This diagnostic is a separate experiment, not a replacement for the official
eight-case result.

## C. Assessment-only-v1 — OFFICIAL RESULT

Valid 3/8; mapped correct 2/8; CHALLENGE 0/3; anchors 2/5. Four invalid outputs,
all `json_syntax`; one timeout; zero provider errors.

Compared descriptively with assessment-v3: validity 6/8 -> 3/8, mapped 4/8 -> 2/8,
invalid outputs 2 -> 4, timeouts 0 -> 1. Removing `next_prompt` did not establish
improved representation reliability in this observation. Path completed and stopped.

## D. interviewer-blocking-context-v1 — OFFICIAL RESULT

Valid 9/11; mapped correct 7/11; CHALLENGE 1/3; original anchors 4/5;
boundary FOLLOW_UP 2/3; complete expected both-true semantic tuples 0/3.
Two invalid outputs (`json_syntax/other_json_syntax`), zero provider errors,
zero timeouts.

The owner-approved blocking-context contract narrowed FOLLOW_UP to necessary
factual prerequisites. Genuine both-true cases retained FOLLOW_UP precedence.
Disposition: one-call blocking-context path stopped; its reviewed definition and
artifacts remain dependencies of the frozen two-stage tools.

## E. interviewer-two-stage-v1 — OFFICIAL RESULT

Stage 1 valid 11/11; route accuracy 9/11. Stage 2 valid 6/6; reasoning accuracy
3/6. Final valid 11/11; final actions correct 6/11; CHALLENGE 1/3;
original anchors 3/5; boundary FOLLOW_UP 2/3. Seventeen provider calls;
zero provider errors, timeouts or combined deadline expirations.

Disposition: architecture retained; Stage 2 semantic boundary unresolved.
One combined 30-second deadline covers the two-stage operation.

## F. interviewer-two-stage-challenge-v1 — OFFICIAL RESULT

Final valid 10/11; final correct 6/11. Stage 1 valid 11/11; route accuracy 9/11.
Stage 2 valid 5/6; approved applicable Stage 2 correct 2/5. CHALLENGE 1/3;
original anchors 3/5; boundary FOLLOW_UP 2/3.

Two false-positive challenges: `multi_turn-001`, `injection-002`.
Missed challenge: `tradeoffs-001`. One Stage 2 JSON-syntax failure.
Zero provider errors/timeouts. The five common intended Stage 2 visits retained
the same Boolean/action outcomes as the preceding reasoning-issue experiment.
Skipped Stage 2 judgments are unobserved, not inferred.

Disposition: Stage 2 semantic prompt iteration stopped. The approved
`challenge_warranted` contract and architecture are retained as frozen research.

## G. First Ultra two-stage attempt — OFFICIAL RESULT

Experiment: interviewer-two-stage-challenge-ultra-v1. Stage 2 model:
`nvidia/nemotron-3-ultra-550b-a55b`. All four attempted Ultra Stage 2 requests
returned HTTP 400. No valid semantic Ultra judgment was produced.

Classification: **INCONCLUSIVE**. HTTP rejection timings are not inference
latency. This run did not establish a semantic model failure or preference.

## H. Ultra request compatibility — COMPATIBILITY RESULT

Initial synthetic probe: minimal Ultra request HTTP 200; frozen eight-field
request HTTP 400. These were synthetic requests, not interview evaluations.

| Delta-debugging request | Result |
| --- | --- |
| B repeat, including reasoning_budget=256 | HTTP 400 |
| Messages-only C (model/messages/stream) | HTTP 200 |
| Generation controls D (+temperature/top_p/max_tokens) | HTTP 200 |
| E (+reasoning_effort=high) | HTTP 200 |
| F (+reasoning_budget=256) | HTTP 400 |
| G (budget=256 without explicit effort) | HTTP 400 |

Classification: **REASONING_BUDGET_FIELD_OR_INTERACTION_SUSPECTED**.
This does **not** prove every `reasoning_budget` value is unsupported, nor that
the documentation is wrong. No raw rejection body or hidden reasoning is archived.

## I. interviewer-stage2-model-match-v1 — OFFICIAL RESULT

Both models used the same frozen challenge-v1 semantic prompt/schema, original
reviewed contexts and fixed owner-reviewed Stage 1 routing premise. No Stage 1
inference was performed. One attempt per model per case, 12 requests total.

Endpoint: `https://integrate.api.nvidia.com/v1/chat/completions`.
Super: `nvidia/nemotron-3-super-120b-a12b`.
Ultra: `nvidia/nemotron-3-ultra-550b-a55b`.
Both: temperature 1.0, top_p 0.95, max_tokens 1024, reasoning_effort high,
stream false, **reasoning_budget ABSENT**, zero retries, 30 seconds per Stage 2
call. This is not a test of the combined pipeline deadline.

| Case | Expected | Super | Ultra |
| --- | --- | --- | --- |
| unsupported_claim-003 | CHALLENGE | CHALLENGE | CHALLENGE |
| tradeoffs-001 | CHALLENGE | CHALLENGE | MOVE_ON |
| injection-004 | CHALLENGE | CHALLENGE | CHALLENGE |
| strong_complete-001 | MOVE_ON | MOVE_ON | MOVE_ON |
| multi_turn-001 | MOVE_ON | CHALLENGE | MOVE_ON |
| injection-002 | MOVE_ON | CHALLENGE | invalid_output |

| Metric | Super | Ultra |
| --- | --- | --- |
| Valid | 6/6 | 5/6 |
| Correct | 4/6 | 4/6 |
| CHALLENGE true correct | 3/3 | 2/3 |
| MOVE_ON false correct | 1/3 | 2/3 |
| False-positive challenges among valid outputs | 2 | 0 |
| Missed challenges among valid outputs | 0 | 1 |
| Invalid outputs | 0 | 1 |
| Provider errors / timeouts | 0 / 0 | 0 / 0 |
| Median latency, all attempts (ms) | 6642.110 | 7563.430 |
| p95 latency, all attempts (ms) | 10218.689 | 20899.741 |

Ultra's invalid attempt was `injection-002`,
`invalid_reason=schema_validation`, `schema_reason=text_bound_violation`.
No semantic value or violated text field is inferred from that failure.

Paired: Ultra recoveries 1; Ultra regressions 1; unchanged correct 3;
unchanged wrong 0; Ultra-only failures 1. Five common valid pairs.
Final predeclared classification: **MODEL-SELECTION NOT SUPPORTED**.
Ultra is not selected. Super's three detected challenges do not establish
production readiness because it also challenged two sufficient MOVE_ON anchors.

## J. Final research disposition — OWNER DECISION

Issue #9 remains open under the original four-action scope. Automatic CHALLENGE
remains unresolved. Super is not production-approved for automatic CHALLENGE;
Ultra is not selected. Conditional two-stage remains the frozen research
architecture. Further full-48 development evaluation is blocked for the frozen
candidate. Held-out remains sealed / not authorized. Provider experimentation
and semantic prompt/model-selection iteration are stopped.

## Historical preservation gaps

Historical live result reports were not previously stored as repository
artifacts; many official observations existed only in execution/conversation
history. This ledger records the known figures, not unavailable complete JSON.
Direct-classifier prompt versions v1–v4 do not all exist as independent source
snapshots. Only the current dataset snapshot is present. Exact historical
dependency manifests were not captured.

Missing prompt text, unavailable result JSON and dependency locks remain
**unavailable**. No raw provider output or hidden reasoning is reconstructed.
Current manifest package versions are checkpoint metadata, not historical locks.
Diagnostic samples never replace official results. Existing full-development
failures and recovered runs retain their separate identities.
