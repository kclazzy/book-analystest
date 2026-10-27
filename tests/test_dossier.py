import json

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.services.dossier import DossierService, estimate_tokens
from tests.fixtures.scripted_llm import ScriptedLLM
from tests.test_qa_export_backup import world  # noqa: F401


def test_markdown_contains_all_sections_with_quotes(world):
    _, _, _, sid, _, _ = world
    svc = DossierService(world[0].db)
    d = svc.build(sid)
    md = svc.to_markdown(d)
    for section in ("# Досье:", "## Книги", "## Краткое содержание", "### По главам", "## Персонажи", "## Хронология событий",
                    "## Загадки, тайны и пророчества", "## Сюжетные линии", "## Мотивы и символы"):
        assert section in md, section
    assert "Мира Ветрова" in md and "Цитаты: «" in md and "не выдумывай" in md
    assert "Мира забирает ключ" in md or "ключ" in md
    assert estimate_tokens(md) > 500


def test_hidden_unverified_facts_and_no_quotes_mode(world):
    qa, _, _, sid, _, _ = world
    svc = DossierService(qa.db)
    assert "боевой магией" not in svc.to_markdown(svc.build(sid))        # заведомо непроверенный факт не попадает
    nq = svc.to_markdown(svc.build(sid, with_quotes=False))
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
