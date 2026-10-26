import json

import pytest
from pydantic import ValidationError

from bookmind.domain.enums import EntityType, FactCategory, FactKind, GoalStatus
from bookmind.domain.schemas import CoreAnalysis, DeepAnalysis, FactItem, GoalItem, inline_schema


def test_enum_synonyms_and_unknown_defaults():
    c = CoreAnalysis.model_validate({
        "entities": [{"name": "Мира", "type": "Персонаж"}, {"name": "X", "type": "нечто странное"}],
        "facts": [{"entity": "Мира", "category": "внешность", "statement": "рыжая", "kind": "неизвестно"}]})
    assert c.entities[0].type == EntityType.CHARACTER
    assert c.entities[1].type == EntityType.TERM            # неизвестное значение не угадывается
    assert c.facts[0].category == FactCategory.APPEARANCE
    assert c.facts[0].kind == FactKind.INFERENCE            # неизвестная достоверность не повышается до «явно»


def test_nulls_extra_fields_and_clamping():
    c = CoreAnalysis.model_validate({"summary": None, "entities": None, "foo": 1,
                                     "facts": [{"entity": "A", "statement": "b", "confidence": 7, "bar": 1}],
                                     "events": [{"description": "e", "participants": ["Мира"]}]})
    assert c.summary == "" and c.entities == [] and c.facts[0].confidence == 1.0
    assert c.events[0].participants[0].name == "Мира"


def test_required_fields_enforced():
    with pytest.raises(ValidationError):
        FactItem.model_validate({"entity": "A"})
    with pytest.raises(ValidationError):
        GoalItem.model_validate({"description": "x"})


def test_goal_status_russian():
    g = GoalItem.model_validate({"character": "Мира", "description": "месть", "status": "отложена"})
    assert g.status == GoalStatus.POSTPONED
    assert DeepAnalysis.model_validate({}).goals == []


def test_inline_schema_has_no_refs():
    schema = inline_schema(CoreAnalysis)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    assert "enum" in json.dumps(schema)
