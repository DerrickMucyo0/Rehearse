# Issue #9 research freeze

**RESEARCH FROZEN — ISSUE #9 OPEN — NOT PRODUCTION APPROVED**

Owner decision recorded on 2026-10-03: Issue #9 remains open. Milestone 5 retains
the original **FOLLOW_UP, CLARIFY, CHALLENGE, MOVE_ON** contract. Automatic
CHALLENGE remains unresolved. Current Nemotron interviewer research is frozen;
no production Nemotron interviewer is approved.

Super (`nvidia/nemotron-3-super-120b-a12b`) is retained only as a research
baseline/comparator. Ultra (`nvidia/nemotron-3-ultra-550b-a55b`) was evaluated but
**not selected**. The final matched classification was
**MODEL-SELECTION NOT SUPPORTED**, not evidence that Super is production-ready.
Conditional two-stage reasoning is retained as the frozen research architecture.

Full-development 48-case evaluation remains blocked for the frozen candidate.
Historical full-development runs remain recorded; this block does not erase them.
Held-out remains **sealed / not authorized** for inspection, inference or design.
Provider experimentation is stopped. Command availability is **not authorization
for provider inference**; commands in experiment documentation are historical
research instructions, not a pending execution plan.

## Production boundary

Existing route wiring is **UNAPPROVED FEATURE-BRANCH WORK**. The normal answer
route still invokes the direct interviewer-v5 classifier; the experimental
two-stage evaluators are not imported by production routes. Preservation does
not disable or enable inference, introduce a feature flag, restore legacy flow,
or integrate a research architecture. This snapshot is **not merge/deployment
authorization**. Runtime cleanup/reversion requires a separate owner decision.

Reusable primitives include strict contracts/parsing, sanitized diagnostics,
deterministic state ownership, probe limits, idempotency, stale-turn protection,
failure atomicity and deadlines. Their offline structural coverage does not
establish semantic model quality or deployed production reliability.

## Preserved research and dependencies

Keep conditional two-stage, challenge-v1, matched-model evaluation and
representation diagnostics active but frozen. Direct variants, assessment
v1/v2/v3, blocking-context and the Ultra pipeline are archive candidates.
Assessment-only may be deleted only after provenance preservation. No experiment
is deleted by this checkpoint.

Archival classification does not make a helper disposable. Active tools reuse
`assessment_independent.py` schema diagnostics, `assessment_live_v2.py` selection,
`blocking_context.py` and `blocking_context_live.py` reviewed definitions/guards,
`evaluate.py` case/scoring helpers, and application contracts/adapter lifecycle.
Challenge-v1 depends on the original two-stage contract/provider/orchestration;
the matched evaluator depends on challenge review/provider helpers. The B1–B3
review artifact remains in the selection dependency closure. Preserve imports,
review checksums and source identities before any future move or deletion.

## Restart conditions

Research resumes only under a materially new reviewed hypothesis, supported by
one or more of:

- Materially stronger future reasoning capability.
- A fine-tuned model.
- A materially larger owner-reviewed development set.
- An explicit annotation guide.
- A reviewer agreement/adjudication process.
- A genuinely new representation capability with documented hosted support.

These are **not restart conditions**: another wording variant, another random
model, retrying stochastic misses, or using held-out to guide design. A new
hypothesis still needs a predeclared experiment and explicit execution approval.
No further provider spend is justified under the frozen research paths.
Product work should move to a milestone independent of unresolved automatic
CHALLENGE; this note does not select a new milestone.

## Historical preservation gaps

Historical live result reports were not previously stored as repository
artifacts. Many official observations existed only in execution/conversation
history. Direct-classifier prompt versions v1–v4 do not all exist as independent
source snapshots. Only the current dataset snapshot is present. Exact historical
dependency manifests were not captured.

Missing prompt snapshots, unavailable result JSON and historical dependency
locks remain **unavailable**; they are not reconstructed. No raw provider output,
hidden reasoning, candidate content or credentials are archived here. The current
runtime versions in the manifest describe this preservation checkpoint, not past
runs. This ledger preserves known observations, not invented complete reports.

## Provenance and offline verification

See the [experiment ledger](EXPERIMENT_LEDGER.md) and
[machine-readable provenance](research_provenance.json). The manifest records
repository-relative paths and SHA-256 checksums. Preservation verification
compares protected Issue #9 artifacts against the final frozen snapshot. The
snapshot incorporates only explicitly authorized preservation changes, including
documentation updates, checksum-pin consistency updates, and final provenance
checksum synchronization.
Dataset hashing verifies identity without semantic held-out inspection.

From the repository root, using the existing Python environment:

```sh
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -W error tests/test_interviewer_research_preservation.py
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -W error
git diff --check
```

These are offline tests. Real async provider transport is blocked by the test
fixture. Reproducing a historical live procedure would not guarantee identical
stochastic predictions and is not authorized by this freeze.
