# Isolated assessment baseline

`assessment_live.py` is an explicitly invoked, development-only architecture
experiment. Production sessions and the interviewer-v5 direct classifier do not
use it. The model outputs semantic assessment fields, never an action; strict
Assessment validation precedes deterministic action mapping.

The separately versioned policy is interviewer-assessment-v1. Transport and
response-envelope handling mirror the existing adapter. Its inherited request
lifecycle preserves the 30-second deadline and sanitized provider diagnostics.
Sampling is fixed at temperature 1.0, top_p 0.95, max_tokens 1024, high effort,
and reasoning budget 256. There are no retries or schema-generation parameters.

Run only with explicit provider authorization and a successful DNS precheck in
the network-enabled execution context. For example, select explicit development
IDs with `python -m evals.interviewer.assessment_live --live --case-id ID ...`
(with backend/ on PYTHONPATH). Unknown/non-development/duplicate IDs are rejected
before provider construction. No whole-split or held-out mode is provided.

Reports contain semantic booleans/nulls, expected/predicted mapped actions,
validity, sanitized errors, metrics and timings. They omit reason/next_prompt
text, source interview fields, raw payloads, hidden reasoning and credentials.
Reports go to stdout; no application report files are created automatically.

Expected assessments are rubric-derived from locked development action labels,
NOT independently human-reviewed ground truth. Source cases remain unchanged.
A field's accuracy denominator uses expected applicability: all selected cases
for understandability, non-null expected values for the other fields. A failed
assessment or a predicted branch that skips an applicable field counts as
incorrect. Nonapplicable fields are reported but not scored. Failures remain in
mapped-action metrics. This first experiment establishes a baseline; no new
pass/fail threshold is defined.
