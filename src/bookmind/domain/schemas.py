"""Схемы данных, которые возвращает модель. Валидаторы снисходительны к синонимам значений (в том числе русским),
но неизвестные значения не угадываются: подставляется безопасное значение по умолчанию."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bookmind.domain.enums import (
    EntityType, FactCategory, FactKind, GoalStatus, GoalType, MotifKind, NarrativeMode, QuestionKind,
    QuestionStatus, ThreadStatus, ContradictionKind,
)

_SYNONYMS: dict[str, dict[str, str]] = {
    "EntityType": {"персонаж": "character", "герой": "character", "человек": "character", "место": "place",
                   "локация": "place", "организация": "organization", "гильдия": "organization", "орден": "organization",
                   "государство": "state", "страна": "state", "предмет": "item", "артефакт": "item",
                   "способность": "ability", "магия": "ability", "правило": "world_rule", "правило мира": "world_rule",
                   "исторический": "historical_event", "термин": "term", "person": "character", "location": "place"},
    "FactKind": {"явно": "explicit", "явное": "explicit", "вывод": "inference", "гипотеза": "hypothesis",
                 "противоречие": "contradictory"},
    "FactCategory": {"внешность": "appearance", "возраст": "age", "характер": "personality", "навык": "skill",
                     "способность": "skill", "слабость": "weakness", "биография": "biography", "желание": "desire",
                     "страх": "fear", "ценность": "value", "секрет": "secret", "знание": "knowledge",
                     "поступок": "action", "действие": "action", "статус": "status", "изменение": "character_change"},
    "GoalStatus": {"неизвестна": "unknown", "активна": "active", "достигнута": "achieved", "провалена": "failed",
                   "отложена": "postponed", "изменена": "changed", "отменена": "cancelled"},
    "GoalType": {"заявленная": "stated", "заявлена": "stated", "подтверждается действиями": "demonstrated",
                 "гипотеза": "hypothesis"},
    "NarrativeMode": {"линейно": "linear", "флешбэк": "flashback", "воспоминание": "memory",
                      "параллельно": "parallel", "ненадёжный": "unreliable"},
    "QuestionKind": {"загадка": "mystery", "секрет": "secret", "тайна": "secret", "предсказание": "prophecy",
                     "пророчество": "prophecy", "линия": "unresolved_thread"},
    "QuestionStatus": {"открыт": "open", "открыта": "open", "раскрыт": "resolved", "раскрыта": "resolved",
                       "частично": "partial"},
    "MotifKind": {"прямое объяснение": "direct_explanation", "мотив": "recurring_motif", "повтор": "recurring_motif",
                  "интерпретация": "interpretation", "гипотеза": "hypothesis"},
    "ThreadStatus": {"открыта": "open", "развивается": "developing", "завершена": "resolved", "заброшена": "abandoned"},
    "ContradictionKind": {"противоречие": "contradiction", "реткон": "retcon", "обман": "deception",
                          "изменение обстоятельств": "change_of_circumstances", "согласуется": "consistent"},
}


def _coerce(enum_cls, value: Any, default):
    if isinstance(value, enum_cls):
        return value
    if value is None:
        return default
    key = str(value).strip().casefold()
    for member in enum_cls:
        if key == member.value:
            return member
    mapped = _SYNONYMS.get(enum_cls.__name__, {}).get(key)
    return enum_cls(mapped) if mapped else default


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v if x not in (None, ""))
    return str(v).strip()


def _str_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", use_enum_values=False)


def _enum_field(name: str, enum_cls, default):
    """Фабрика валидатора для поля с перечислением."""
    return field_validator(name, mode="before")(classmethod(lambda cls, v: _coerce(enum_cls, v, default)))


def _clamp(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


class EntityItem(_Base):
    name: str
    type: EntityType = EntityType.CHARACTER
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    quote: str = ""
    v_type = _enum_field("type", EntityType, EntityType.TERM)

    @field_validator("name", "description", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("aliases", mode="before")
    @classmethod
    def _al(cls, v):
        return _str_list(v)


class FactItem(_Base):
    entity: str
    category: FactCategory = FactCategory.OTHER
    statement: str
    kind: FactKind = FactKind.EXPLICIT
    quote: str = ""
    confidence: float = 0.7
    v_category = _enum_field("category", FactCategory, FactCategory.OTHER)
    v_kind = _enum_field("kind", FactKind, FactKind.INFERENCE)

    @field_validator("entity", "statement", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _cf(cls, v):
        return _clamp(v)


class ParticipantItem(_Base):
    name: str
    role: str = ""

    @field_validator("name", "role", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)


class EventItem(_Base):
    description: str
    participants: list[ParticipantItem] = Field(default_factory=list)
    causes: str = ""
    consequences: str = ""
    place: str = ""
    story_time: str = ""
    narrative_mode: NarrativeMode = NarrativeMode.LINEAR
    confidence: float = 0.7
    quote: str = ""
    v_narrative_mode = _enum_field("narrative_mode", NarrativeMode, NarrativeMode.LINEAR)

    @field_validator("description", "causes", "consequences", "place", "story_time", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("participants", mode="before")
    @classmethod
    def _p(cls, v):
        out = []
        for p in v or []:
            out.append({"name": p} if isinstance(p, str) else p)
        return out

    @field_validator("confidence", mode="before")
    @classmethod
    def _cf(cls, v):
        return _clamp(v)


class CoreAnalysis(_Base):
    summary: str = ""
    entities: list[EntityItem] = Field(default_factory=list)
    facts: list[FactItem] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("entities", "facts", "events", mode="before")
    @classmethod
    def _l(cls, v):
        return v or []


class GoalItem(_Base):
    character: str
    description: str
    origin: str = ""
    motivation: str = ""
    desired_result: str = ""
    obstacles: str = ""
    methods: str = ""
    allies: str = ""
    opponents: str = ""
    goal_type: GoalType = GoalType.STATED
    status: GoalStatus = GoalStatus.ACTIVE
    change_note: str = ""
    quote: str = ""
    v_goal_type = _enum_field("goal_type", GoalType, GoalType.HYPOTHESIS)
    v_status = _enum_field("status", GoalStatus, GoalStatus.UNKNOWN)

    @field_validator("character", "description", "origin", "motivation", "desired_result", "obstacles", "methods",
                     "allies", "opponents", "change_note", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)


class RelationshipItem(_Base):
    a: str
    b: str
    rel_type: str
    state: str = ""
    description: str = ""
    change_note: str = ""
    quote: str = ""

    @field_validator("a", "b", "rel_type", "state", "description", "change_note", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)


class ThreadItem(_Base):
    title: str
    description: str = ""
    status: ThreadStatus = ThreadStatus.OPEN
    events: list[str] = Field(default_factory=list)
    quote: str = ""
    v_status = _enum_field("status", ThreadStatus, ThreadStatus.OPEN)

    @field_validator("title", "description", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("events", mode="before")
    @classmethod
    def _e(cls, v):
        return _str_list(v)


class QuestionItem(_Base):
    kind: QuestionKind = QuestionKind.MYSTERY
    text: str
    status: QuestionStatus = QuestionStatus.OPEN
    clues: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    resolution: str = ""
    quote: str = ""
    v_kind = _enum_field("kind", QuestionKind, QuestionKind.MYSTERY)
    v_status = _enum_field("status", QuestionStatus, QuestionStatus.OPEN)

    @field_validator("text", "resolution", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("clues", "hypotheses", mode="before")
    @classmethod
    def _l(cls, v):
        return _str_list(v)


class MotifItem(_Base):
    name: str
    kind: MotifKind = MotifKind.RECURRING_MOTIF
    description: str = ""
    quote: str = ""
    v_kind = _enum_field("kind", MotifKind, MotifKind.HYPOTHESIS)

    @field_validator("name", "description", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)


class DeepAnalysis(_Base):
    goals: list[GoalItem] = Field(default_factory=list)
    relationships: list[RelationshipItem] = Field(default_factory=list)
    plot_threads: list[ThreadItem] = Field(default_factory=list)
    questions: list[QuestionItem] = Field(default_factory=list)
    motifs: list[MotifItem] = Field(default_factory=list)

    @field_validator("goals", "relationships", "plot_threads", "questions", "motifs", mode="before")
    @classmethod
    def _l(cls, v):
        return v or []


class ChapterSummary(_Base):
    summary: str
    key_points: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)

    @field_validator("summary", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("key_points", "open_threads", mode="before")
    @classmethod
    def _l(cls, v):
        return _str_list(v)


class ContradictionJudgement(_Base):
    relation: ContradictionKind = ContradictionKind.UNRELATED
    explanation: str = ""
    v_relation = _enum_field("relation", ContradictionKind, ContradictionKind.UNRELATED)

    @field_validator("explanation", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)


class QuestionReview(_Base):
    status: QuestionStatus = QuestionStatus.OPEN
    resolution: str = ""
    new_clues: list[str] = Field(default_factory=list)
    quote: str = ""
    v_status = _enum_field("status", QuestionStatus, QuestionStatus.OPEN)

    @field_validator("resolution", "quote", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("new_clues", mode="before")
    @classmethod
    def _l(cls, v):
        return _str_list(v)


class AnswerDraft(_Base):
    answer: str
    sources: list[str] = Field(default_factory=list)
    insufficient: bool = False

    @field_validator("answer", mode="before")
    @classmethod
    def _s(cls, v):
        return _str(v)

    @field_validator("sources", mode="before")
    @classmethod
    def _l(cls, v):
        return _str_list(v)


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema модели с развёрнутыми $ref (проще для структурированного вывода Ollama)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return resolve(defs[name])
            return {k: resolve(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
