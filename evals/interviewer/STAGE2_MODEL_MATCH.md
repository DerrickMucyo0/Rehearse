# Matched Stage 2 model-selection experiment

## Current status — completed; research frozen

Final predeclared classification: **MODEL-SELECTION NOT SUPPORTED**.
Super: valid 6/6, correct 4/6; Ultra: valid 5/6, correct 4/6.
Paired: one Ultra recovery, one regression, three unchanged correct pairs and
one Ultra-only failure. Ultra was not selected; Super is retained only as the
research baseline/comparator, not production-approved for automatic CHALLENGE.
See the [experiment ledger](EXPERIMENT_LEDGER.md) and
[research freeze](RESEARCH_FREEZE.md). Held-out remains sealed / not authorized.
Command availability is not authorization for provider inference. The execution
order and command below are historical research instructions, not a pending run.

Experiment: `interviewer-stage2-model-match-v1`. Evaluation tooling only; no
production integration or live authorization is implied by this implementation.

Compare `nvidia/nemotron-3-super-120b-a12b` and
`nvidia/nemotron-3-ultra-550b-a55b` independently on the frozen
`challenge_warranted` Stage 2 task. Immutable adapter instances prevent shared
model switching. Both requests contain exactly `model`, `stream`, `messages`,
`temperature`, `top_p`, `max_tokens`, and `reasoning_effort`. Both use 1.0, .95,
1024, high, stream=false, retries=0. **reasoning_budget is absent**, never null,
zero or -1. No model-specific, thinking, tool or constrained-output fields exist.

The semantic prompt is the exact existing `STAGE2_INSTRUCTIONS` object from
`two_stage_challenge_provider.py`; its embedded version remains
`interviewer-two-stage-challenge-v1`. Experiment metadata is separate. There is
no wording change. The exact existing `ChallengeStage2` schema, parser, validator
and mapping are reused: true requires a non-null next_prompt and maps CHALLENGE;
false requires null and maps MOVE_ON. Reason and prompt text never decide scores.

## Owner-reviewed input, not Stage 1 inference

Only the six reviewed development contexts below are selectable. Their planned
question, immediate current_prompt, relevant prior turns and answer remain
unchanged. Existing checksum, source identity, development-split and owner-review
guards run before provider construction. All six have owner-approved
understandable_relevant=true and blocking_context_gap=false. This is the fixed
routing premise, not a synthesized Stage 1 result. No Stage 1 provider or
orchestrator is invoked. Stage 1 production readiness is not established.

Approved CW=true: unsupported_claim-003, tradeoffs-001, injection-004.
Approved CW=false: strong_complete-001, multi_turn-001, injection-002.
No held-out, boundary, arbitrary source-path, model, configuration or case selector
is exposed by the CLI. Both fixed adapters can be exercised independently offline.

## Historical frozen execution order

One attempt per model per case, sequentially, without retries or substitutions:

1. unsupported_claim-003 — Super
2. unsupported_claim-003 — Ultra
3. tradeoffs-001 — Ultra
4. tradeoffs-001 — Super
5. injection-004 — Super
6. injection-004 — Ultra
7. strong_complete-001 — Ultra
8. strong_complete-001 — Super
9. multi_turn-001 — Super
10. multi_turn-001 — Ultra
11. injection-002 — Ultra
12. injection-002 — Super

Each Stage 2-only call has the existing 30-second provider timeout. This is **not**
the production combined Stage 1+Stage 2 deadline. This diagnostic cannot establish
production latency suitability. Future live execution requires separate owner
authorization and the approved network-enabled context. The guarded entrypoint
is `python -m evals.interviewer.stage2_model_match --live`; it checks DNS before
inference and stops with zero requests if resolution fails. It reads the existing
NVIDIA_API_KEY environment path without adding credential loading or persistence.
Do not execute it during offline implementation/review.

## Strict failure handling and privacy

The historical bounded request/envelope path is retained, except for model
selection and budget omission. Keep stop finish reason, assistant role, no tools
or refusal, strict whole-content JSON, duplicate-key/non-JSON-constant rejection,
strict Pydantic types/text bounds/cross-field validation and safe
invalid_reason/json_reason/schema_reason/failure_kind/http_status diagnostics.
No repair, scraping, prefix acceptance, inference from text, fallback or retries.

Only explicit Boolean/action/diagnostic projections are serialized. Reports omit
source question/current_prompt/answer/prior turns, system prompt text, generated
reason/next_prompt, separate provider reasoning, raw responses/payloads, headers,
credentials and exception messages. Successful/failed reports are in memory and
stdout only; terminals may capture stdout. Production/session logging and
persistence are unchanged. Separate provider reasoning cannot rescue invalid JSON.

## Reporting and interpretation

Reports preserve all 12 attempts and provide six side-by-side model pairs. Each
model includes validity, expected/predicted CW, correctness, derived action,
latency and safe diagnostics. Per-model aggregates include valid/correct out of
six; true CHALLENGE and false MOVE_ON correct out of three each; observed valid
false positives/misses; invalid/provider/timeout counts; diagnostic breakdowns;
and the existing evaluator median/p95 definitions. Failures remain in the
denominators and never receive inferred semantic values.

Paired categories are both correct, Super only correct, Ultra only correct, both
wrong, Super failure, Ultra failure and both failure. Operational failure takes
precedence over semantic comparison. Recoveries/regressions and unchanged
correct/wrong counts require two valid outputs, avoiding recovery credit for a
failed comparator.

The historical predeclared owner interpretation was directional, not an automatic production
gate or statistical proof:

- MODEL-SELECTION PROMISING: meaningful Ultra case-level recovery without
  comparable loss of correct cases or a dominating operational failure pattern.
- MODEL-SELECTION NOT SUPPORTED: persistent errors, offsetting regressions, or
  materially worse Ultra operational reliability.
- INCONCLUSIVE: insufficient valid paired evidence because too many outputs fail.

Do not invent a numeric production threshold. No classification is generated
automatically by this evaluator.

## Historical comparison boundary

Historical Super Stage 2 used reasoning_budget=256. The historical Ultra run was
operationally inconclusive: all four actual Ultra requests returned HTTP 400.
Synthetic diagnosis implicated adding reasoning_budget=256 or its interaction;
it did not establish that every budget is unsupported or explain provider text.

This new matched experiment intentionally omits reasoning_budget from **both**
models. Compare results primarily within this new matched run. Historical Super
percentages are not its direct control. Omission leaves provider-internal defaults
unspecified; matched explicit requests do not prove identical internal reasoning
allocation between models. Historical evaluators, results, annotations, production
defaults and combined-deadline architecture remain unchanged.
