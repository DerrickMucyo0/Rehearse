import pytest
from app.thesis import THESIS_PACKS, ALLOWED_THESIS_PACKS, ThesisValidationError, mock_thesis_questions

def test_thesis_packs_exist():
    assert len(THESIS_PACKS) > 0
    assert "default" in ALLOWED_THESIS_PACKS or len(ALLOWED_THESIS_PACKS) > 0

def test_mock_thesis_questions_callable():
    assert callable(mock_thesis_questions)
