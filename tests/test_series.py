import json

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.purge import purge_book_analysis
from tests.fixtures import scripted_llm as sl
from tests.fixtures.builders import write_txt
from tests.fixtures.novel import BOOK2, BOOK2_TITLE
from tests.fixtures.scripted_llm import ScriptedLLM

ALL = AnalysisPipeline.STAGES_ORDER
PROPHECY = "Когда огонь на башне погаснет, ключ найдёт свой замок"


def contradiction_handler(prompt):
    if "серые" in prompt and "карие" in prompt:
        return {"relation": "contradiction", "explanation": "Цвет глаз Эльдара назван по-разному в двух томах."}
    return {"relation": "unrelated", "explanation": ""}


def review_handler(prompt):
    if "ключ найдёт свой замок" in prompt and "Теперь ключ нашёл свой замок" in prompt:
        return {"status": "resolved", "resolution": "Ключ открыл подвал башни", "new_clues": [],
                "quote": "Теперь ключ нашёл свой замок"}
    return {"status": "open", "resolution": "", "new_clues": [], "quote": ""}


@pytest.fixture
def two_books(db, library, settings, data_dir, tmp_path, book1, model):
    llm = ScriptedLLM(extra={"contradiction": {"handler": contradiction_handler},
                             "question_review": {"handler": review_handler}})
    p = AnalysisPipeline(db, llm, settings, data_dir)
    s1 = p.run(p.start(book1.book_id, model, stages=ALL))
    assert s1.status == "completed", s1
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    book2 = library.import_book(write_txt(tmp_path / "book2.txt", BOOK2), title=BOOK2_TITLE, series_id=sid)
    s2 = p.run(p.start(book2.book_id, model, stages=ALL))
    assert s2.status == "completed", s2
    return p, llm, sid, book1, book2


def _eid(db, name):
    return db.scalar("SELECT id FROM entities WHERE name = ?", (name,))


def test_entities_are_shared_across_volumes(two_books, db):
    _, _, sid, _, _ = two_books
    assert db.scalar("SELECT COUNT(*) FROM entities WHERE name = 'Эльдар Корн'") == 1
    assert db.scalar("SELECT COUNT(*) FROM books WHERE series_id = ?", (sid,)) == 2


def test_eye_colour_contradiction_found_with_sources_from_both_volumes(two_books, db):
    _, _, sid, _, _ = two_books
    found = KnowledgeService(db).contradictions(sid)
    assert len(found) == 1
    c = found[0]
    assert c["entity_name"] == "Эльдар Корн" and c["kind"] == "contradiction" and c["kind_label"]
    assert "серые" in c["a"]["statement"] and "карие" in c["b"]["statement"]
    assert c["a"]["series_order"] == 1 and c["b"]["series_order"] == 2
    assert c["a"]["sources"] and c["b"]["sources"]
    assert "бледными серыми глазами" in c["a"]["sources"][0]["quote"] and "тёмные карие" in c["b"]["sources"][0]["quote"]


def test_contradiction_check_is_not_repeated(two_books, db, model):
    p, llm, sid, _, _ = two_books
    assert llm.count("contradiction") > 0
    eldar = _eid(db, "Эльдар Корн")
    assert p.series.candidate_pairs(eldar) == []          # все пары уже оценены
    before = llm.count("contradiction")
    raw, _, apply = p.series.contradiction_task(eldar, model)
    apply()
    assert llm.count("contradiction") == before and len(KnowledgeService(db).contradictions(sid)) == 1


def test_dismissed_contradiction_leaves_pending_list(two_books, db):
    _, _, sid, _, _ = two_books
    ks = KnowledgeService(db)
    ks.set_contradiction_status(ks.contradictions(sid)[0]["id"], "dismissed")
    assert ks.contradictions(sid) == []
    assert len(ks.contradictions(sid, status="dismissed")) >= 1


def test_open_prophecy_from_volume_one_is_resolved_with_verified_quote(two_books, db):
    _, _, sid, _, book2 = two_books
    q = next(q for q in KnowledgeService(db).questions(sid) if q["text"] == PROPHECY)
    assert q["status"] == "resolved" and q["resolution"] == "Ключ открыл подвал башни"
    assert q["resolved_at"]["book_title"] == BOOK2_TITLE
    assert db.scalar("SELECT COUNT(*) FROM change_log WHERE owner_type='question' AND owner_id=? AND new_value='resolved'", (q["id"],)) == 1


