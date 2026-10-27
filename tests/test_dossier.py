import json

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.services.dossier import DossierService, estimate_tokens
from tests.fixtures.scripted_llm import ScriptedLLM
from tests.test_qa_export_backup import world  # noqa: F401


def test_markdown_contains_all_sections(world):
    _, _, _, sid, _, _ = world
    svc = DossierService(world[0].db)
    d = svc.build(sid, quotes="limited")
    md = svc.to_markdown(d)
    for section in ("# Досье:", "## Книги", "## Краткое содержание", "### По главам", "## Персонажи", "## Хронология событий",
                    "## Загадки, тайны и пророчества", "## Сюжетные линии", "## Мотивы и символы"):
        assert section in md, section
    assert "Мира Ветрова" in md and "Цитаты: «" in md and "не выдумывай" in md
    assert "Мира забирает ключ" in md or "ключ" in md
    assert estimate_tokens(md) > 500


def test_hidden_unverified_facts_and_default_mode_has_no_quotes(world):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    assert "боевой магией" not in svc.to_markdown(svc.build(sid, quotes="limited"))      # заведомо непроверенный факт не попадает
    nq = svc.to_markdown(svc.build(sid))
    assert "Цитаты:" not in nq and "Мира Ветрова" in nq


def test_spoiler_limit_cuts_later_books(world):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    full = svc.build(sid)
    first = svc.build(sid, up_to_book_order=1)
    assert len(full["books"]) == 2 and len(first["books"]) == 1
    assert len(svc.to_markdown(first)) < len(svc.to_markdown(full)) and "до №1" in svc.to_markdown(first)


def test_completeness_flags_failed_tasks(db, library, book1, model, settings, data_dir):
    llm = ScriptedLLM()
    llm.fail_plan = {n: "не json" for n in (2, 4)}
    settings.parse_retries = 0
    p = AnalysisPipeline(db, llm, settings, data_dir)
    assert p.run(p.start(book1.book_id, model, ("core",))).status == "failed"
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    svc = DossierService(db)
    c = svc.completeness(sid)[0]
    assert c["failed"] == 2 and c["done"] == 6 and c["tasks_total"] == 8
    md = svc.to_markdown(svc.build(sid))
    assert "## Полнота данных" in md and "не выполнено 2" in md          # досье собирается даже при ошибках и честно о них говорит


def test_split_into_parts_keeps_sections_and_numbers(world, tmp_path):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    md = svc.to_markdown(svc.build(sid))
    parts = svc.split_markdown(md, 3000)
    assert len(parts) > 2 and all(len(p) <= 3000 + 300 for p in parts)
    assert parts[0].startswith("_Часть 1 из ") and f"из {len(parts)}" in parts[-1]
    joined = "\n".join(parts)
    for fact_line in [l for l in md.split("\n") if l.startswith("- [")][:20]:
        assert fact_line in joined                                          # ничего не потеряно
    assert svc.split_markdown(md, 0) == [md] and svc.split_markdown(md, 10 ** 9) == [md]


def test_export_files_md_parts_and_json(world, tmp_path):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    one = svc.export(sid, tmp_path / "out" / "d.md")
    assert len(one) == 1 and one[0].read_text(encoding="utf-8").startswith("# Досье")
    many = svc.export(sid, tmp_path / "out" / "d.md", max_chars=4000)
    assert len(many) > 1 and many[0].name.startswith("d_01of") and all(f.exists() for f in many)
    js = svc.export(sid, tmp_path / "out" / "d.json", fmt="json")[0]
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["series"] and data["summaries"]["chapters"] and data["completeness"] and data["characters"] and "preamble" in data


