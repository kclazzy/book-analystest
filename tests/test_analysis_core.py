import json

from bookmind.ingestion.text_utils import norm_key


def _run(pipeline, book, model):
    run_id = pipeline.start(book.book_id, model)
    return run_id, pipeline.run(run_id)


def test_book_analysis_creates_verifiable_records(pipeline, book1, model, db, library, llm):
    run_id, summary = _run(pipeline, book1, model)
    assert summary.status == "completed", summary
    assert summary.counts["core"]["done"] == 8 and llm.count("core") == 8

    names = {r["name"] for r in db.query("SELECT name FROM entities WHERE type='character'")}
    assert names == {"Мира Ветрова", "Аларик Вейн", "Лина", "Эльдар Корн", "Тихон Бреве"}
    assert {r["name"] for r in db.query("SELECT name FROM entities WHERE type='place'")} == {"Лаборатория", "Западная башня"}
    assert db.scalar("SELECT COUNT(*) FROM entities WHERE type='organization'") == 1
    assert db.scalar("SELECT COUNT(*) FROM entities WHERE type='item'") == 2  # ключ и Огненный реестр

    # каждый подтверждённый факт опирается на цитату, найденную в оригинале по указанным позициям
    facts = db.query("SELECT * FROM facts WHERE verification='verified'")
    assert len(facts) >= 20
    for f in facts:
        ev = db.one("SELECT e.*, c.text FROM evidence e JOIN chunks c ON c.id = e.chunk_id "
                    "WHERE e.owner_type='fact' AND e.owner_id = ?", (f["id"],))
        assert ev is not None and ev["verified"] == 1 and ev["chunk_id"] == f["chunk_id"]
        assert norm_key(ev["quote"]) in norm_key(ev["text"][ev["start_char"]:ev["end_char"]])


def test_unverified_claim_is_kept_but_not_treated_as_fact(pipeline, book1, model, db):
    _run(pipeline, book1, model)
    bad = db.one("SELECT * FROM facts WHERE statement LIKE '%боевой магией%'")
    assert bad is not None and bad["verification"] == "unverified"
    ev = db.one("SELECT * FROM evidence WHERE owner_id = ?", (bad["id"],))
    assert ev["verified"] == 0 and ev["start_char"] is None
    # в поиске только подтверждённые факты
    assert not db.query("SELECT 1 FROM search_index WHERE kind='fact' AND ref_id = ?", (bad["id"],))
    task = db.one("SELECT issues_json FROM analysis_tasks WHERE scope_id = ?", (bad["chunk_id"],))
    assert any("не найдена" in i for i in json.loads(task["issues_json"]))


def test_events_have_participants_order_flashback_and_sources(pipeline, book1, model, db):
    _run(pipeline, book1, model)
    events = db.query("SELECT * FROM events ORDER BY seq")
    assert len(events) == 9 and [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    flash = [e for e in events if e["narrative_mode"] == "flashback"]
    assert len(flash) == 1 and flash[0]["story_time"] == "десять лет назад"
    betrayal = db.one("SELECT * FROM events WHERE description LIKE '%тайно выдаёт%'")
    roles = {r["role"] for r in db.query("SELECT role FROM event_participants WHERE event_id = ?", (betrayal["id"],))}
    assert {"предатель", "заказчик", "свидетельница"} <= roles
    assert db.scalar("SELECT COUNT(*) FROM evidence WHERE owner_type='event' AND verified=1") == 9
    place = db.one("SELECT e.name FROM events v JOIN entities e ON e.id = v.place_entity_id WHERE v.id = ?",
                   (db.one("SELECT id FROM events WHERE description LIKE '%Западной башне%'")["id"],))
    assert place["name"] == "Западная башня"


def test_chunk_summaries_and_search_index(pipeline, book1, model, db):
    _run(pipeline, book1, model)
    assert db.scalar("SELECT COUNT(*) FROM report_versions WHERE kind='chunk_summary'") == 8
    from bookmind.services.search import SearchIndex
    s = SearchIndex(db)
    assert s.count("entity") >= 10 and s.count("event") == 9
    hits = s.search("предательство Тихона отец тюрьма", kinds=["fact", "event"])
    assert hits and hits[0].kind in ("fact", "event")


def test_second_run_is_noop_for_completed_run(pipeline, book1, model, db, llm):
    run_id, _ = _run(pipeline, book1, model)
    calls = len(llm.calls)
    again = pipeline.start(book1.book_id, model)
    assert again == run_id
    pipeline.run(again)
    assert len(llm.calls) == calls
    assert db.scalar("SELECT COUNT(*) FROM analysis_runs") == 1


def test_invalid_model_answer_marks_task_failed_and_retry_succeeds(pipeline, book1, model, db, llm, settings):
    settings.parse_retries = 0
    llm.fail_plan = {2: "это не JSON"}
    run_id = pipeline.start(book1.book_id, model)
    s = pipeline.run(run_id)
    assert s.status == "failed" and s.counts["core"]["failed"] == 1 and s.counts["core"]["done"] == 7
    failed = db.one("SELECT * FROM analysis_tasks WHERE status='failed'")
    assert failed["raw_response"] == "это не JSON" and "JSON" in failed["error"] and failed["attempts"] == 1
    # повторный запуск обрабатывает только упавшую задачу
    llm.fail_plan = {}
    before = len(llm.calls)
    pipeline.start(book1.book_id, model)
    s2 = pipeline.run(run_id)
    assert s2.status == "completed" and len(llm.calls) - before == 1


def test_json_repair_retry_in_same_task(pipeline, book1, model, db, llm, settings):
    settings.parse_retries = 1
    llm.fail_plan = {1: "```json\n{\"summary\": \"x\", \"facts\": [{\"entity\": \"A\"}]}\n```"}  # нарушает схему
    run_id = pipeline.start(book1.book_id, model)
    s = pipeline.run(run_id)
    assert s.status == "completed" and len(llm.calls) == 9  # 1 повторный запрос внутри первой задачи
    first = db.one("SELECT issues_json FROM analysis_tasks ORDER BY seq LIMIT 1")
    assert any("попытки 2" in i for i in json.loads(first["issues_json"]))


def test_no_duplicate_entities_or_facts_on_repeated_names(pipeline, book1, model, db):
    _run(pipeline, book1, model)
    assert db.scalar("SELECT COUNT(*) FROM entities WHERE name = 'Мира Ветрова'") == 1
    assert db.scalar("SELECT COUNT(*) FROM entities WHERE status = 'needs_review'") == 0
    assert db.scalar("SELECT COUNT(*) FROM merge_candidates") == 0
    mira = db.one("SELECT id FROM entities WHERE name = 'Мира Ветрова'")
    assert db.scalar("SELECT COUNT(*) FROM entity_mentions WHERE entity_id = ?", (mira["id"],)) >= 7
    aliases = {r["alias"] for r in db.query("SELECT alias FROM entity_aliases WHERE entity_id = ?", (mira["id"],))}
    assert {"Мира Ветрова", "Мира"} <= aliases


def test_prompt_contains_known_entities_not_whole_book(pipeline, book1, model, llm):
    _run(pipeline, book1, model)
    p = llm.prompts[3]  # глава 4: известные сущности из глав 1-3
    assert "Мира Ветрова [character]" in p and "Эльдар Корн" in p
    assert "Глава 1. Мёртвая лаборатория" not in p.split("ТЕКСТ ДЛЯ АНАЛИЗА:")[1]
    assert len(p) < 6000
