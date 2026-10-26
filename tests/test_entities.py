import pytest

from bookmind.analysis.entity_resolver import EntityResolver
from bookmind.analysis.models import load_chunk_ref
from bookmind.domain.enums import EntityType
from bookmind.domain.exceptions import BookMindError
from bookmind.services.entities import EntityService
from bookmind.storage.database import new_id, now_iso
from tests.conftest import MODEL
from tests.fixtures.builders import write_txt
from tests.fixtures.novel import BOOK1, BOOK2, BOOK2_TITLE

C = EntityType.CHARACTER


@pytest.fixture
def two_books(library, tmp_path, book1):
    b1 = library.repo.get_book(book1.book_id)
    b2 = library.import_book(write_txt(tmp_path / "book2.txt", BOOK2), series_id=b1["series_id"], title=BOOK2_TITLE)
    ch1 = [load_chunk_ref(library.db, c["id"]) for c in library.repo.chunks_of(book1.book_id)]
    ch2 = [load_chunk_ref(library.db, c["id"]) for c in library.repo.chunks_of(b2.book_id)]
    return b1["series_id"], ch1, ch2


def test_same_name_in_same_book_links_and_adds_alias(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Мира Ветрова", C, chunk=ch1[0], run_id=None, aliases=["Мира"])
    b = r.resolve("Мира", C, chunk=ch1[1], run_id=None)
    assert a.created and not b.created and a.entity_id == b.entity_id and b.resolution == "alias"
    assert db.scalar("SELECT COUNT(*) FROM entities") == 1


def test_same_name_different_type_is_not_merged(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Ключ", EntityType.ITEM, chunk=ch1[0], run_id=None)
    b = r.resolve("Ключ", C, chunk=ch1[0], run_id=None)
    assert a.entity_id != b.entity_id


def test_partial_name_is_candidate_only_never_auto_merged(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Мира Ветрова", C, chunk=ch1[0], run_id=None)  # без алиаса «Мира»
    b = r.resolve("Мира", C, chunk=ch1[1], run_id=None)
    assert b.created and b.entity_id != a.entity_id and b.candidate_ids == [a.entity_id]
    cand = db.one("SELECT * FROM merge_candidates")
    assert cand["status"] == "pending" and cand["score"] < 0.7


def test_cross_book_exact_name_needs_evidence(db, two_books):
    sid, ch1, ch2 = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Тихон Бреве", C, chunk=ch1[3], run_id=None, descriptor="подмастерье кузнеца")
    weak = r.resolve("Тихон Бреве", C, chunk=ch2[1], run_id=None)            # ничем не подтверждено
    assert weak.created and weak.resolution == "new" and db.scalar("SELECT status FROM entities WHERE id = ?", (weak.entity_id,)) == "needs_review"
    ok = EntityResolver(db, sid).resolve("Тихон Бреве", C, chunk=ch2[1], run_id=None, offered_ids=frozenset({a.entity_id}),
                                         descriptor="друг-кузнец")
    # теперь два одноимённых кандидата: побеждает та же книга (weak), а не старая запись
    assert ok.entity_id == weak.entity_id and not ok.created


def test_cross_book_offered_by_context_links(db, two_books):
    sid, ch1, ch2 = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Эльдар Корн", C, chunk=ch1[2], run_id=None)
    b = r.resolve("Эльдар Корн", C, chunk=ch2[0], run_id=None, offered_ids=frozenset({a.entity_id}))
    assert b.entity_id == a.entity_id and b.score >= 0.7


def test_ambiguity_creates_provisional_and_candidates(db, two_books):
    sid, ch1, ch2 = two_books
    r = EntityResolver(db, sid)
    now = now_iso()
    ids = []
    for n in range(2):  # два разных человека с одним именем, созданные вручную/импортом
        eid = new_id()
        db.insert("entities", {"id": eid, "series_id": sid, "type": "character", "name": "Иван", "name_norm": "иван",
                               "status": "active", "first_book_id": ch1[0].book_id, "created_at": now})
        ids.append(eid)
    res = r.resolve("Иван", C, chunk=ch2[0], run_id=None)
    assert res.resolution == "ambiguous" and res.created and set(res.candidate_ids) == set(ids)
    assert db.scalar("SELECT COUNT(*) FROM merge_candidates WHERE status='pending'") == 2
    assert db.scalar("SELECT status FROM entities WHERE id = ?", (res.entity_id,)) == "needs_review"


def test_alias_that_belongs_to_other_entity_is_not_copied(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Тихон Бреве", C, chunk=ch1[3], run_id=None, aliases=["Тихон"])
    b = r.resolve("Тихон Рыбак", C, chunk=ch1[4], run_id=None, aliases=["Тихон"])
    assert a.entity_id != b.entity_id
    assert db.scalar("SELECT COUNT(*) FROM entity_aliases WHERE alias_norm = 'тихон'") == 1


def test_manual_merge_moves_everything_and_keeps_history(db, two_books, pipeline, book1, model):
    sid, ch1, _ = two_books
    pipeline.run(pipeline.start(book1.book_id, model))
    r = EntityResolver(db, sid)
    dup = r.resolve("Мира", C, chunk=ch1[0], run_id=None).entity_id  # новая сущность-дубликат (имя только частично совпадает)
    mira = db.scalar("SELECT id FROM entities WHERE name = 'Мира Ветрова'")
    if dup == mira:  # «Мира» уже алиас — создаём дубликат принудительно
        dup = new_id()
        db.insert("entities", {"id": dup, "series_id": sid, "type": "character", "name": "М. Ветрова", "name_norm": "м ветрова",
                               "status": "active", "first_book_id": ch1[0].book_id, "created_at": now_iso()})
    db.insert("facts", {"id": new_id(), "series_id": sid, "book_id": ch1[0].book_id, "chapter_id": ch1[0].chapter_id,
                        "chunk_id": ch1[0].chunk_id, "entity_id": dup, "category": "other", "statement": "тест",
                        "statement_norm": "тест", "kind": "explicit", "verification": "verified", "created_at": now_iso()})
    svc = EntityService(db)
    facts_before = db.scalar("SELECT COUNT(*) FROM facts WHERE entity_id = ?", (mira,))
    svc.merge(mira, dup, reason="проверка")
    assert db.scalar("SELECT COUNT(*) FROM facts WHERE entity_id = ?", (mira,)) == facts_before + 1
    row = db.one("SELECT * FROM entities WHERE id = ?", (dup,))
    assert row["status"] == "merged" and row["merged_into"] == mira
    assert db.scalar("SELECT COUNT(*) FROM change_log WHERE owner_id = ? AND field = 'merge'", (mira,)) == 1
    assert EntityResolver(db, sid).follow(dup) == mira
    assert db.integrity_report()["ok"]
    # слитая сущность больше не участвует в сопоставлении
    again = EntityResolver(db, sid).resolve("М. Ветрова", C, chunk=ch1[1], run_id=None)
    assert again.entity_id == mira or again.created


def test_merge_validations(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Аларик", C, chunk=ch1[0], run_id=None).entity_id
    k = r.resolve("Ключ", EntityType.ITEM, chunk=ch1[0], run_id=None).entity_id
    svc = EntityService(db)
    with pytest.raises(BookMindError):
        svc.merge(a, a)
    with pytest.raises(BookMindError):
        svc.merge(a, k)  # разные типы


def test_accept_and_reject_candidates(db, two_books):
    sid, ch1, _ = two_books
    r = EntityResolver(db, sid)
    a = r.resolve("Мира Ветрова", C, chunk=ch1[0], run_id=None)
    b = r.resolve("Мира", C, chunk=ch1[1], run_id=None)
    svc = EntityService(db)
    cands = svc.pending_candidates(sid)
    assert len(cands) == 1 and {cands[0]["name_a"], cands[0]["name_b"]} == {"Мира Ветрова", "Мира"}
    keep = svc.accept(cands[0]["id"])
    assert keep == a.entity_id and svc.pending_candidates(sid) == []
    assert db.scalar("SELECT status FROM entities WHERE id = ?", (b.entity_id,)) == "merged"

    c = r.resolve("Лина Ветрова", C, chunk=ch1[1], run_id=None)
    d = r.resolve("Лина", C, chunk=ch1[2], run_id=None)
    cand = svc.pending_candidates(sid)[0]
    svc.reject(cand["id"])
    r.resolve("Лина", C, chunk=ch1[3], run_id=None)  # повторное упоминание не воскрешает отклонённого кандидата
    assert svc.pending_candidates(sid) == []
    assert db.scalar("SELECT status FROM entities WHERE id = ?", (d.entity_id,)) == "active"