# ---------- авторское право: режимы цитат ----------
def _evidence_quotes(db):
    return [r["quote"] for r in db.query("SELECT quote FROM evidence WHERE verified = 1 AND length(quote) > 20")]


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def test_default_dossier_has_no_quotes_anywhere_but_keeps_locators(world):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    d = svc.build(sid)
    md = svc.to_markdown(d)
    assert "Цитаты:" not in md and "Источник: " in md and "Глава 3. Гильдия" in md          # вместо цитаты — ссылка на место
    assert all("quote" not in src for node in _walk(d) for src in node.get("sources", []))   # и в JSON цитат нет
    assert '"quote"' not in json.dumps(d, ensure_ascii=False, default=str)


def test_limited_mode_cuts_quotes_to_word_limit(world, monkeypatch):
    from bookmind.services import dossier as dz
    qa, _, _, sid, _, _ = world
    monkeypatch.setattr(dz, "MAX_QUOTE_WORDS", 4)
    svc = DossierService(qa.db)
    md = svc.to_markdown(svc.build(sid, quotes="limited"))
    found = [m for m in __import__("re").findall(r"«([^»]+)»", md.split("## Персонажи")[1].split("## Места")[0])]
    quotes = [f for f in found if "…" in f or len(f.split()) <= 4]
    assert quotes and all(len(q.rstrip("…").split()) <= 4 for q in quotes)


def test_limited_mode_total_budget_and_per_chapter_cap(world, monkeypatch):
    from bookmind.services import dossier as dz
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    monkeypatch.setattr(dz, "QUOTE_SHARE", 1.0)
    n_free = svc.to_markdown(svc.build(sid, quotes="limited")).count("Цитаты:")
    monkeypatch.setattr(dz, "QUOTE_SHARE", 0.03)
    tight = svc.to_markdown(svc.build(sid, quotes="limited"))
    assert 0 < tight.count("Цитаты:") < n_free
    assert "Источник: " in tight                                                   # без цитаты остаётся ссылка
    monkeypatch.setattr(dz, "QUOTE_SHARE", 1.0)
    monkeypatch.setattr(dz, "MAX_QUOTES_PER_CHAPTER", 1)
    capped = svc.build(sid, quotes="limited")
    per = {}
    seen = set()
    for node in _walk(capped):
        for src in node.get("sources", []):
            if src.get("quote") and src["id"] not in seen:
                seen.add(src["id"])
                per[src["chapter_id"]] = per.get(src["chapter_id"], 0) + 1
    assert per and max(per.values()) == 1


def test_unknown_quote_mode_is_rejected(world):
    qa, _, _, sid, _, _ = world
    with pytest.raises(ValueError, match="режим"):
        DossierService(qa.db).build(sid, quotes="all")


# ---------- авторское право: пересказ не должен копировать текст ----------
def _copy_from_book(db, words=30):
    text = db.scalar("SELECT text FROM chunks ORDER BY rowid LIMIT 1")
    return " ".join(text.split()[:words])


def test_summary_copied_from_the_book_is_replaced_and_counted(world):
    qa, _, _, sid, _, _ = world
    db = qa.db
    copied = _copy_from_book(db)
    row = db.one("SELECT id FROM report_versions WHERE kind = 'chapter_summary' ORDER BY rowid LIMIT 1")
    db.execute("UPDATE report_versions SET content = ? WHERE id = ?", ("Сводка главы: " + copied, row["id"]))
    svc = DossierService(db)
    d = svc.build(sid)
    md = svc.to_markdown(d)
    assert copied not in md and copied[:60] not in md
    assert "опущено: слишком близко к тексту книги" in md and d["filtered"] >= 1
    assert "Исключено фрагментов" in md                                       # пользователь видит, что фильтр сработал
    assert md.count("Сводка главы:") == 0
    assert "Мира сообщает больной сестре Лине" in md


def test_short_statements_and_normal_summaries_are_kept(world):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    d = svc.build(sid)
    md = svc.to_markdown(d)
    assert d["filtered"] == 0 and "Исключено" not in md
    assert "Мира спрятала ключ под половицей" in md                           # короткий факт, совпадающий с фразой книги, допустим
