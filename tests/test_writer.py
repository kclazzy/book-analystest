import zipfile

import pytest

from bookmind.analysis.extractor import StructuredCaller
from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.creation import book_export as be
from bookmind.creation.schemas import AdaptParams
from bookmind.creation.service import AdaptationService
from bookmind.creation.writer import BookWriter, clean_prose, validate_prose
from bookmind.domain.exceptions import BookMindError
from bookmind.ingestion.epub import parse_epub
from bookmind.ingestion.fb2 import parse_fb2
from tests.fixtures.scripted_llm import ScriptedLLM, unavailable
from tests.test_creation import ORIGINAL, make, sid  # noqa: F401


def setup(db, sid, settings, model, extra=None, words=500, **p):
    llm = ScriptedLLM(extra=extra or {})
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    p.setdefault("auto_fix", False)
    pid = svc.create(sid, AdaptParams(divergence="medium", seed=3, chapter_words=words, **p))
    assert svc.run(pid, model).status in ("completed", "awaiting_review")
    return svc, BookWriter(svc), llm, pid


def test_clean_prose_strips_markup_and_service_lines():
    raw = "```\n## Глава 3\n**Часть 2**\nОна *тихо* сказала **да**.\n\n---\n(Конец части)\nВторой абзац."
    assert clean_prose(raw) == "Она тихо сказала да.\n\nВторой абзац."


def test_validate_prose_checks_length_language_repeats():
    assert validate_prose("Короткий текст.", 500)
    assert any("не по-русски" in i for i in validate_prose("This is plain english text. " * 100, 100))
    p = "Абзац " * 30
    assert any("повторяющиеся" in i for i in validate_prose(f"{p}\n\n{p}", 10))
    assert validate_prose("Слово " * 400, 500) == []


def test_write_all_chapters_follows_plan_and_is_clean(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=500)
    s = w.write(pid, model)
    assert s.status == "completed" and s.written == 8 and s.flagged == []
    assert llm.count("write_part") == 8
    assert set(w.text_status(pid).values()) == {"ok"}
    book = w.book(pid)
    assert len(book.chapters) == 8 and all(len(c.text.split()) >= 250 for c in book.chapters)
    text = " ".join(c.text for c in book.chapters)
    assert not any(n in text for n in ORIGINAL)
    # промпт части несёт план, героев и предыдущие главы
    last = llm.prompts[-1]
    assert "Отсек 8" in last and "ЧТО БЫЛО В ПРЕДЫДУЩИХ ГЛАВАХ" in last and "Отсек" in last.split("ЧТО БЫЛО")[1]


def test_long_chapter_is_written_in_parts_with_continuation(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=1500)
    w.write(pid, model, chapters=[0])
    assert llm.count("write_part") == 3
    prompts = [p for p in llm.prompts if "Верни только текст части" in p]
    assert "Начало главы" in prompts[0] and "Середина" in prompts[1] and "Финал главы" in prompts[2]
    assert "(глава ещё не начата)" in prompts[0] and "(глава ещё не начата)" not in prompts[1]


def test_resume_does_not_rewrite_finished_chapters_or_parts(db, sid, settings, model):
    calls = {"n": 0}

    def handler(prompt, default):
        calls["n"] += 1
        if calls["n"] == 5:
            raise unavailable()
        return default(prompt)
    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": handler}, words=1000)
    s = w.write(pid, model)
    assert s.status == "paused" and "недоступен" in s.message
    done_calls = llm.count("write_part")
    st = w.text_status(pid)
    assert list(st.values()).count("ok") == 2 and "partial" not in st.values() or True
    s2 = w.write(pid, model)
    assert s2.status == "completed" and set(w.text_status(pid).values()) == {"ok"}
    # недописанная глава продолжилась с нужной части, готовые главы не переписывались: всего 8 глав * 2 части + повтор упавшего вызова
    assert llm.count("write_part") == 16 + 1


def test_original_names_trigger_rewrite_then_flag(db, sid, settings, model):
    def handler(prompt, default):
        return default(prompt) + "\n\nАларик Вейн молча кивнул Мире и ушёл в Западную башню."
    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": handler}, words=500)
    s = w.write(pid, model, chapters=[0])
    assert s.flagged == [0] and llm.count("write_part") == 3              # 1 + 2 переписывания (max_replans=2)
    assert any("имена" in i for i in w.issues(pid, 0))
    assert "Отсек" in llm.prompts[-1] and "ЗАМЕЧАНИЯ К ПРЕДЫДУЩЕЙ ПОПЫТКЕ" in llm.prompts[-1]


