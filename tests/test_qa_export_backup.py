import csv
import json
import sqlite3
import zipfile

import pytest

from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.analysis.extractor import StructuredCaller
from bookmind.domain.exceptions import BookMindError
from bookmind.llm.prompts import PromptStore
from bookmind.services.backup import BackupError, BackupService
from bookmind.services.exports import ExportService, _csv_safe
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.qa import INSUFFICIENT_TEXT, QAService
from bookmind.storage.database import Database
from tests.fixtures.builders import write_txt
from tests.fixtures.novel import BOOK2, BOOK2_TITLE
from tests.fixtures.scripted_llm import ScriptedLLM

ALL = AnalysisPipeline.STAGES_ORDER


def _handlers():
    def contradiction(prompt):
        if "серые" in prompt and "карие" in prompt:
            return {"relation": "contradiction", "explanation": "Цвет глаз назван по-разному."}
        return {"relation": "unrelated", "explanation": ""}
    return contradiction


@pytest.fixture
def world(db, library, settings, data_dir, tmp_path, book1, model):
    answers = {"handler": None}
    llm = ScriptedLLM(extra={"contradiction": {"handler": _handlers()}, "answer": answers})
    p = AnalysisPipeline(db, llm, settings, data_dir)
    assert p.run(p.start(book1.book_id, model, stages=ALL)).status == "completed"
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    b2 = library.import_book(write_txt(tmp_path / "b2.txt", BOOK2), title=BOOK2_TITLE, series_id=sid)
    assert p.run(p.start(b2.book_id, model, stages=ALL)).status == "completed"
    qa = QAService(db, StructuredCaller(llm, settings), PromptStore(data_dir))
    return qa, llm, answers, sid, book1, b2


# ---------- вопросы и ответы ----------
def test_no_materials_means_no_llm_call(world, model):
    qa, llm, _, sid, _, _ = world
    a = qa.ask("Квантовая хромодинамика и пицца?", sid, model)
    assert a.insufficient and a.answer == INSUFFICIENT_TEXT and not a.llm_called and llm.count("answer") == 0


def test_answer_with_valid_citations_carries_sources(world, model):
    qa, llm, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "У Эльдара бледные серые глаза [F1].", "sources": ["F1"], "insufficient": False}
    a = qa.ask("Какие глаза у Эльдара?", sid, model)
    assert not a.insufficient and a.llm_called and [c.label for c in a.citations] == ["F1"]
    c = a.citations[0]
    assert c.kind in ("fact", "entity") and c.book_title
    assert "[F1]" in a.answer
    prompt = llm.prompts[-1]
    assert "Какие глаза у Эльдара?" in prompt and "[F1]" in prompt and "Цитата:" in prompt


def test_answer_without_citations_is_rejected(world, model):
    qa, _, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "Эльдар — злодей.", "sources": [], "insufficient": False}
    a = qa.ask("Какие глаза у Эльдара?", sid, model)
    assert a.insufficient and a.citations == [] and any("без ссылок" in i for i in a.issues)


def test_invented_labels_are_removed(world, model):
    qa, _, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "Глаза серые [F1] и красные [F99].", "sources": ["F1", "F99"], "insufficient": False}
    a = qa.ask("Какие глаза у Эльдара?", sid, model)
    assert "[F99]" not in a.answer and [c.label for c in a.citations] == ["F1"]
    assert any("F99" in i for i in a.issues)


def test_only_invented_labels_means_insufficient(world, model):
    qa, _, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "Он хороший [F77].", "sources": ["F77"], "insufficient": False}
    assert qa.ask("Какие глаза у Эльдара?", sid, model).insufficient


def test_model_may_declare_insufficient(world, model):
    qa, _, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "Данных о цвете волос нет.", "sources": [], "insufficient": True}
    a = qa.ask("Какие волосы у Эльдара?", sid, model)
    assert a.insufficient and a.citations == []


def test_spoiler_guard_hides_later_volumes_from_materials(world, model):
    qa, llm, answers, sid, _, _ = world
    answers["handler"] = lambda prompt: {"answer": "ok [F1]", "sources": ["F1"], "insufficient": False}
    qa.ask("Какие глаза у Эльдара?", sid, model, up_to_book_order=1)
    assert "карие" not in llm.prompts[-1] and "серые" in llm.prompts[-1]
    qa.ask("Какие глаза у Эльдара?", sid, model)
    assert "карие" in llm.prompts[-1]


def test_unverified_items_never_reach_the_model(world, db, model):
    qa, llm, answers, sid, _, _ = world
    db.execute("UPDATE facts SET verification = 'unverified' WHERE statement LIKE '%карие%'")
    mats = qa.gather("Какие глаза у Эльдара?", sid)
    assert all("карие" not in m.text for m in mats if m.kind == "fact")


