import copy
import json

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.purge import purge_book_analysis
from tests.fixtures import scripted_llm as sl
from tests.fixtures.scripted_llm import ScriptedLLM

ALL = AnalysisPipeline.STAGES_ORDER


@pytest.fixture
def analyzed(pipeline, book1, model, db):
    run_id = pipeline.start(book1.book_id, model, stages=ALL)
    summary = pipeline.run(run_id)
    assert summary.status == "completed", summary
    series_id = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    return run_id, series_id, KnowledgeService(db)


def _eid(db, name):
    return db.scalar("SELECT id FROM entities WHERE name = ?", (name,))


def test_all_stages_complete_with_expected_task_counts(analyzed, db):
    run_id, _, _ = analyzed
    counts = {r["stage"]: r["n"] for r in db.query("SELECT stage, COUNT(*) AS n FROM analysis_tasks WHERE run_id=? AND status='done' GROUP BY stage", (run_id,))}
    assert counts == {"core": 8, "deep": 8, "chapter_summary": 8, "arc_summary": 2}


def test_goal_history_is_preserved_and_status_changes_are_logged(analyzed, db):
    _, sid, ks = analyzed
    mira = _eid(db, "Мира Ветрова")
    goals = {g["description"]: g for g in ks.goals(sid, entity_id=mira)}
    assert set(goals) == {sl.REVENGE, sl.PROTECT}
    revenge = goals[sl.REVENGE]
    assert [(h["old_status"], h["new_status"]) for h in revenge["history"]] == [(None, "active"), ("active", "postponed")]
    assert revenge["status"] == "postponed" and revenge["history"][1]["note"] == "защита Лины важнее мести"
    assert revenge["motivation"] == "горе от потери наставника" and revenge["goal_type"] == "stated"
    assert goals[sl.PROTECT]["goal_type"] == "demonstrated" or goals[sl.PROTECT]["goal_type"] == "stated"  # повышен до заявленной в гл. 7
    assert goals[sl.PROTECT]["goal_type"] == "stated"
    assert revenge["origin_event_id"] is not None
    assert db.scalar("SELECT COUNT(*) FROM event_goals WHERE goal_id = ?", (revenge["id"],)) >= 1
    assert db.scalar("SELECT COUNT(*) FROM change_log WHERE owner_type='goal' AND owner_id = ?", (revenge["id"],)) == 1
    assert all(s["verified"] == 1 for s in revenge["sources"]) and len(revenge["sources"]) == 2
    assert db.scalar("SELECT COUNT(*) FROM goals") == 6
    achieved = ks.goals(sid, status="achieved")
    assert [g["entity_name"] for g in achieved] == ["Аларик Вейн"]


def test_relationship_changes_are_tracked(analyzed, db):
    _, sid, ks = analyzed
    rel = ks.relationship_between(_eid(db, "Мира Ветрова"), _eid(db, "Тихон Бреве"))
    assert [(h["rel_type"], h["state"]) for h in rel["history"]] == [("друг", "доверие"), ("враг", "предательство")]
    assert rel["history"][1]["note"] == "Тихон выдал ключ Эльдару" and rel["rel_type"] == "враг"
    eldar = ks.relationship_between(_eid(db, "Эльдар Корн"), _eid(db, "Мира Ветрова"))
    assert [h["rel_type"] for h in eldar["history"]] == ["недоверие", "враг"]
    assert len(ks.all_relationships(sid)) == 6


def test_open_questions_secrets_and_prophecies(analyzed):
    _, sid, ks = analyzed
    qs = {q["text"]: q for q in ks.questions(sid)}
    key = qs[sl.KEY_Q]
    assert key["status"] == "open" and key["kind"] == "mystery" and len(key["clues"]) == 3
    assert key["raised_at"]["chapter_title"].startswith("Глава 1") and key["resolved_at"] is None
    killer = qs[sl.KILLER_Q]
    assert killer["status"] == "partial" and killer["resolution"] == "Эльдар не отрицает причастность"
    prophecy = qs["Когда огонь на башне погаснет, ключ найдёт свой замок"]
    assert prophecy["kind"] == "prophecy" and prophecy["status"] == "partial"
    assert len(ks.questions(sid, status="open")) == 3  # ключ, «знал ли Эльдар», Огненный реестр


