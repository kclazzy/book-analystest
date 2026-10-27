"""Схемы создания новой книги: параметры проекта и ответы модели."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from bookmind.domain.schemas import _Base, _str, _str_list

Divergence = Literal["weak", "medium", "strong"]
DIVERGENCE_LABELS = {"weak": "слабое (та же структура, новый мир и имена)",
                     "medium": "среднее (часть поворотов меняется)",
                     "strong": "сильное (другой исход и другая оболочка)"}
KEEP_KINDS = ("mystery", "relationship", "goal", "thread", "flashback")


class AdaptParams(BaseModel):
    divergence: Divergence = "medium"
    chapters: int | None = None            # None — столько же, сколько глав в оригинале
    genre: str = ""
    setting: str = ""                      # пожелания к миру/эпохе; пусто — модель придумает сама
    keep: list[str] = Field(default_factory=list)   # какие типы элементов сохранить (см. KEEP_KINDS)
    seed: int | None = None
    pause_after_plan: bool = False         # остановиться на проверку плана перед написанием текста
    max_replans: int = 2                   # сколько раз пересоставлять главу, не прошедшую проверку
    source_book_ids: list[str] = Field(default_factory=list)   # пусто — все книги серии
    approved: bool = False
    chapter_words: int = 1200              # ориентир длины главы при написании текста


class Premise(_Base):
    ALIASES = {"title": ("name", "book_title", "название"),
               "logline": ("premise", "summary", "description", "synopsis", "pitch", "краткое описание", "описание", "завязка"),
               "central_conflict": ("conflict", "main_conflict", "конфликт", "центральный конфликт"),
               "setting": ("world", "world_setting", "мир", "место действия"), "tone": ("mood", "atmosphere", "тон", "атмосфера"),
               "genre": ("жанр",)}
    title: str = ""
    logline: str = ""
    genre: str = ""
    setting: str = ""
    tone: str = ""
    central_conflict: str = ""
    themes: list[str] = Field(default_factory=list)
    world_rules: list[str] = Field(default_factory=list)

    @field_validator("title", "logline", "genre", "setting", "tone", "central_conflict", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _str(v)

    @field_validator("themes", "world_rules", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return _str_list(v)


class CastMember(_Base):
    role: str = ""
    name: str = ""
    appearance: str = ""
    personality: str = ""
    goal: str = ""
    weakness: str = ""
    secret: str = ""
    backstory: str = ""

    @field_validator("role", "name", "appearance", "personality", "goal", "weakness", "secret", "backstory", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _str(v)


class Cast(_Base):
    members: list[CastMember] = Field(default_factory=list)


class ChapterPlan(_Base):
    ALIASES = {"title": ("name", "chapter_title", "chapter_name", "heading", "название", "заголовок"),
               "summary": ("description", "synopsis", "content", "plot", "overview", "what_happens", "scene", "events",
                           "содержание", "описание", "краткое содержание", "сюжет"),
               "pov": ("point_of_view", "narrator", "точка зрения"), "location": ("setting", "place", "место"),
               "goal": ("objective", "цель"), "conflict": ("obstacle", "obstacles", "конфликт", "препятствие"),
               "outcome": ("result", "resolution", "итог", "результат"), "hook": ("cliffhanger", "ending", "крючок")}
    title: str = ""
    summary: str = ""
    pov: str = ""
    location: str = ""
    goal: str = ""
    conflict: str = ""
    outcome: str = ""
    characters: list[str] = Field(default_factory=list)
    relationship_changes: list[str] = Field(default_factory=list)
    clues_planted: list[str] = Field(default_factory=list)
    reveals: list[str] = Field(default_factory=list)
    hook: str = ""
    beats_covered: list[str] = Field(default_factory=list)

    @field_validator("title", "summary", "pov", "location", "goal", "conflict", "outcome", "hook", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _str(v)

    @field_validator("characters", "relationship_changes", "clues_planted", "reveals", "beats_covered", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return _str_list(v)

    def text(self) -> str:
        return " ".join([self.title, self.summary, self.goal, self.conflict, self.outcome, self.hook, *self.relationship_changes,
                         *self.clues_planted, *self.reveals])
