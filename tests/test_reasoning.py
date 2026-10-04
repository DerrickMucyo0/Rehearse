import json

import pytest

from app.reasoning import Decision, InvalidDecision, parse_decision


@pytest.mark.parametrize('action', ['FOLLOW_UP', 'CLARIFY', 'CHALLENGE', 'MOVE_ON'])
def test_valid_actions(action):
    result = parse_decision(json.dumps({'action': action, 'reason': '  Useful detail.  ',
                                       'next_prompt': None if action == 'MOVE_ON' else '  Explain?  '}))
    assert result.reason == 'Useful detail.'
    assert result.next_prompt == (None if action == 'MOVE_ON' else 'Explain?')


@pytest.mark.parametrize('change', [
    {'action': 'CHAT'}, {'action': 'move_on'}, {'action': 1}, {'reason': ''},
    {'reason': ' '}, {'reason': 'x' * 301}, {'reason': True}, {'reason': None},
    {'next_prompt': 'unexpected'}, {'extra': 'no'},
])
def test_reject_invalid_fields(change):
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps({'action': 'MOVE_ON', 'reason': 'Complete.', 'next_prompt': None, **change}))


@pytest.mark.parametrize('field', ['action', 'reason', 'next_prompt'])
def test_all_fields_required(field):
    value = {'action': 'MOVE_ON', 'reason': 'Complete.', 'next_prompt': None}
    del value[field]
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps(value))


@pytest.mark.parametrize('prompt', [None, '', ' ', 'x' * 501, 123, False, [], {}])
def test_probe_requires_short_nonempty_prompt(prompt):
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps({'action': 'FOLLOW_UP', 'reason': 'Incomplete.', 'next_prompt': prompt}))


@pytest.mark.parametrize('value', [
    '', '{} trailing', '```json\n{}\n```', 'Here is {}', '[]', 'null',
    '{"action":"MOVE_ON","action":"MOVE_ON","reason":"ok","next_prompt":null}',
    '{"action":"MOVE_ON","reason":NaN,"next_prompt":null}', 'x' * 8193,
])
def test_reject_invalid_json_without_repair(value):
    with pytest.raises(InvalidDecision):
        parse_decision(value)


def test_constructed_instance_cannot_bypass_validation():
    with pytest.raises(ValueError):
        Decision.model_validate(Decision.model_construct(action='UNKNOWN', reason='ok', next_prompt=None))


@pytest.mark.parametrize('content,code', [
    (None, 'content_type'), ({}, 'content_type'),
    ('x' * 8193, 'content_size'),
    ('PRIVATE malformed', 'json_syntax'),
    ('{"reason":"PRIVATE","reason":"PRIVATE"}', 'duplicate_json_key'),
    ('{"reason":NaN}', 'non_json_constant'),
    ('{"reason":Infinity}', 'non_json_constant'),
    ('[]', 'schema_validation'),
    ('{"action":"CHAT","reason":"PRIVATE","next_prompt":null}', 'schema_validation'),
    ('{"action":"CHALLENGE","reason":"PRIVATE","next_prompt":null}', 'schema_validation'),
    ('{"action":"MOVE_ON","reason":"PRIVATE","next_prompt":"PRIVATE"}', 'schema_validation'),
    ('{"action":"MOVE_ON","reason":"","next_prompt":null}', 'schema_validation'),
    ('{"action":"MOVE_ON","reason":true,"next_prompt":null}', 'schema_validation'),
    ('{"action":"MOVE_ON","reason":"PRIVATE","next_prompt":null,"extra":"PRIVATE"}', 'schema_validation'),
])
def test_invalid_decision_codes_never_contain_content(content, code):
    with pytest.raises(InvalidDecision) as caught:
        parse_decision(content)
    assert caught.value.invalid_reason == code
    assert caught.value.args == ()
    assert str(caught.value) == ''
    assert caught.value.failure is None
    assert 'PRIVATE' not in json.dumps(vars(caught.value))


def test_invalid_reason_allowlist_is_closed():
    with pytest.raises(ValueError):
        InvalidDecision(invalid_reason='PRIVATE arbitrary text')
    assert InvalidDecision().invalid_reason is None