def test_motifs_need_several_episodes_to_count_as_recurring(analyzed, db):
    _, sid, ks = analyzed
    m = ks.motifs(sid)[0]
    assert m["name"] == "Пламя" and m["kind"] == "recurring_motif" and len(m["occurrences"]) == 2
    db.execute("DELETE FROM motif_occurrences WHERE id = (SELECT MIN(id) FROM motif_occurrences)")
    m = ks.motifs(sid)[0]
    assert m["claimed_kind"] == "recurring_motif" and m["kind"] == "hypothesis"  # один эпизод — только предположение


def test_plot_threads_updated_not_duplicated(analyzed):
    _, sid, ks = analyzed
    threads = ks.plot_threads(sid)
    assert [t["title"] for t in threads] == [sl.DEATH_THREAD, "Предательство Тихона"]


def test_chapter_and_arc_summaries_are_versioned_reports(analyzed, db):
    run_id, _, _ = analyzed
    assert db.scalar("SELECT COUNT(*) FROM report_versions WHERE kind='chapter_summary'") == 8
    arcs = db.query("SELECT title FROM report_versions WHERE kind='arc_summary' ORDER BY title")
    assert len(arcs) == 2 and arcs[0]["title"].startswith("Арка 1")
    # сводки не заменяют исходные факты и источники
    assert db.scalar("SELECT COUNT(*) FROM facts") > 20 and db.scalar("SELECT COUNT(*) FROM evidence WHERE verified = 1") > 40


def test_dossier_has_no_invented_fields(analyzed, db):
    _, sid, ks = analyzed
    mira = ks.character_dossier(_eid(db, "Мира Ветрова"))
    assert mira["first_appearance"]["chapter_title"].startswith("Глава 1")
    assert "appearance" in mira["facts"] and "age" not in mira["facts"]       # возраст Миры в тексте не назван
    assert mira["current_state"] is None                                       # неизвестное не заполняется
    assert [g["status"] for g in mira["goals"]] == ["postponed", "active"]
    assert {r["other_name"] for r in mira["relationships"]} == {"Лина", "Эльдар Корн", "Тихон Бреве", "Аларик Вейн"}
    assert all(f["verification"] == "verified" for fs in mira["facts"].values() for f in fs)
    assert not any("боевой магией" in f["statement"] for fs in mira["facts"].values() for f in fs)
    alarik = ks.character_dossier(_eid(db, "Аларик Вейн"))
    assert alarik["current_state"].startswith("Аларик Вейн убит")
    kinds = {f["kind"] for fs in mira["facts"].values() for f in fs}
    assert kinds <= {"explicit", "inference", "hypothesis", "contradictory"}


def test_inference_is_distinguished_from_explicit(analyzed, db):
    _, sid, ks = analyzed
    eldar = ks.character_dossier(_eid(db, "Эльдар Корн"))
    knowledge = {f["statement"]: f["kind"] for f in eldar["facts"]["knowledge"]}
    assert knowledge["Эльдар не спросил, как именно погиб Аларик"] == "explicit"
    assert knowledge["Эльдар, возможно, заранее знал об убийстве"] == "inference"
    assert eldar["facts"]["knowledge"][1]["kind_label"] == "Обоснованный вывод"


