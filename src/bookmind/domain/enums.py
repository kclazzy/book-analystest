"""Перечисления домена. Значения — латиница (хранятся в БД), подписи — по-русски (для интерфейса)."""
from __future__ import annotations

from enum import Enum


class LabeledEnum(str, Enum):
    @property
    def label(self) -> str:
        return _LABELS.get(self.value, self.value)


class EntityType(LabeledEnum):
    CHARACTER = "character"
    PLACE = "place"
    ORGANIZATION = "organization"
    STATE = "state"
    ITEM = "item"
    ABILITY = "ability"
    WORLD_RULE = "world_rule"
    HISTORICAL_EVENT = "historical_event"
    TERM = "term"


class FactKind(LabeledEnum):
    """Достоверность факта: явное указание / обоснованный вывод / гипотеза / противоречивое."""
    EXPLICIT = "explicit"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    CONTRADICTORY = "contradictory"


class Verification(LabeledEnum):
    VERIFIED = "verified"      # цитата найдена в оригинале
    UNVERIFIED = "unverified"  # цитаты нет или она не найдена: не считается подтверждённым фактом


class FactCategory(LabeledEnum):
    APPEARANCE = "appearance"
    AGE = "age"
    PERSONALITY = "personality"
    SKILL = "skill"
    WEAKNESS = "weakness"
    BIOGRAPHY = "biography"
    DESIRE = "desire"
    FEAR = "fear"
    VALUE = "value"
    SECRET = "secret"
    KNOWLEDGE = "knowledge"
    ACTION = "action"
    STATUS = "status"
    CHARACTER_CHANGE = "character_change"
    OTHER = "other"


class GoalStatus(LabeledEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ACHIEVED = "achieved"
    FAILED = "failed"
    POSTPONED = "postponed"
    CHANGED = "changed"
    CANCELLED = "cancelled"


class GoalType(LabeledEnum):
    STATED = "stated"              # заявлена персонажем/текстом
    DEMONSTRATED = "demonstrated"  # подтверждается действиями
    HYPOTHESIS = "hypothesis"      # гипотеза о скрытых намерениях


class NarrativeMode(LabeledEnum):
    LINEAR = "linear"
    FLASHBACK = "flashback"
    MEMORY = "memory"
    PARALLEL = "parallel"
    UNRELIABLE = "unreliable"


class QuestionKind(LabeledEnum):
    MYSTERY = "mystery"
    SECRET = "secret"
    PROPHECY = "prophecy"
    UNRESOLVED_THREAD = "unresolved_thread"


class QuestionStatus(LabeledEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    PARTIAL = "partial"


class MotifKind(LabeledEnum):
    DIRECT_EXPLANATION = "direct_explanation"  # прямое объяснение в тексте
    RECURRING_MOTIF = "recurring_motif"        # повтор в нескольких эпизодах
    INTERPRETATION = "interpretation"          # интерпретация модели
    HYPOTHESIS = "hypothesis"                  # неподтверждённая гипотеза


class ThreadStatus(LabeledEnum):
    OPEN = "open"
    DEVELOPING = "developing"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class TaskStatus(LabeledEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(LabeledEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Stage(LabeledEnum):
    CORE = "core"
    DEEP = "deep"
    CHAPTER_SUMMARY = "chapter_summary"


class ContradictionKind(LabeledEnum):
    CONTRADICTION = "contradiction"
    RETCON = "retcon"
    DECEPTION = "deception"
    CHANGE_OF_CIRCUMSTANCES = "change_of_circumstances"
    CONSISTENT = "consistent"
    UNRELATED = "unrelated"


_LABELS = {
    # типы сущностей
    "character": "Персонаж", "place": "Место", "organization": "Организация", "state": "Государство",
    "item": "Предмет", "ability": "Способность", "world_rule": "Правило мира",
    "historical_event": "Историческое событие", "term": "Термин",
    # достоверность
    "explicit": "Явно указано", "inference": "Обоснованный вывод", "hypothesis": "Гипотеза",
    "contradictory": "Противоречивые данные",
    "verified": "Подтверждено цитатой", "unverified": "Не подтверждено",
    # цели
    "unknown": "Неизвестна", "active": "Активна", "achieved": "Достигнута", "failed": "Провалена",
    "postponed": "Отложена", "changed": "Изменена", "cancelled": "Отменена",
    "stated": "Заявленная", "demonstrated": "Подтверждаемая действиями",
    # режим повествования
    "linear": "Линейно", "flashback": "Флешбэк", "memory": "Воспоминание",
    "parallel": "Параллельно", "unreliable": "Ненадёжный рассказчик",
    # вопросы
    "mystery": "Загадка", "secret": "Секрет", "prophecy": "Предсказание",
    "unresolved_thread": "Незакрытая линия", "open": "Открыт", "resolved": "Раскрыт", "partial": "Частично раскрыт",
    # мотивы
    "direct_explanation": "Прямое объяснение", "recurring_motif": "Повторяющийся мотив",
    "interpretation": "Интерпретация модели",
    # линии
    "developing": "Развивается", "abandoned": "Заброшена",
    # противоречия
    "contradiction": "Противоречие", "retcon": "Реткон", "deception": "Обман",
    "change_of_circumstances": "Изменение обстоятельств", "consistent": "Согласуется", "unrelated": "Не связаны",
}