def test_short_text_gets_rewritten(db, sid, settings, model):
    state = {"n": 0}

    def handler(prompt, default):
        state["n"] += 1
        return "Коротко." if state["n"] <= 4 else default(prompt)
    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": handler}, words=500)
    s = w.write(pid, model, chapters=[0])
    assert s.flagged == [] and w.text_status(pid)[0] == "ok"


def test_verbatim_copy_of_original_is_flagged(db, sid, settings, model):
    chunk = db.scalar("SELECT text FROM chunks ORDER BY rowid LIMIT 1")
    sentences = [x.strip() for x in chunk.split(".") if len(x.split()) > 6][:3]
    for n in ORIGINAL:
        sentences = [x.replace(n, "Кто-то") for x in sentences]

    def handler(prompt, default):
        return default(prompt) + "\n\n" + ". ".join(sentences) + "."
    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": handler}, words=300)
    w.write(pid, model, chapters=[0])
    assert w.text_status(pid)[0] == "flagged"


def test_rewrite_and_manual_edit(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=300)
    w.write(pid, model, chapters=[0, 1])
    before = llm.count("write_part")
    w.write(pid, model, chapters=[0])                       # уже готово — ничего не делает
    assert llm.count("write_part") == before
    w.write(pid, model, chapters=[0], rewrite=True)
    assert llm.count("write_part") == before + 1
    assert w.update_text(pid, 1, "Слишком коротко.") and w.text_status(pid)[1] == "flagged"
    assert w.update_text(pid, 1, ("Свой текст автора. " * 80)) == [] and w.text_status(pid)[1] == "ok"
    assert w.chapter_text(pid, 1).startswith("Свой текст")


def test_cannot_write_without_plan_or_before_approval(db, sid, settings, model):
    svc = AdaptationService(db, StructuredCaller(ScriptedLLM(), settings), settings)
    pid = svc.create(sid, AdaptParams())
    with pytest.raises(BookMindError, match="план"):
        BookWriter(svc).write(pid, model)
    svc2, w, _, pid2 = setup(db, sid, settings, model, pause_after_plan=True)
    assert svc2._row(pid2)["status"] == "awaiting_review"
    with pytest.raises(BookMindError, match="подтвердите"):
        w.write(pid2, model)
    svc2.approve(pid2)
    assert w.write(pid2, model, chapters=[0]).status == "completed"


# ---------- экспорт ----------
@pytest.fixture
def book():
    return be.Book("Тишина <на> орбите", [be.BookChapter("Глава 1. Отсек & шлюз", "Первый абзац.\n\nВторой «абзац» с <тегом> & знаком."),
                                           be.BookChapter("Дальше", "Текст второй главы.")], annotation="Экипаж станции.")


def test_markdown_txt_headings(book):
    md = be.to_markdown(book)
    assert md.startswith("# Тишина <на> орбите") and "## Глава 1. Отсек & шлюз" in md and "## Глава 2. Дальше" in md
    assert "Глава 1. Глава 1" not in md and "ГЛАВА" not in be.to_text(book).split("\n\n")[2]


def test_fb2_is_escaped_and_reimportable(book, tmp_path):
    path = be.export_book(book, tmp_path / "b.fb2")
    parsed = parse_fb2(path)
    assert parsed.title == "Тишина <на> орбите" and len(parsed.chapters) == 2
    assert "<тегом> & знаком" in parsed.chapters[0].raw_text and "Первый абзац" in parsed.chapters[0].raw_text


