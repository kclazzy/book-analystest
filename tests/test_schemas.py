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


# ---------- устойчивость глубокого анализа к неточным ответам модели ----------
def test_threads_without_title_use_alternative_keys_or_are_skipped():
    from bookmind.llm.json_parser import parse_model_json
    from bookmind.domain.schemas import DeepAnalysis
    raw = ('{"plot_threads": [{"name": "Расследование убийства", "description": "Мира ищет убийцу"}, '
           '{"thread": "Болезнь Лины"}, "Тайна ключа", {"description": "без названия, но с текстом"}, {"status": "open"}, 42],'
           ' "goals": [{"goal": "найти убийцу", "name": "Мира"}], "questions": [{"question": "Кто убил Аларика?"}],'
           ' "motifs": [{"motif": "огонь"}], "relationships": [{"a": "Мира", "b": "Лина", "type": "сестра"}]}')
    deep, issues = parse_model_json(raw, DeepAnalysis)
    assert [t.title for t in deep.plot_threads] == ["Расследование убийства", "Болезнь Лины", "Тайна ключа"]
    assert deep.plot_threads[0].description == "Мира ищет убийцу"
    assert deep.goals[0].character == "Мира" and deep.goals[0].description == "найти убийцу"
    assert deep.questions[0].text == "Кто убил Аларика?" and deep.motifs[0].name == "огонь" and deep.relationships[0].rel_type == "сестра"
    skipped = [i for i in issues if "пропущен" in i]
    assert len(skipped) == 3 and any("plot_threads[3]" in i for i in skipped) and any("plot_threads[5]" in i for i in skipped)


def test_wrong_container_types_do_not_crash_deep_analysis():
    from bookmind.llm.json_parser import parse_model_json
    from bookmind.domain.schemas import DeepAnalysis
    deep, issues = parse_model_json('{"plot_threads": "нет", "goals": {"character": "Мира", "description": "цель"}, "motifs": null}', DeepAnalysis)
    assert deep.plot_threads == [] and len(deep.goals) == 1 and deep.motifs == [] and any("ожидался список" in i for i in issues)
    clean, issues2 = parse_model_json('{"goals": []}', DeepAnalysis)
    assert issues2 == [] and clean.dropped == []
