from types import SimpleNamespace

import pytest

from app.speaking_metrics import measure_transcription
from app.transcription import TranscriptionResult, WordTiming


def timing(text, start=0.0, end=1.0):
    return WordTiming(text=text, start=start, end=end)


@pytest.mark.parametrize(('text', 'count'), [
    ('', 0), (' \t\n', 0), ('Hello there, world!', 3), ('... ! — _', 0),
    ('one\t two\nthree   four', 4), ('MiXeD CASE', 2),
    ("I'm don't we’re mother-in-law", 4), ('one—two three/four five_six', 6),
    ('123 café 中文', 3), ('\"word\" (other).', 2),
])
def test_documented_word_rule(text, count):
    result = measure_transcription(text, None, [])
    assert result.recognized_word_count == count
    assert result.source == 'original_transcription'


@pytest.mark.parametrize('language', ['eng', None, 'fra', 'en', 'ENG', ''])
def test_word_rule_is_language_independent(language):
    assert measure_transcription("I'm mother-in-law!", language, []).recognized_word_count == 2


@pytest.mark.parametrize(('text', 'um', 'uh'), [
    ('um', 1, 0), ('uh', 0, 1), ('Um, UH! \"um\" (Uh)', 2, 2),
    ('um um uh um uh', 3, 2), ('umbrella thumb uhh', 0, 0),
    ('like you know basically actually', 0, 0), ('No fillers here.', 0, 0),
    ("um-like uh's", 0, 0), ('um—uh', 1, 1),
])
def test_only_standalone_supported_fillers(text, um, uh):
    result = measure_transcription(text, 'eng', [])
    assert (result.um_count, result.uh_count) == (um, uh)
    assert result.filler_unavailable_reason is None


@pytest.mark.parametrize('language', [None, 'fra', 'en', 'ENG', 'english', 'eng ', ''])
def test_unsupported_language_is_unavailable_not_zero(language):
    result = measure_transcription('um uh', language, [])
    assert result.um_count is None
    assert result.uh_count is None
    assert result.filler_unavailable_reason == 'unsupported_language'


def assert_unavailable(result, reason):
    assert result.timed_utterance_span_seconds is None
    assert result.estimated_words_per_minute is None
    assert result.timing_unavailable_reason == reason


def test_missing_timings():
    assert_unavailable(measure_transcription('one two', 'eng', []), 'missing_timings')


def test_complete_timings_known_pace_and_leading_silence_excluded():
    result = measure_transcription('One, two three!', 'eng', [
        timing('One,', 5.0, 5.5), timing('two', 5.5, 6.0), timing('three!', 7.0, 8.0),
    ])
    assert result.timed_utterance_span_seconds == 3.0
    assert result.estimated_words_per_minute == 60.0
    assert result.timing_unavailable_reason is None


@pytest.mark.parametrize('words', [
    [timing('one')], [timing('two')],
    [timing('one'), timing('other', 1.0, 2.0)],
    [timing('one'), timing('two', 1.0, 2.0), timing('extra', 2.0, 3.0)],
    [timing('one two')], [timing('One'), timing('two', 1.0, 2.0)],
])
def test_partial_or_mismatched_coverage_cannot_produce_pace(words):
    assert_unavailable(measure_transcription('one two', 'eng', words), 'timing_coverage_mismatch')


@pytest.mark.parametrize('words', [
    [timing('one', 2.0, 3.0), timing('two', 0.0, 1.0)],
    [timing('one', 0.0, 2.0), timing('two', 1.0, 3.0)],
    [timing('one', 0.0, 1.0), timing('two', 0.0, 1.0)],
])
def test_unordered_or_overlapping_timings_are_unavailable(words):
    assert_unavailable(measure_transcription('one two', 'eng', words), 'invalid_timing_order')


@pytest.mark.parametrize(('start', 'end'), [
    (-1.0, 1.0), (0.0, -1.0), (2.0, 1.0),
    (float('nan'), 1.0), (0.0, float('inf')), (float('-inf'), 1.0),
    (True, 1.0), ('0', 1.0),
])
def test_invalid_intervals_defensively_unavailable(start, end):
    word = SimpleNamespace(text='one', start=start, end=end)
    assert_unavailable(measure_transcription('one', 'eng', [word]), 'invalid_timing')


@pytest.mark.parametrize(('start', 'end'), [(0.0, 0.0), (2.0, 2.0), (0.0, 5e-324)])
def test_zero_or_unrepresentable_pace_is_unavailable(start, end):
    assert_unavailable(measure_transcription('one', 'eng', [timing('one', start, end)]), 'unusable_span')


def test_low_pace_is_a_value_not_unavailable():
    result = measure_transcription('one', 'eng', [timing('one', 0.0, 120.0)])
    assert result.estimated_words_per_minute == 0.5
    assert result.timing_unavailable_reason is None


def test_punctuation_only_timings_do_not_enter_word_count_or_span():
    result = measure_transcription('one two.', 'eng', [
        timing('!', 0.0, 1.0), timing('one', 5.0, 6.0),
        timing('two.', 6.0, 7.0), timing('.', 9.0, 10.0),
    ])
    assert result.recognized_word_count == 2
    assert result.timed_utterance_span_seconds == 2.0
    assert result.estimated_words_per_minute == 60.0
    assert_unavailable(measure_transcription('!', 'eng', [timing('!')]), 'missing_timings')


def test_contractions_and_hyphens_have_matching_timing_coverage():
    result = measure_transcription("I'm mother-in-law", 'eng', [
        timing("I'm"), timing('mother-in-law', 1.0, 2.0),
    ])
    assert result.estimated_words_per_minute == 60.0


def test_pure_measurement_does_not_mutate_original_or_use_edited_text():
    original = TranscriptionResult(text='Um one', language='eng', words=[
        timing('Um'), timing('one', 1.0, 2.0),
    ])
    before = original.model_dump()
    result = measure_transcription(original.text, original.language, original.words)
    edited_answer = 'A much longer edited answer with no filler tokens.'
    assert edited_answer != original.text
    assert original.model_dump() == before
    assert result == measure_transcription(original.text, original.language, original.words)
    assert result.recognized_word_count == 2
    assert result.um_count == 1
    assert result.estimated_words_per_minute == 60.0
    assert set(result.model_dump()) == {
        'source', 'recognized_word_count', 'um_count', 'uh_count', 'filler_unavailable_reason',
        'timed_utterance_span_seconds', 'estimated_words_per_minute', 'timing_unavailable_reason',
    }
    assert 'Um one' not in result.model_dump_json()
