"""Offline integrity checks for the owner-approved research freeze snapshot."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

import pytest

from app.reasoning import ACTIONS
from evals.interviewer.evaluate import DATASET_VERSION

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / 'evals/interviewer'
MANIFEST_PATH = RESEARCH / 'research_provenance.json'
MANIFEST = json.loads(MANIFEST_PATH.read_text())
STATUS_DOCS = ('README.md', 'ASSESSMENT_ONLY.md', 'BLOCKING_CONTEXT.md',
               'TWO_STAGE.md', 'TWO_STAGE_CHALLENGE.md',
               'TWO_STAGE_CHALLENGE_ULTRA.md', 'STAGE2_MODEL_MATCH.md')
PRESERVATION_DOCS = ('RESEARCH_FREEZE.md', 'EXPERIMENT_LEDGER.md')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('name', (*PRESERVATION_DOCS, 'research_provenance.json'))
def test_preservation_artifact_exists(name):
    assert (RESEARCH / name).is_file()


def test_manifest_is_deterministic_json():
    assert MANIFEST_PATH.read_text() == json.dumps(MANIFEST, indent=2, sort_keys=True) + '\n'


@pytest.mark.parametrize('field,expected', [
    ('research_status', 'frozen'), ('issue', 9),
    ('branch', 'feat/nemotron-interviewer'), ('production_approval', False),
    ('selected_production_candidate', None), ('preferred_stage2_model', None),
    ('held_out_status', 'sealed'),
    ('full_development_status', 'blocked_for_frozen_candidate'),
    ('baseline_model', 'nvidia/nemotron-3-super-120b-a12b'),
    ('ultra_model', 'nvidia/nemotron-3-ultra-550b-a55b'),
])
def test_frozen_disposition(field, expected):
    assert MANIFEST[field] == expected
    assert type(MANIFEST[field]) is type(expected)


def test_four_action_contract_remains_exact():
    assert MANIFEST['four_action_contract'] == ['FOLLOW_UP', 'CLARIFY', 'CHALLENGE', 'MOVE_ON']
    assert tuple(MANIFEST['four_action_contract']) == ACTIONS


def test_dataset_identity_without_inspecting_cases():
    assert MANIFEST['dataset_version'] == DATASET_VERSION == 'interviewer-cases-v2'
    assert MANIFEST['dataset_path'] == 'evals/interviewer/cases.jsonl'
    assert MANIFEST['dataset_sha256'] == (
        'd496391a3f25ac8173e504794497c4aa4120526a0c7238f94164b3bd234dfc35')
    assert digest(ROOT / MANIFEST['dataset_path']) == MANIFEST['dataset_sha256']


def test_all_review_artifacts_are_checksummed():
    expected = {p.relative_to(ROOT).as_posix() for p in RESEARCH.glob('*development*.json')}
    rows = MANIFEST['annotation_artifacts']
    assert len(rows) == len(expected) == 5
    assert {row['path'] for row in rows} == expected
    for row in rows:
        assert set(row) == {'path', 'sha256'}
        assert digest(ROOT / row['path']) == row['sha256']


def test_prompt_schema_and_dependency_artifacts_are_checksummed():
    expected = {p.relative_to(ROOT).as_posix() for p in RESEARCH.glob('*.py')}
    expected.update({'backend/app/reasoning.py', 'backend/app/nemotron.py'})
    rows = MANIFEST['prompt_schema_artifacts']
    assert len(rows) == len(expected)
    assert {row['path'] for row in rows} == expected
    for row in rows:
        assert set(row) == {'path', 'sha256'}
        assert (ROOT / row['path']).is_file()
        assert digest(ROOT / row['path']) == row['sha256']


def test_historical_files_unchanged_except_authorized_docs():
    expected_docs = {'README.md', 'BUILD_LOG.md',
                     *(f'evals/interviewer/{name}' for name in STATUS_DOCS)}
    assert set(MANIFEST['authorized_documentation_updates']) == expected_docs
    preserved = MANIFEST['historical_file_sha256']
    assert len(preserved) == 64
    assert not set(preserved) & expected_docs
    assert '.env.example' in preserved
    assert 'evals/interviewer/ASSESSMENT.md' in preserved
    assert 'evals/interviewer/ASSESSMENT_V2.md' in preserved
    for path, checksum in preserved.items():
        assert (ROOT / path).is_file()
        assert digest(ROOT / path) == checksum


def test_provenance_fields_are_allowlisted():
    assert set(MANIFEST) == {
        'manifest_version', 'recorded_on', 'research_status', 'issue', 'branch',
        'four_action_contract', 'selected_production_candidate', 'production_approval',
        'preferred_stage2_model', 'baseline_model', 'ultra_model', 'dataset_version',
        'dataset_path', 'dataset_sha256', 'annotation_artifacts', 'prompt_schema_artifacts',
        'experiment_document_paths', 'result_ledger_path', 'freeze_note_path',
        'held_out_status', 'full_development_status', 'runtime_metadata',
        'runtime_metadata_scope', 'historical_file_sha256',
        'authorized_documentation_updates', 'historical_preservation_gaps',
    }


def test_provenance_has_only_relative_repository_paths():
    paths = [MANIFEST['dataset_path'], MANIFEST['result_ledger_path'],
             MANIFEST['freeze_note_path'], *MANIFEST['experiment_document_paths'],
             *MANIFEST['historical_file_sha256'], *MANIFEST['authorized_documentation_updates'],
             *(row['path'] for key in ('annotation_artifacts', 'prompt_schema_artifacts')
               for row in MANIFEST[key])]
    for path in paths:
        assert not PurePosixPath(path).is_absolute()
        assert '..' not in PurePosixPath(path).parts
        assert '\\' not in path
    assert '/Users/' not in MANIFEST_PATH.read_text()
    assert '/home/' not in MANIFEST_PATH.read_text()


def test_runtime_metadata_is_safe_checkpoint_metadata():
    assert set(MANIFEST['runtime_metadata']) == {
        'python', 'fastapi', 'pydantic', 'starlette', 'pytest', 'httpx2', 'httpx'}
    for version in MANIFEST['runtime_metadata'].values():
        assert version is None or re.fullmatch(r'[0-9][A-Za-z0-9.+_-]*', version)
    assert MANIFEST['runtime_metadata_scope'] == (
        'preservation_checkpoint_only_not_historical_dependency_lock')


def test_preservation_artifacts_do_not_contain_credentials_or_private_paths():
    text = '\n'.join((RESEARCH / name).read_text()
                     for name in (*PRESERVATION_DOCS, 'research_provenance.json'))
    assert '/Users/' not in text
    assert not re.search(r'nvapi-[A-Za-z0-9_-]{16,}', text)
    assert not re.search(r'sk-[A-Za-z0-9_-]{16,}', text)
    assert not re.search(r'Bearer\s+[A-Za-z0-9._-]{16,}', text, re.IGNORECASE)
    assert not re.search(r'(?:NVIDIA_API_KEY|Authorization)\s*[:=]\s*[\"\']?\S+', text)
    assert '-----BEGIN PRIVATE KEY-----' not in text


def test_model_selection_and_challenge_disposition_are_explicit():
    for name in PRESERVATION_DOCS:
        text = ' '.join((RESEARCH / name).read_text().lower().split())
        assert 'model-selection not supported' in text
        assert 'not selected' in text
        assert 'automatic challenge remains unresolved' in text


def test_freeze_warns_about_existing_route_wiring():
    text = (RESEARCH / 'RESEARCH_FREEZE.md').read_text()
    assert 'UNAPPROVED FEATURE-BRANCH WORK' in text
    assert 'not merge/deployment' in text
    assert 'interviewer-v5' in text
    assert 'Runtime cleanup/reversion requires a separate owner decision.' in text


def test_preservation_does_not_claim_production_approval():
    freeze = (RESEARCH / 'RESEARCH_FREEZE.md').read_text().lower()
    ledger = (RESEARCH / 'EXPERIMENT_LEDGER.md').read_text().lower()
    assert 'no production nemotron interviewer is approved' in freeze
    assert 'no production interviewer approved' in ledger
    assert MANIFEST['production_approval'] is False
    for text in (freeze, ledger):
        assert 'production approval granted' not in text
        assert 'approved for production deployment' not in text


def test_top_level_readme_links_and_current_version():
    text = (ROOT / 'README.md').read_text()
    assert '(evals/interviewer/RESEARCH_FREEZE.md)' in text
    assert '(evals/interviewer/EXPERIMENT_LEDGER.md)' in text
    assert 'RESEARCH FROZEN — ISSUE #9 OPEN — NOT PRODUCTION APPROVED' in text
    assert '| Prompt version in current application source | `interviewer-v5` |' in text
    assert 'Historical initial implementation checkpoint:' in text


@pytest.mark.parametrize('name', STATUS_DOCS)
def test_eval_documents_state_sealed_status_and_no_inference_authorization(name):
    text = (RESEARCH / name).read_text().lower()
    assert 'sealed / not authorized' in text
    assert 'command availability is not authorization for provider inference' in (
        text.replace('**', '').replace('\n', ' '))
    assert '(experiment_ledger.md)' in text
    assert '(research_freeze.md)' in text


def test_ledger_separates_result_types_and_diagnostic_samples():
    text = (RESEARCH / 'EXPERIMENT_LEDGER.md').read_text()
    assert 'OFFICIAL RESULT' in text
    assert 'DIAGNOSTIC RESULT' in text
    assert 'COMPATIBILITY RESULT' in text
    assert 'Neither sample\nreplaces the official eight-case gate.' in text
    assert 'This diagnostic is a separate experiment' in text
    assert 'not retroactive proof' in text


def test_ledger_preserves_official_direct_results():
    text = (RESEARCH / 'EXPERIMENT_LEDGER.md').read_text()
    assert '| interviewer-v1 development baseline | 25/48 (52.1%) | 0.5013 | 0/10 | 1 | 6 | 3 |' in text
    assert '| Recovered interviewer-v3 development | 32/48 (66.67%) | 0.585101 | 0/10 | 3 | 0 | 0 |' in text
    assert '| interviewer-v4 frozen eight | 5/8 | not recorded here | 1/3 | 2 | 0 | 0 |' in text
    assert '| interviewer-v5 frozen eight | 3/8 | not recorded here | 0/3 | 2 | 0 | 0 |' in text


def test_ledger_preserves_matched_comparison_configuration_and_failures():
    text = (RESEARCH / 'EXPERIMENT_LEDGER.md').read_text()
    assert '**reasoning_budget ABSENT**' in text
    assert 'No Stage 1\ninference was performed.' in text
    assert '| Valid | 6/6 | 5/6 |' in text
    assert '| Correct | 4/6 | 4/6 |' in text
    assert 'Ultra recoveries 1; Ultra regressions 1' in text
    assert 'schema_reason=text_bound_violation' in text
    assert 'This is not a test of the combined pipeline deadline.' in text


def test_compatibility_evidence_is_qualified():
    text = (RESEARCH / 'EXPERIMENT_LEDGER.md').read_text()
    assert 'REASONING_BUDGET_FIELD_OR_INTERACTION_SUSPECTED' in text
    assert 'does **not** prove every `reasoning_budget` value is unsupported' in text
    assert 'HTTP rejection timings are not inference' in text


def test_historical_gaps_are_unavailable_not_reconstructed():
    gaps = MANIFEST['historical_preservation_gaps']
    assert gaps['historical_dependency_manifests'] == 'unavailable'
    assert gaps['unavailable_result_json'] == 'not_reconstructed'
    assert gaps['direct_classifier_v1_v4_independent_snapshots'] == 'not_all_available'
    assert gaps['dataset_history'] == 'current_snapshot_only'
    for name in PRESERVATION_DOCS:
        text = (RESEARCH / name).read_text()
        assert 'Historical preservation gaps' in text
        assert '**unavailable**' in text


def test_restart_conditions_exclude_more_stochastic_tuning():
    text = (RESEARCH / 'RESEARCH_FREEZE.md').read_text()
    for phrase in ('fine-tuned model', 'annotation guide', 'agreement/adjudication',
                   'documented hosted support', 'not restart conditions',
                   'another wording variant', 'another random', 'retrying stochastic misses',
                   'using held-out to guide design'):
        assert phrase in text


def test_documentation_local_links_and_manifest_references_exist():
    docs = [ROOT / 'README.md', ROOT / 'BUILD_LOG.md',
            *(RESEARCH / name for name in (*PRESERVATION_DOCS, *STATUS_DOCS))]
    for doc in docs:
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', doc.read_text()):
            if target.startswith(('https://', 'http://', '#')):
                continue
            assert (doc.parent / target.split('#')[0]).exists(), (doc.name, target)
    for path in (MANIFEST['freeze_note_path'], MANIFEST['result_ledger_path'],
                 *MANIFEST['experiment_document_paths']):
        assert (ROOT / path).is_file()


def test_build_log_appends_freeze_without_rewriting_checkpoint():
    text = (ROOT / 'BUILD_LOG.md').read_text()
    old, new = text.split('## 2026-10-03 — Issue #9 research-freeze checkpoint', 1)
    assert 'No live NVIDIA request was made.' in old
    assert 'MODEL-SELECTION NOT SUPPORTED' in new
    assert 'Issue #9 remains open' in new
    assert 'Held-out remains sealed / not authorized' in new