# ---------- экспорт ----------
def test_json_export_contains_only_verified_with_sources(world, db, tmp_path):
    _, _, _, sid, _, _ = world
    p = ExportService(db).export_json(sid, tmp_path / "out" / "kb.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["series"] and len(d["books"]) == 2 and {c["name"] for c in d["characters"]} >= {"Мира Ветрова", "Эльдар Корн"}
    assert all(g["sources"] for g in d["goals"]) and d["contradictions"]
    assert all(f["verification"] == "verified" for c in d["characters"] for fs in c["facts"].values() for f in fs)


def test_export_up_to_volume_hides_spoilers(world, db, tmp_path):
    _, _, _, sid, _, _ = world
    ex = ExportService(db)
    d1 = ex.collect(sid, up_to_book_order=1)
    assert len(d1["books"]) == 1 and d1["contradictions"] == []
    text = ex.export_markdown(sid, tmp_path / "v1.md", up_to_book_order=1).read_text(encoding="utf-8")
    assert "карие" not in text and "Пепел Западной башни" not in text and "без спойлеров" in text
    assert all(q["status"] != "resolved" or q["resolved_at"]["series_order"] <= 1 for q in d1["questions"])
    assert not any(e["series_order"] > 1 for e in d1["events"])
    full = ex.export_markdown(sid, tmp_path / "all.md").read_text(encoding="utf-8")
    assert "Пепел Западной башни" in full and "карие" in full


def test_csv_export_files_and_formula_guard(world, db, tmp_path):
    _, _, _, sid, _, _ = world
    files = ExportService(db).export_csv(sid, tmp_path / "csv")
    assert {f.name for f in files} == {f"{n}.csv" for n in ("characters", "goals", "relationships", "events", "questions", "threads", "motifs", "contradictions")}
    rows = list(csv.reader((tmp_path / "csv" / "characters.csv").open(encoding="utf-8-sig"), delimiter=";"))
    assert rows[0][0] == "Имя" and any(r[0] == "Мира Ветрова" for r in rows[1:])
    assert _csv_safe("=1+1") == "'=1+1" and _csv_safe("@x") == "'@x" and _csv_safe("обычный") == "обычный"


def test_html_export_escapes_markup(world, db, tmp_path):
    _, _, _, sid, _, _ = world
    db.execute("UPDATE entities SET description = '<script>alert(1)</script>' WHERE name = 'Лина'")
    out = ExportService(db).export_html(sid, tmp_path / "kb.html").read_text(encoding="utf-8")
    assert "<script>" not in out and "&lt;script&gt;" in out and out.startswith("<!doctype html>") and "<h1>" in out


# ---------- резервные копии ----------
def _make_data(db, data_dir):
    (data_dir / "settings.json").write_text('{"selected_model": "qwen3:8b"}', encoding="utf-8")
    (data_dir / "prompts").mkdir(exist_ok=True)
    (data_dir / "prompts" / "answer.md").write_text("свой промпт", encoding="utf-8")


def test_backup_roundtrip(world, db, data_dir, tmp_path):
    _make_data(db, data_dir)
    n_facts = db.scalar("SELECT COUNT(*) FROM facts")
    svc = BackupService(data_dir)
    z = svc.create(tmp_path / "b.zip")
    manifest = svc.verify(z)
    assert manifest["counts"]["facts"] == n_facts and "database/bookmind.sqlite" in manifest["files"]
    db.close()
    (data_dir / "database" / "bookmind.sqlite").write_bytes(b"broken")
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")
    keep = svc.restore(z)
    assert (keep / "settings.json").read_text(encoding="utf-8") == "{}"        # прежние данные сохранены
    restored = Database(data_dir / "database" / "bookmind.sqlite")
    assert restored.scalar("SELECT COUNT(*) FROM facts") == n_facts and restored.integrity_report()["ok"]
    assert json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))["selected_model"] == "qwen3:8b"
    assert (data_dir / "prompts" / "answer.md").read_text(encoding="utf-8") == "свой промпт"
    restored.close()


def test_backup_detects_tampering(world, db, data_dir, tmp_path):
    svc = BackupService(data_dir)
    z = svc.create(tmp_path / "b.zip")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n) + (b"x" if n == "database/bookmind.sqlite" else b""))
    with pytest.raises(BackupError, match="Контрольная сумма"):
        svc.verify(bad)


def test_restore_rejects_foreign_and_unsafe_archives(world, data_dir, tmp_path):
    svc = BackupService(data_dir)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(BackupError):
        svc.verify(junk)
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("a.txt", "x")
    with pytest.raises(BackupError, match="manifest"):
        svc.verify(other)
    evil = tmp_path / "evil.zip"
    good = svc.create(tmp_path / "g.zip")
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(evil, "w") as dst:
        m = json.loads(src.read("manifest.json"))
        m["files"]["../evil.txt"] = {"sha256": "0", "size": 1}
        dst.writestr("manifest.json", json.dumps(m))
        for n in src.namelist():
            if n != "manifest.json":
                dst.writestr(n, src.read(n))
        dst.writestr("../evil.txt", "x")
    with pytest.raises(BackupError, match="Небезопасный"):
        svc.verify(evil)
    assert not (tmp_path.parent / "evil.txt").exists()


def test_failed_restore_does_not_touch_current_data(world, db, data_dir, tmp_path):
    svc = BackupService(data_dir)
    before = db.scalar("SELECT COUNT(*) FROM facts")
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"x")
    with pytest.raises(BookMindError):
        svc.restore(junk)
    assert db.scalar("SELECT COUNT(*) FROM facts") == before
    assert not (data_dir / "backups").exists() or not list((data_dir / "backups").glob("pre-restore-*"))
