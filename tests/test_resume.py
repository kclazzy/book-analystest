import threading

import pytest

from bookmind.analysis.persister import CorePersister
from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.domain.exceptions import BookMindError
from tests.fixtures.scripted_llm import ScriptedLLM, unavailable


def _counts(db):
    return {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in
            ("entities", "facts", "events", "evidence", "entity_mentions", "event_participants")}


def test_cancel_then_resume_processes_only_remaining(pipeline, book1, model, db, llm, settings, data_dir):
    cancel = threading.Event()
    seen = []

    def on_progress(p):
        seen.append((p.done, p.chapter))
        if p.done == 3 and p.message == "готово":
            cancel.set()

    run_id = pipeline.start(book1.book_id, model)
    s1 = pipeline.run(run_id, on_progress, cancel)
    assert s1.status == "paused" and s1.counts["core"]["done"] == 3 and len(llm.calls) == 3
    assert db.scalar("SELECT status FROM analysis_runs WHERE id = ?", (run_id,)) == "paused"

    p2 = AnalysisPipeline(db, llm, settings, data_dir)  # «перезапуск программы»
    assert p2.start(book1.book_id, model) == run_id
    s2 = p2.run(run_id)
    assert s2.status == "completed" and s2.counts["core"]["done"] == 8
    assert len(llm.calls) == 8  # ни один завершённый фрагмент не обрабатывался повторно
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_resumed_result_equals_uninterrupted_result(book1, model, db, settings, data_dir, tmp_path):
    from bookmind.services.library import LibraryService
    from bookmind.storage.database import Database
    from tests.fixtures.builders import write_txt
    from tests.fixtures.novel import BOOK1, BOOK1_TITLE

    # эталон: один непрерывный прогон в отдельной базе
    d2 = tmp_path / "ref"
    d2.mkdir()
    ref_db = Database(d2 / "x.sqlite")
    ref_lib = LibraryService(ref_db, settings, d2)
    ref_book = ref_lib.import_book(write_txt(tmp_path / "ref.txt", BOOK1), title=BOOK1_TITLE)
    ref_pipe = AnalysisPipeline(ref_db, ScriptedLLM(), settings, d2)
    ref_pipe.run(ref_pipe.start(ref_book.book_id, model))

    llm = ScriptedLLM()
    llm.fail_plan = {3: unavailable(), 6: unavailable()}
    p = AnalysisPipeline(db, llm, settings, data_dir)
    run_id = p.start(book1.book_id, model)
    for _ in range(3):
        p.run(run_id)
        p = AnalysisPipeline(db, llm, settings, data_dir)
        p.start(book1.book_id, model)
    assert db.scalar("SELECT status FROM analysis_runs WHERE id = ?", (run_id,)) == "completed"
    assert _counts(db) == _counts(ref_db)
    ref_db.close()


def test_ollama_unavailable_pauses_without_burning_attempts(pipeline, book1, model, db, llm):
    llm.fail_plan = {4: unavailable("connection refused")}
    run_id = pipeline.start(book1.book_id, model)
    s = pipeline.run(run_id)
    assert s.status == "paused" and "недоступен" in s.message
    assert s.counts["core"]["done"] == 3 and s.counts["core"].get("failed", 0) == 0
    pending = db.one("SELECT * FROM analysis_tasks WHERE status='pending' ORDER BY seq LIMIT 1")
    assert pending["attempts"] == 0
    pipeline.start(book1.book_id, model)
    assert pipeline.run(run_id).status == "completed"