def test_chronology_flashback_and_estimated_story_order(analyzed):
    _, sid, ks = analyzed
    narrative = ks.chronology(sid)
    flash = next(e for e in narrative if e["narrative_mode"] == "flashback")
    assert narrative.index(flash) == 4 and flash["story_time"] == "десять лет назад"
    story = ks.chronology(sid, order="story_estimate")
    assert story[0]["id"] == flash["id"]                 # флешбэк с известным временем ставится раньше
    assert [e["id"] for e in narrative] != [e["id"] for e in story]
    assert all(e["sources"] and e["participants"] for e in narrative)


def test_who_knows_a_secret(analyzed):
    _, sid, ks = analyzed
    found = ks.character_knowledge(sid, "убийство")
    assert any(r["entity"] == "Эльдар Корн" for r in found)


def test_unverified_status_change_does_not_alter_known_goal(db, book1, model, settings, data_dir):
    deep = copy.deepcopy(sl.DEEP)
    key = (sl.B1, "Глава 7. Ультиматум")
    deep[key]["goals"][0]["quote"] = "Мира решила забыть о мести навсегда"      # цитаты нет в тексте
    llm = ScriptedLLM(deep=deep)
    p = AnalysisPipeline(db, llm, settings, data_dir)
    p.run(p.start(book1.book_id, model, stages=("core", "deep")))
    g = db.one("SELECT * FROM goals WHERE description = ?", (sl.REVENGE,))
    assert g["status"] == "active"
    assert db.scalar("SELECT COUNT(*) FROM goal_history WHERE goal_id = ?", (g["id"],)) == 1
    t = db.one("SELECT issues_json FROM analysis_tasks WHERE stage='deep' AND seq = 6")
    assert any("не принято" in i for i in json.loads(t["issues_json"]))


def test_unverified_new_items_are_stored_but_hidden(db, book1, model, settings, data_dir):
    deep = copy.deepcopy(sl.DEEP)
    deep[(sl.B1, "Глава 4. Тихон")]["goals"][0]["quote"] = "Тихон мечтал стать королём"
    p = AnalysisPipeline(db, ScriptedLLM(deep=deep), settings, data_dir)
    p.run(p.start(book1.book_id, model, stages=("core", "deep")))
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    ks = KnowledgeService(db)
    hidden = [g for g in ks.goals(sid, include_unverified=True) if g["verification"] == "unverified"]
    assert len(hidden) == 1 and hidden[0]["entity_name"] == "Тихон Бреве"
    assert hidden[0]["description"] not in [g["description"] for g in ks.goals(sid)]


def test_deep_stage_is_idempotent_on_rerun(analyzed, pipeline, book1, model, db):
    run_id, _, _ = analyzed
    before = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in ("goals", "goal_history", "relationships", "relationship_history", "open_questions", "motifs", "motif_occurrences", "evidence")}
    pipeline.start(book1.book_id, model, stages=ALL)
    pipeline.run(run_id)
    after = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in before}
    assert before == after


def test_purge_removes_all_derived_knowledge(analyzed, db, book1):
    purge_book_analysis(db, book1.book_id)
    for table in ("entities", "facts", "events", "goals", "goal_history", "relationships", "relationship_history", "open_questions",
                  "plot_threads", "motifs", "evidence", "entity_mentions", "analysis_runs", "analysis_tasks", "report_versions"):
        assert db.scalar(f"SELECT COUNT(*) FROM {table}") == 0, table
    assert db.scalar("SELECT COUNT(*) FROM chunks") == 8                         # сам текст не затронут
    assert db.scalar("SELECT COUNT(*) FROM search_index WHERE kind != 'chunk'") == 0
    assert db.integrity_report()["ok"]


def test_book_report_is_versioned_and_deterministic(analyzed, db, book1):
    from bookmind.services.reports import ReportService
    rs = ReportService(db)
    a = rs.build_book_report(book1.book_id)
    b = rs.build_book_report(book1.book_id)
    assert a == b and "Мира Ветрова" in a and sl.KEY_Q in a and "## Краткое содержание" in a
    assert db.scalar("SELECT MAX(version) FROM report_versions WHERE kind='book_report'") == 2