def test_unanswered_question_stays_open_not_invented(two_books, db):
    _, _, sid, _, _ = two_books
    left = KnowledgeService(db).questions(sid, status="open")
    assert left and all(q["resolution"] == "" and q["resolved_at"] is None for q in left)


def test_unverified_review_quote_is_rejected(db, library, settings, data_dir, tmp_path, book1, model):
    def bad(prompt):
        return {"status": "resolved", "resolution": "выдумка", "new_clues": [], "quote": "Этой фразы нет в тексте нигде"}
    llm = ScriptedLLM(extra={"contradiction": {"handler": contradiction_handler}, "question_review": {"handler": bad}})
    p = AnalysisPipeline(db, llm, settings, data_dir)
    p.run(p.start(book1.book_id, model, stages=("core", "deep")))
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    b2 = library.import_book(write_txt(tmp_path / "b2.txt", BOOK2), title=BOOK2_TITLE, series_id=sid)
    p.run(p.start(b2.book_id, model, stages=("core", "series")))
    q = db.one("SELECT * FROM open_questions WHERE text = ?", (PROPHECY,))
    assert q["status"] == "partial" and q["resolution"] != "выдумка"


def test_character_state_per_volume(two_books, db):
    _, _, sid, _, _ = two_books
    ks = KnowledgeService(db)
    mira = _eid(db, "Мира Ветрова")
    s1, s2 = ks.state_at(mira, 1), ks.state_at(mira, 2)
    g1 = {g["description"]: g["status"] for g in s1["goals"]}
    g2 = {g["description"]: g["status"] for g in s2["goals"]}
    assert g1[sl.REVENGE] == "postponed" and g2[sl.REVENGE] == "active"
    assert g1[sl.PROTECT] == "active" and g2[sl.PROTECT] == "achieved"
    tikhon1 = {r["with"]: r["type"] for r in s1["relationships"]}["Тихон Бреве"]
    tikhon2 = {r["with"]: r["type"] for r in s2["relationships"]}["Тихон Бреве"]
    assert (tikhon1, tikhon2) == ("враг", "союзник")
    assert s1["series_order"] == 1 and s2["series_order"] == 2


def test_state_does_not_leak_future_knowledge(two_books, db):
    _, _, sid, _, _ = two_books
    ks = KnowledgeService(db)
    eldar = _eid(db, "Эльдар Корн")
    d1 = ks.character_dossier(eldar, up_to_book_order=1)
    d2 = ks.character_dossier(eldar, up_to_book_order=2)
    eyes1 = [f["statement"] for f in d1["facts"]["appearance"]]
    assert eyes1 == ["У Эльдара бледные серые глаза"]
    assert len(d2["facts"]["appearance"]) == 2
    assert ks.state_at(eldar, 0) is None
    goals1 = ks.goals(sid, up_to_book_order=1)
    assert not any(g["description"] == sl.TIKHON_GOAL and g["status"] == "failed" for g in goals1)
    assert [g["status"] for g in ks.goals(sid, up_to_book_order=2) if g["description"] == sl.TIKHON_GOAL] == ["failed"]


def test_purging_second_volume_restores_first(two_books, db, model):
    _, _, sid, book1, book2 = two_books
    purge_book_analysis(db, book2.book_id)
    ks = KnowledgeService(db)
    assert ks.contradictions(sid, status=None) == [] or all(c["a"]["series_order"] == 1 and c["b"]["series_order"] == 1 for c in ks.contradictions(sid, status=None))
    q = next(q for q in ks.questions(sid) if q["text"] == PROPHECY)
    assert q["status"] == "partial"
    assert db.scalar("SELECT COUNT(*) FROM entity_states WHERE book_id = ?", (book2.book_id,)) == 0
    assert db.scalar("SELECT COUNT(*) FROM entity_states WHERE book_id = ?", (book1.book_id,)) > 0
    mira = ks.goals(sid, entity_id=_eid(db, "Мира Ветрова"))
    assert {g["description"]: g["status"] for g in mira}[sl.REVENGE] == "postponed"
    assert db.integrity_report()["ok"]