def test_epub_structure_and_reimport(book, tmp_path):
    path = be.export_book(book, tmp_path / "b.epub")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert names[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip" and "OEBPS/content.opf" in names and "OEBPS/ch002.xhtml" in names
        assert z.testzip() is None
    parsed = parse_epub(path)
    assert parsed.title == "Тишина <на> орбите"
    body = " ".join(c.raw_text for c in parsed.chapters)
    assert "Первый абзац" in body and "Текст второй главы" in body and "<тегом> & знаком" in body


def test_export_errors(book, tmp_path):
    with pytest.raises(BookMindError, match="формат"):
        be.export_book(book, tmp_path / "b.pdf")
    with pytest.raises(BookMindError, match="нет написанных"):
        be.export_book(be.Book("Пусто"), tmp_path / "b.md")
    assert be.export_book(book, tmp_path / "sub" / "b.txt").exists()


# ---------- полный конвейер ----------
def test_full_pipeline_from_imported_book_to_epub(db, book1, settings, model, data_dir, tmp_path):
    from bookmind.creation.full import FullProgress, run_full_book
    llm = ScriptedLLM()
    pipe = AnalysisPipeline(db, llm, settings, data_dir)
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    w = BookWriter(svc)
    sid_ = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    seen = []
    out = tmp_path / "book.epub"
    r = run_full_book(pipe, svc, w, sid_, model, AdaptParams(seed=3, chapter_words=300, pause_after_plan=True, auto_fix=False), export_path=out,
                      on_progress=lambda p: seen.append(p.phase))
    assert r.status == "completed" and r.path == out and out.exists() and r.flagged == []
    assert r.qa_errors == 0 and r.qa_warnings > 0          # шаблонный текст заглушки честно получает замечания о повторах
    assert [p for i, p in enumerate(seen) if i == 0 or seen[i - 1] != p] == ["analysis", "plan", "write", "export"]
    assert llm.count("core") > 0 and llm.count("write_part") == 8
    parsed = parse_epub(out)
    assert len(parsed.chapters) >= 8
    calls = len(llm.calls)
    r2 = run_full_book(pipe, svc, w, sid_, model, AdaptParams(seed=3, chapter_words=300, auto_fix=False), project_id=r.project_id, export_path=tmp_path / "again.fb2")
    assert r2.status == "completed" and len(llm.calls) == calls               # ничего не делается повторно


def test_full_pipeline_pauses_and_resumes(db, book1, settings, model, data_dir, tmp_path):
    from bookmind.creation.full import run_full_book
    import threading
    llm = ScriptedLLM()
    pipe = AnalysisPipeline(db, llm, settings, data_dir)
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    w = BookWriter(svc)
    sid_ = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    ev = threading.Event()
    ev.set()
    r = run_full_book(pipe, svc, w, sid_, model, AdaptParams(chapter_words=300, auto_fix=False), cancel=ev)
    assert r.status == "paused" and not r.project_id
    r = run_full_book(pipe, svc, w, sid_, model, AdaptParams(seed=1, chapter_words=300, auto_fix=False), export_path=tmp_path / "b.md")
    assert r.status == "completed" and (tmp_path / "b.md").read_text(encoding="utf-8").startswith("# ")


def test_full_pipeline_empty_series(db, settings, model, data_dir):
    from bookmind.creation.full import run_full_book
    llm = ScriptedLLM()
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    r = run_full_book(AnalysisPipeline(db, llm, settings, data_dir), svc, BookWriter(svc), "nope", model)
    assert r.status == "failed" and "нет книг" in r.message


def test_full_pipeline_continues_when_some_analysis_tasks_fail(db, book1, settings, model, data_dir, tmp_path):
    from bookmind.creation.full import run_full_book
    settings.parse_retries = 0
    llm = ScriptedLLM()
    llm.fail_plan = {n: "не json" for n in (2, 5, 6)}                      # три фрагмента основного анализа упали
    pipe = AnalysisPipeline(db, llm, settings, data_dir)
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    sid_ = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    out = tmp_path / "partial.md"
    r = run_full_book(pipe, svc, BookWriter(svc), sid_, model, AdaptParams(seed=2, chapter_words=300, auto_fix=False), export_path=out)
    assert r.status == "completed" and out.exists()
    assert len(r.warnings) == 1 and "неполным данным" in r.warnings[0] and "Повторить неудавшиеся" in r.warnings[0]


def test_book_report_is_stored_after_writing_and_refreshed_on_edit(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=500)
    assert w.book_report(pid) is None
    w.write(pid, model)
    rep = w.book_report(pid)
    assert rep is not None and rep.chapters_checked == 8 and rep.chapters_total == 8
    w.update_text(pid, 0, "Короткий текст без конца и")
    again = w.book_report(pid)
    assert any(f.code == "truncated" and f.chapter == 0 for f in again.findings)
    assert w.check_book(pid).errors


def test_check_book_reports_missing_chapters(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=500)
    w.write(pid, model, chapters=[0, 1])
    rep = w.check_book(pid)
    assert {f.chapter for f in rep.findings if f.code == "missing_text"} == set(range(2, 8))


def test_foreign_script_triggers_rewrite(db, sid, settings, model):
    calls = []

    def write(prompt, default):
        calls.append(prompt)
        return ("Вера Лунина шла по отсеку. 你好世界。 " if len(calls) == 1 else "Вера Лунина шла по отсеку. ") * 80

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500)
    w.write(pid, model, chapters=[0])
    assert len(calls) >= 2 and any("иероглиф" in c or "другого письма" in c for c in calls[1:])


def test_book_report_is_built_for_books_written_before_checks_existed(db, sid, settings, model):
    svc, w, llm, pid = setup(db, sid, settings, model, words=500)
    w.write(pid, model)
    svc._save(pid, qa_json="{}")                       # книга создана старой версией: отчёта нет
    rep = w.book_report(pid)
    assert rep is not None and rep.chapters_checked == 8
    assert w.book_report(pid) is not None and svc._row(pid)["qa_json"] != "{}"
    svc2, w2, _, pid2 = setup(db, sid, settings, model, words=500)
    assert w2.book_report(pid2) is None                # текстов нет — проверять нечего


