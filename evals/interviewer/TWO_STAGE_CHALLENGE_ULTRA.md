# Experimental Stage 2 model-only candidate

## Current status — completed; Ultra not selected

The initial live pipeline run was operationally INCONCLUSIVE: all four attempted
Ultra Stage 2 requests returned HTTP 400, with no semantic judgments. Rejection
timings are not inference latency. Later synthetic compatibility work implicated
reasoning_budget=256 or its interaction; it did not establish that every budget
value is unsupported. Ultra then participated in the matched no-budget comparison,
whose final classification was MODEL-SELECTION NOT SUPPORTED. Ultra was not
selected. This pipeline is an archive candidate, not production-approved.
See the [experiment ledger](EXPERIMENT_LEDGER.md) and
[research freeze](RESEARCH_FREEZE.md). Held-out remains sealed / not authorized.
Command availability is not authorization for provider inference.

Experiment version: `interviewer-two-stage-challenge-ultra-v1`.
Historical implementation checkpoint: this was offline implementation only,
not production approval or live authorization. Frozen definitions follow.

Stage 1 remains `nvidia/nemotron-3-super-120b-a12b`. Stage 2 alone uses
`nvidia/nemotron-3-ultra-550b-a55b`. The owner supplied completed authoritative
hosted-model availability/interface verification. No inference was used to verify
that claim during implementation. Production pricing, cost, quotas, rate limits
and SLA characteristics remain unapproved; free prototype endpoint availability
does not establish a production SLA.

## Exact frozen comparison

The Stage 2 prompt version remains `interviewer-two-stage-challenge-v1` and its
system prompt bytes are inherited unchanged, including the original version text.
The new version identifies the experiment, not a semantic prompt revision.
Schema, challenge_warranted definition, mapping, source annotations and contrastive
review material are reused directly. No annotation is copied, reinterpreted or
modified. True maps to CHALLENGE with a non-null prompt; false maps to MOVE_ON with
null. Owner expectations never control routing; unannotated unexpected Stage 2
visits remain unassessable.

Endpoint remains https://integrate.api.nvidia.com/v1/chat/completions.
Both stages retain temperature=1.0, top_p=.95, max_tokens=1024,
reasoning_effort=high, reasoning_budget=256, stream=false and retries=0.
The Ultra request/envelope implementation matches the historical StageService
body except for the model value; an AST regression test and captured mock payloads
verify this. Strict parsing, size bounds, stop finish reason, validation and safe
failure/json/schema diagnostics are unchanged.

No guided_json, response_format, json_schema, constrained decoding, tools, seed
or other provider feature is added. Hosted constrained JSON support is not assumed
for this exact endpoint/model. Application validation remains authoritative.
Separate provider reasoning content is ignored: never logged, persisted, exposed,
scored or used to repair malformed final content.

## Model isolation and operations

UltraStageService has a fixed read-only model property, with no model selector in
production routes. It never assigns or temporarily replaces another module's
MODEL. Concurrent Super and Ultra adapters retain their own request identities.
Use separate reasoner instances for concurrent operations; the historical
mutable experimental last_trace remains sequential-per-instance, unchanged.

UltraReasoner reuses the exact challenge operation, strict bridge and experimental
session harness. Stage 1 terminal/failure paths skip Ultra; actual CONTINUE invokes
it once. One combined 30-second deadline is unchanged; Ultra gets only the remainder,
and exhausted budgets skip its call. Failures never commit or consume probes;
freshness, replay, max-two-probe enforcement and no lock across awaits remain intact.

## Future evaluation and privacy

The separate two_stage_challenge_ultra_live entrypoint requires --live and explicit
unique owner-reviewed development IDs, rejects held-out/whole-split selections,
and uses repeats=1. Future inference requires separate authorization and a DNS
precheck in the approved network-enabled context. No provider calls occurred here.

Reports keep the original prompt versions, annotations and metrics, adding a fixed
experiment_version and explicit stage_1_model/stage_2_model. The root model field
denotes the Stage 2 candidate. Default reports omit reason/next_prompt, source
context, raw payloads, headers, credentials, exception messages and hidden traces.
Stdout may be captured by the surrounding terminal. No new report persistence,
credential loading, production integration or PASS/FAIL gate is introduced.
