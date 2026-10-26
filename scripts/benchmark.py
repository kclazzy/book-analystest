"""Замер производительности без LLM: импорт, разбиение на фрагменты, FTS-индексация, поиск, размер БД.
Запуск: python scripts/benchmark.py [число_глав]   (по умолчанию 300 глав ≈ 1,2 млн знаков, как толстая книга)."""
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bookmind.config import Settings  # noqa: E402
from bookmind.services.library import LibraryService  # noqa: E402
from bookmind.services.search import SearchIndex  # noqa: E402
from bookmind.storage.database import Database  # noqa: E402

WORDS = ("мира алхимик башня ключ огонь тихон гильдия свиток дорога замок ночь лина письмо город стража "
         "река тайна память дверь клинок шёпот пламя пепел мост зал печать рецепт колокол").split()


def make_book(chapters: int, rnd: random.Random) -> str:
    parts = []
    for i in range(1, chapters + 1):
        parts.append(f"Глава {i}. Название {i}\n")
        for _ in range(3):
            parts.append(" ".join(rnd.choice(WORDS) for _ in range(rnd.randint(150, 250))).capitalize() + ".\n")
    return "\n".join(parts)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    tmp = Path(tempfile.mkdtemp())
    text = make_book(n, random.Random(1))
    src = tmp / "big.txt"
    src.write_text(text, encoding="utf-8")
    db = Database(tmp / "db" / "bookmind.sqlite")
    lib = LibraryService(db, Settings(), tmp)
    t = time.perf_counter()
    res = lib.import_book(src)
    t_imp = time.perf_counter() - t
    t = time.perf_counter()
    hits = [len(SearchIndex(db).search(q, limit=20)) for q in ("ключ башня", "алхимик гильдия", "пепел мост") * 10]
    t_search = (time.perf_counter() - t) / 30
    size = Path(db.path).stat().st_size / 1e6
    print(f"Текст: {len(text) / 1e6:.2f} млн знаков, глав {res.chapters}, фрагментов {res.chunks}")
    print(f"Импорт+разбиение+FTS: {t_imp:.1f} с ({len(text) / t_imp / 1e3:.0f} тыс. знаков/с)")
    print(f"Поиск: {t_search * 1000:.1f} мс на запрос (найдено до {max(hits)})")
    print(f"Размер БД: {size:.1f} МБ")
    print(f"Оценка LLM-этапа: {res.chunks} вызовов core + {res.chunks} deep; при 15 с/вызов ≈ {res.chunks * 2 * 15 / 3600:.1f} ч (оценка, не измерение)")


if __name__ == "__main__":
    main()