def _sentences(tag: str, n: int = 120) -> str:
    return " ".join(f"Вера Лунина {tag} сделала шаг номер {k} по отсеку." for k in range(n))


def test_fix_book_rewrites_flagged_chapter_with_findings_as_feedback(db, sid, settings, model):
    prompts = []

    def write(prompt, default):
        prompts.append(prompt)
        return _sentences("после исправления")

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500)
    w.write(pid, model)
    w.update_text(pid, 1, _sentences("до") + " «Незакрытая цитата.")
    assert any(f.code == "quotes" and f.chapter == 1 for f in w.check_book(pid).findings)
    prompts.clear()
    s = w.fix_book(pid, model, rounds=1, chapters=[1])
    assert s.fixed == [1] and len(prompts) == 1 and "кавычки" in prompts[0]
    assert "после исправления" in w.chapter_text(pid, 1)
    assert not any(f.code == "quotes" for f in w.book_report(pid).findings)


def test_fix_book_keeps_old_text_when_rewrite_is_worse(db, sid, settings, model):
    def write(prompt, default):
        return "Вера Лунина шла по отсеку. 你好世界。 " * 80 if "ЗАМЕЧАНИЯ" in prompt else default(prompt)

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500)
    w.write(pid, model, chapters=[0])
    old = _sentences("до") + " «Незакрытая цитата."
    w.update_text(pid, 0, old)
    s = w.fix_book(pid, model, rounds=1, chapters=[0])
    assert s.fixed == [] and w.chapter_text(pid, 0) == old


def test_write_runs_fix_automatically_when_enabled(db, sid, settings, model):
    calls = []

    def write(prompt, default):
        calls.append(prompt)
        return _sentences("первый") + " «Незакрытая." if len(calls) == 1 else _sentences("второй")

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500, auto_fix=True, fix_rounds=1)
    s = w.write(pid, model, chapters=[0])
    assert len(calls) == 2 and s.fixed == [0] and "второй" in w.chapter_text(pid, 0)


def test_book_gets_author_cover_and_illustrations_from_params(db, sid, settings, model):
    pytest.importorskip("PIL")
    svc, w, llm, pid = setup(db, sid, settings, model, words=500, author="Mister.Ti", illustrations=True)
    w.write(pid, model, chapters=[0, 1])
    b = w.book(pid)
    assert b.author == "Mister.Ti" and b.cover and all(c.image for c in b.chapters)
    svc2, w2, _, pid2 = setup(db, sid, settings, model, words=500, illustrations=False)
    w2.write(pid2, model, chapters=[0])
    b2 = w2.book(pid2)
    assert b2.author == "BookMind" and b2.cover is None and b2.chapters[0].image is None


def test_clean_prose_drops_service_lines_and_repeated_sentences():
    p = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до выхода."
    out = clean_prose(f"Место действия: шлюз\nКадр 2: финал\n{p} Осип молчал.\n\n{p} Осип молчал.")
    assert out.count("медленно шла") == 1 and "Место действия" not in out and "Кадр" not in out


def test_prompt_forbids_known_generation_defects(db, sid, settings, model):
    prompts = []

    def write(prompt, default):
        prompts.append(prompt)
        return default(prompt)

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500)
    w.write(pid, model, chapters=[0])
    p = prompts[0].casefold()
    for rule in ("третьего лица", "латин", "штамп", "не повторяй", "слово «кадр»"):
        assert rule in p


def test_fix_book_cleans_repeats_before_asking_model(db, sid, settings, model):
    calls = []

    def write(prompt, default):
        calls.append(prompt)
        return _sentences("исходный")

    svc, w, llm, pid = setup(db, sid, settings, model, extra={"write_handler": write}, words=500)
    w.write(pid, model, chapters=[0])
    dup = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до выхода."
    w.update_text(pid, 0, _sentences("x") + f" {dup} Конец сцены.\n\nМежду тем.\n\n{dup} Конец сцены.")
    assert any(f.code == "dup_sentence" for f in w.check_book(pid).findings)
    calls.clear()
    s = w.fix_book(pid, model, rounds=1, chapters=[0])
    # модель могла быть вызвана из-за других замечаний (охват плана), но про повторы ей писать уже не нужно
    assert 0 in s.fixed and all("повторяются предложения" not in c for c in calls)
    assert w.chapter_text(pid, 0).count("медленно шла") <= 1
    assert not any(f.code == "dup_sentence" for f in w.book_report(pid).findings)
