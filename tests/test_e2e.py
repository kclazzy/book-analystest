"""Сквозной сценарий: импорт EPUB → полный анализ → вопрос → экспорт → резервная копия → восстановление."""
import json

from bookmind.analysis.extractor import StructuredCaller
from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.llm.prompts import PromptStore
from bookmind.services.backup import BackupService
from bookmind.services.exports import ExportService
from bookmind.services.qa import QAService
from bookmind.storage.database import Database
from tests.fixtures.builders import write_epub
from tests.fixtures.novel import BOOK1, BOOK1_TITLE
from tests.fixtures.scripted_llm import ScriptedLLM


def test_import_to_export_and_restore(db, library, settings, data_dir, tmp_path, model):
    imp = library.import_book(write_epub(tmp_path / "b.epub", BOOK1_TITLE, "Автор", BOOK1), title=BOOK1_TITLE)
    assert imp.chapters == 8
    llm = ScriptedLLM(extra={"answer": {"handler": lambda p: {"answer": "Мира нашла ключ в руке Аларика [F1].", "sources": ["F1"], "insufficient": False}}})
    p = AnalysisPipeline(db, llm, settings, data_dir)
    assert p.run(p.start(imp.book_id, model, stages=AnalysisPipeline.STAGES_ORDER)).status == "completed"
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (imp.book_id,))

    ans = QAService(db, StructuredCaller(llm, settings), PromptStore(data_dir)).ask("Где Мира нашла серебряный ключ?", sid, model)
    assert not ans.insufficient and ans.citations and ans.citations[0].book_title

    ex = ExportService(db)
    js = json.loads(ex.export_json(sid, tmp_path / "e" / "kb.json").read_text(encoding="utf-8"))
    assert len(js["characters"]) >= 4 and js["goals"] and js["questions"]
    ex.export_csv(sid, tmp_path / "e" / "csv")
    ex.export_html(sid, tmp_path / "e" / "kb.html")

    svc = BackupService(data_dir)
    z = svc.create(tmp_path / "b.zip")
    facts = db.scalar("SELECT COUNT(*) FROM facts")
    db.close()
    (data_dir / "database" / "bookmind.sqlite").unlink()
    svc.restore(z)
    again = Database(data_dir / "database" / "bookmind.sqlite")
    try:
        assert again.scalar("SELECT COUNT(*) FROM facts") == facts and again.integrity_report()["ok"]
        assert ExportService(again).collect(sid)["goals"]
    finally:
        again.close()