def test_hard_crash_leaves_no_partial_data_and_resumes(book1, model, db, settings, data_dir, monkeypatch):
    class Crash(BaseException):
        pass

    llm = ScriptedLLM()
    p = AnalysisPipeline(db, llm, settings, data_dir)
    run_id = p.start(book1.book_id, model)
    real = CorePersister._events
    calls = {"n": 0}

    def crash_on_third(self, ctx, core):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Crash()
        return real(self, ctx, core)

    monkeypatch.setattr(CorePersister, "_events", crash_on_third)
    with pytest.raises(Crash):
        p.run(run_id)
    third = db.one("SELECT * FROM analysis_tasks WHERE seq = 2")
    assert third["status"] == "running"                       # процесс «умер» посреди задачи
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE chunk_id = ?", (third["scope_id"],)) == 0  # частичных данных нет
    assert db.scalar("SELECT COUNT(*) FROM analysis_tasks WHERE status='done'") == 2

    monkeypatch.setattr(CorePersister, "_events", real)
    p2 = AnalysisPipeline(db, llm, settings, data_dir)
    assert p2.start(book1.book_id, model) == run_id
    assert p2.run(run_id).status == "completed"
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE chunk_id = ?", (third["scope_id"],)) == 5  # ровно столько фактов в эталонном ответе главы 3
    assert db.scalar("SELECT COUNT(*) FROM report_versions WHERE kind='chunk_summary'") == 8


def test_internal_error_in_one_task_rolls_back_that_task_only(pipeline, book1, model, db, monkeypatch):
    real = CorePersister._events
    n = {"i": 0}

    def boom(self, ctx, core):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("сбой записи")
        return real(self, ctx, core)

    monkeypatch.setattr(CorePersister, "_events", boom)
    run_id = pipeline.start(book1.book_id, model)
    s = pipeline.run(run_id)
    assert s.status == "failed" and s.counts["core"]["failed"] == 1 and s.counts["core"]["done"] == 7
    failed = db.one("SELECT * FROM analysis_tasks WHERE status='failed'")
    assert "Внутренняя ошибка" in failed["error"]
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE chunk_id = ?", (failed["scope_id"],)) == 0
    assert db.integrity_report()["ok"]


def test_attempts_are_limited(pipeline, book1, model, db, llm, settings):
    settings.parse_retries = 0
    settings.task_max_attempts = 2
    llm.fail_plan = {n: "не json" for n in range(1, 40)}
    run_id = pipeline.start(book1.book_id, model)
    pipeline.run(run_id)
    n_calls = len(llm.calls)
    pipeline.start(book1.book_id, model)
    pipeline.run(run_id)
    assert len(llm.calls) == n_calls * 2 and n_calls == 8
    pipeline.start(book1.book_id, model)
    s = pipeline.run(run_id)
    assert len(llm.calls) == n_calls * 2       # попытки исчерпаны: новых запросов нет
    assert s.status == "failed" and s.counts["core"]["failed"] == 8


def test_prompt_change_requires_explicit_replace(pipeline, book1, model, db, llm, settings, data_dir):
    pipeline.run(pipeline.start(book1.book_id, model))
    before = _counts(db)
    (data_dir / "prompts").mkdir()
    base = pipeline.prompts.template("core_analysis")
    (data_dir / "prompts" / "core_analysis.md").write_text(base + "\nДополнительное правило.\n", encoding="utf-8")
    p2 = AnalysisPipeline(db, llm, settings, data_dir)
    with pytest.raises(BookMindError):
        p2.start(book1.book_id, model)
    assert _counts(db) == before                                   # отказ ничего не испортил
    run2 = p2.start(book1.book_id, model, replace=True)
    assert db.scalar("SELECT COUNT(*) FROM analysis_runs") == 1
    assert db.scalar("SELECT COUNT(*) FROM facts") == 0            # старые результаты заменены
    assert p2.run(run2).status == "completed"
    assert _counts(db) == before


def test_editing_chapters_after_analysis_requires_purge(pipeline, library, book1, model, db):
    pipeline.run(pipeline.start(book1.book_id, model))
    with pytest.raises(BookMindError):
        library.rename_chapter(book1.book_id, 0, "Новое имя")
    library.rename_chapter(book1.book_id, 0, "Новое имя", purge_analysis=True)
    assert db.scalar("SELECT COUNT(*) FROM analysis_runs") == 0 and db.scalar("SELECT COUNT(*) FROM facts") == 0
    assert db.scalar("SELECT COUNT(*) FROM entities") == 0
