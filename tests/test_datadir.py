import sqlite3

import pytest

from bookmind import config
from bookmind.domain.exceptions import BookMindError
from bookmind.system import datadir
from tests.fixtures.builders import write_txt
from tests.fixtures.novel import BOOK1, BOOK1_TITLE


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.delenv(config.ENV_DATA_DIR, raising=False)
    monkeypatch.setattr(config, "home_dir", lambda: h)
    return h


def test_default_follows_pointer_and_falls_back(home, tmp_path):
    assert config.default_data_dir() == home
    target = tmp_path / "D" / "books"
    target.mkdir(parents=True)
    config.write_pointer(target)
    assert config.default_data_dir() == target
    target.rmdir()
    assert config.default_data_dir() == home           # диск/папка недоступны — не создаём пустую базу в другом месте
    config.write_pointer(None)
    assert config.read_pointer() is None


def test_env_var_wins_over_pointer(home, tmp_path, monkeypatch):
    config.write_pointer(tmp_path)
    monkeypatch.setenv(config.ENV_DATA_DIR, str(tmp_path / "env"))
    assert config.default_data_dir() == tmp_path / "env"


def test_target_checks(home, tmp_path):
    cur = tmp_path / "cur"
    cur.mkdir()
    assert not datadir.check_target(cur, cur).ok
    assert not datadir.check_target(cur, cur / "inner").ok
    full = tmp_path / "full"
    full.mkdir()
    (full / "x.txt").write_text("1")
    assert "не пуста" in datadir.check_target(cur, full).message
    empty = tmp_path / "new" / "deep"
    assert datadir.check_target(cur, empty).ok and not datadir.check_target(cur, empty).has_data


def test_change_copies_database_and_files(home, tmp_path):
    from bookmind.ui.context import AppContext
    ctx = AppContext(data_dir=tmp_path / "cur")
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    (ctx.data_dir / "exports" / "r.md").write_text("отчёт", encoding="utf-8")
    ctx.save_settings()
    target = tmp_path / "moved"
    datadir.change_data_dir(ctx.data_dir, target)
    con = sqlite3.connect(target / "database" / "bookmind.sqlite")
    assert con.execute("select count(*) from books").fetchone()[0] == 1
    con.close()
    assert (target / "exports" / "r.md").read_text(encoding="utf-8") == "отчёт" and (target / "settings.json").exists()
    assert (ctx.data_dir / "database" / "bookmind.sqlite").exists()       # исходные данные не удалены
    assert config.default_data_dir() == target
    ctx.close()
    ctx2 = AppContext()
    assert ctx2.data_dir == target and len(ctx2.repo.list_books()) == 1
    ctx2.close()


def test_existing_data_folder_is_used_without_copy(home, tmp_path):
    from bookmind.ui.context import AppContext
    a = AppContext(data_dir=tmp_path / "a")
    a.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    a.close()
    cur = tmp_path / "cur"
    cur.mkdir()
    datadir.change_data_dir(cur, tmp_path / "a", copy=True)
    assert config.default_data_dir() == tmp_path / "a"
    with pytest.raises(BookMindError):
        datadir.change_data_dir(cur, cur / "sub")


def test_has_data(home, tmp_path):
    from bookmind.ui.context import AppContext
    empty = tmp_path / "e"
    empty.mkdir()
    assert not datadir.has_data(empty)
    ctx = AppContext(data_dir=tmp_path / "d")
    assert not datadir.has_data(ctx.data_dir)                      # пустая база и пустые папки — не данные
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    assert datadir.has_data(ctx.data_dir)
    ctx.close()
    broken = tmp_path / "broken"
    (broken / "database").mkdir(parents=True)
    (broken / "database" / "bookmind.sqlite").write_bytes(b"not a database")
    assert datadir.has_data(broken)
