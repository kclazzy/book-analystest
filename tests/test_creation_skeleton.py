import json

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.creation.skeleton import SkeletonBuilder
from tests.fixtures.scripted_llm import ScriptedLLM

ORIGINAL_WORDS = [r"Мир[аеуы]", r"Мирой", r"Алари", r"Эльдар", r"Тихон", r"Лин[аыуе]\b", r"Линой", r"Ветров", r"Бреве", r"Вейн", r"Корн", r"Гильди", r"Западн", r"реестр"]


@pytest.fixture
def analysed(db, book1, model, settings, data_dir):
    p = AnalysisPipeline(db, ScriptedLLM(), settings, data_dir)
    assert p.run(p.start(book1.book_id, model, stages=("core", "deep", "chapter_summary"))).status == "completed"
    return db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))


def test_roles_by_function_not_by_name(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    labels = sk.role_labels()
    assert labels[0] == "Протагонист"
    assert {"Наставник", "Союзник-предатель", "Антагонист", "Близкий человек"} <= set(labels)
    assert sk.source_chapters == 8


def test_skeleton_contains_no_original_names(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    import re
    blob = sk.model_dump_json()
    for w in ORIGINAL_WORDS:
        assert not re.search(r"(?<!\w)" + w, blob, re.IGNORECASE), w


def test_beats_are_ordered_with_valid_positions(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    pos = [b.position for b in sk.beats]
    assert pos == sorted(pos) and all(0 < p < 1 for p in pos)
    assert [b.id for b in sk.beats] == [f"B{i:02d}" for i in range(1, len(sk.beats) + 1)]
    kinds = {b.kind for b in sk.beats}
    assert {"goal_set", "goal_change", "relationship_shift", "mystery_raised", "clue", "flashback", "thread_open"} <= kinds
    assert all(b.abstract and b.detailed for b in sk.beats)


def test_mystery_group_clues_between_raise_and_resolve(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    groups: dict[str, list] = {}
    for b in sk.beats:
        if b.group.startswith("Q"):
            groups.setdefault(b.group, []).append(b)
    assert groups
    for beats in groups.values():
        raised = next(b for b in beats if b.kind == "mystery_raised")
        for b in beats:
            if b.kind == "clue":
                assert b.position > raised.position
            if b.kind == "mystery_resolved":
                assert b.position > raised.position
    assert any(b.kind == "clue" for beats in groups.values() for b in beats)


def test_relationship_reversal_is_captured(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    shifts = [b.abstract for b in sk.beats if b.kind == "relationship_shift" and "Союзник-предатель" in b.roles]
    assert any("друг → враг" in s for s in shifts)


def test_goal_status_change_beat_has_postponed(db, analysed):
    sk, _ = SkeletonBuilder(db).build(analysed)
    assert any("активна → отложена" in b.abstract for b in sk.beats)


def test_requires_analysis(db, book1):
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    with pytest.raises(ValueError, match="проанализированных"):
        SkeletonBuilder(db).build(sid)
