import json
import logging
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from bookmind.system import errorlog as el
from bookmind.system.errorlog import ErrorLog, install_hooks, scrub


@pytest.fixture
def log(tmp_path):
    return ErrorLog(tmp_path / "logs" / "errors.jsonl", "9.9.9")


def boom():
    raise ValueError("сломалось")


def test_record_and_read_back_newest_first(log):
    log.record("первая", source="анализ", level="WARNING")
    try:
        boom()
    except ValueError as e:
        log.record("вторая", e, source="поток", context={"книга": "Тест"})
    items = log.entries()
    assert [e.message for e in items] == ["вторая", "первая"] and items[0].version == "9.9.9"
    assert "ValueError: сломалось" in items[0].traceback and items[0].context == {"книга": "Тест"}
    assert [e.message for e in log.entries(level="WARNING")] == ["первая"] and log.count() == 2


def test_repeats_are_counted_not_duplicated(log):
    for _ in range(5):
        log.record("Ollama недоступен", source="сеть")
    log.record("другое", source="сеть")
    items = log.entries()
    assert len(items) == 2 and items[1].count == 5 and log.revision == 6


def test_personal_paths_are_scrubbed(log):
    home = str(Path.home())
    s = scrub(f'File "{home}/proj/a.py"; C:\\Users\\Иван Петров\\AppData\\x; /home/anna/secret; /Users/bob/x')
    assert home not in s and "Иван" not in s and "anna" not in s and "bob" not in s and "<user>" in s
    log.record(f"ошибка в {home}/data", source="x", context={"путь": f"{home}/b"})
    e = log.entries()[0]
    assert home not in e.message and home not in e.context["путь"]


def test_corrupt_lines_and_missing_file_are_tolerated(log):
    assert log.entries() == [] and log.count() == 0
    log.record("ok1", source="a")
    with log.path.open("a", encoding="utf-8") as f:
        f.write('{"обрыв": \n')
    log.record("ok2", source="b")
    assert [e.message for e in log.entries()] == ["ok2", "ok1"]


def test_log_is_trimmed_when_too_big(log, monkeypatch):
    monkeypatch.setattr(el, "MAX_BYTES", 3000)
    for i in range(80):
        log.record(f"ошибка номер {i} " + "x" * 50, source=f"s{i}")
    items = log.entries(limit=1000)
    assert items[0].message.startswith("ошибка номер 79") and 5 <= len(items) < 80
    assert log.path.stat().st_size < 6000


def test_clear(log):
    log.record("a", source="s")
    log.clear()
    assert log.entries() == [] and not log.path.exists()
    log.clear()


def test_unwritable_path_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = ErrorLog(blocker / "sub" / "e.jsonl")
    bad.record("не пишется", source="s")
    assert bad.entries() == []


def test_hooks_capture_uncaught_exceptions_logging_and_threads(log, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    root = logging.getLogger()
    before = list(root.handlers)
    install_hooks(log)
    try:
        try:
            boom()
        except ValueError:
            sys.excepthook(*sys.exc_info())
        t = threading.Thread(target=boom)
        t.start()
        t.join()
        logging.getLogger("bookmind.test").error("ошибка из logging")
        logging.getLogger("bookmind.test").info("не ошибка")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
    msgs = {(e.source, e.level): e.message for e in log.entries()}
    assert msgs[("приложение", "CRITICAL")].startswith("Необработанная ошибка: сломалось")
    assert msgs[("поток", "CRITICAL")].startswith("Ошибка в фоновом потоке")
    assert msgs[("лог:bookmind.test", "ERROR")] == "ошибка из logging" and len(log.entries()) == 3


def test_report_zip_has_system_errors_and_log_tail_only(log, tmp_path):
    log.record("упало", source="анализ", context={"этап": "core"})
    lf = tmp_path / "bookmind.log"
    lf.write_text("\n".join(f"строка {i}" for i in range(800)), encoding="utf-8")
    out = log.export_report(tmp_path / "out" / "r.zip", log_file=lf, extra={"Модель": "qwen"})
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["bookmind.log", "errors.txt", "system.txt"]
        assert "BookMind 9.9.9" in z.read("system.txt").decode() and "Модель: qwen" in z.read("system.txt").decode()
        err = z.read("errors.txt").decode()
        assert "упало" in err and "этап: core" in err
        tail = z.read("bookmind.log").decode().splitlines()
        assert len(tail) == 500 and tail[-1] == "строка 799"
    empty = ErrorLog(tmp_path / "e2.jsonl").export_report(tmp_path / "r2.zip")
    assert "Ошибок нет" in zipfile.ZipFile(empty).read("errors.txt").decode()
