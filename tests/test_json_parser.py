import pytest

from bookmind.domain.exceptions import ModelOutputError
from bookmind.domain.schemas import CoreAnalysis
from bookmind.llm.json_parser import load_json_object, parse_model_json


def test_plain_and_fenced_and_think():
    assert load_json_object('{"a": 1}')[0] == {"a": 1}
    data, issues = load_json_object('Вот ответ:\n```json\n{"a": 1}\n```\nготово')
    assert data == {"a": 1} and issues
    assert load_json_object('<think>рассуждаю {не json}</think>{"b": 2}')[0] == {"b": 2}


def test_trailing_commas_repaired():
    data, issues = load_json_object('{"a": [1, 2,], "b": {"c": 3,},}')
    assert data == {"a": [1, 2], "b": {"c": 3}} and issues


def test_truncated_json_closed_and_flagged():
    raw = '{"summary": "кратко", "facts": [{"entity": "Мира", "statement": "рыжая"}, {"entity": "Ти'
    data, issues = load_json_object(raw)
    assert data["summary"] == "кратко" and data["facts"][0]["entity"] == "Мира"
    assert any("обрезан" in i for i in issues)


def test_braces_inside_strings():
    assert load_json_object('{"a": "}{", "b": 1}')[0] == {"a": "}{", "b": 1}


def test_garbage_raises_with_raw():
    with pytest.raises(ModelOutputError) as e:
        load_json_object("извините, не могу")
    assert e.value.raw == "извините, не могу"


def test_truncated_right_after_key_is_flagged():
    data, issues = load_json_object('{"summary": "x", "facts": ')
    assert data == {"summary": "x"} and any("обрезан" in i for i in issues)


def test_schema_mismatch_raises():
    with pytest.raises(ModelOutputError) as e:
        parse_model_json('{"facts": [{"entity": "A"}]}', CoreAnalysis)
    assert "схем" in str(e.value)


def test_valid_object_parsed():
    obj, _ = parse_model_json('{"summary": "x", "facts": []}', CoreAnalysis)
    assert obj.summary == "x"
