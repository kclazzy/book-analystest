import pytest

from bookmind.creation import qa
from bookmind.creation.schemas import Cast, CastMember, ChapterPlan

PLAN = ChapterPlan(title="Отсек 1", summary="Вера находит в шлюзе пропавший журнал дежурств и скрывает находку от экипажа",
                   pov="Вера Лунина", location="шлюз", goal="найти журнал", conflict="нехватка кислорода", outcome="журнал найден",
                   hook="в динамиках звучит чужой голос")
CAST = Cast(members=[CastMember(role="Протагонист", name="Вера Лунина"), CastMember(role="Союзник", name="Осип Градов")])
GOOD = ("Вера Лунина вошла в шлюз и сразу увидела пропавший журнал дежурств на полу. Она подняла находку и спрятала её от экипажа. "
        "Кислорода оставалось мало, а время уходило. Осип Градов ждал её у выхода и ничего не знал. "
        "В динамиках вдруг прозвучал чужой голос.")


def codes(findings):
    return {f.code for f in findings}


def test_clean_chapter_has_no_findings():
    assert qa.check_chapter(GOOD, PLAN, CAST, target_words=30) == []


@pytest.mark.parametrize("text,code,severity", [
    (GOOD.replace("шлюз", "шлюз 门口"), "foreign_script", "error"),
    (GOOD.rstrip(".") + " и тогда она", "truncated", "error"),
    ("Конечно! Вот глава, которую вы просили. " + GOOD, "meta", "error"),
    (GOOD + " Как языковая модель, я не могу продолжить.", "meta", "error"),
    ((GOOD + " ") + "Она замерла и не двигалась с места. " * 4, "loop", "error"),
    (GOOD.replace("«", "").replace("прозвучал", "прозвучал «чужой"), "quotes", "warning"),
])
def test_chapter_problems_are_detected(text, code, severity):
    found = [f for f in qa.check_chapter(text, PLAN, CAST, target_words=30) if f.code == code]
    assert found and found[0].severity == severity and found[0].message


def test_plan_adherence_and_pov():
    off = "Осип Градов долго пил чай в кают-компании и рассказывал анекдоты про старые времена. Все смеялись и расходились спать."
    f = codes(qa.check_chapter(off, PLAN, CAST, target_words=20))
    assert "pov_missing" in f and "plan_coverage" in f


def test_unknown_names_need_repetition_and_ignore_cast_and_sentence_starts():
    text = GOOD + " Потом Кронов сказал, что Кронов всё видел. Позже Кронов ушёл. Вера Лунина промолчала."
    f = [x for x in qa.check_chapter(text, PLAN, CAST, target_words=30) if x.code == "unknown_names"]
    assert f and "Кронов" in f[0].message and "Вера" not in f[0].message and "Осип" not in f[0].message
    assert "unknown_names" not in codes(qa.check_chapter(GOOD + " Один раз появился Кронов.", PLAN, CAST, target_words=30))


def test_overlong_chapter_warns():
    assert "too_long" in codes(qa.check_chapter(GOOD + " " + GOOD.replace("Вера", "Дина") * 8, PLAN, CAST, target_words=30))


# ---------- по всей книге ----------
def plans(n):
    return {i: ChapterPlan(title=f"Глава {i}", summary=f"Событие номер {i} происходит на станции и меняет планы экипажа",
                           pov="Вера Лунина") for i in range(n)}


def make_text(i, body=""):
    return (f"Вера Лунина в главе {i} изучала событие номер {i} на станции и меняла планы экипажа. " + body + " " +
            " ".join(f"Уникальное предложение {i}-{k} про станцию и людей." for k in range(8)))


def test_book_report_missing_and_uneven_and_absent_cast():
    texts = {0: make_text(0), 1: "", 2: make_text(2, "Слово " * 400), 3: make_text(3)}
    rep = qa.check_book(texts, plans(4), CAST, n_chapters=5)
    f = {(x.code, x.chapter) for x in rep.findings}
    assert ("missing_text", 1) in f and ("missing_text", 4) in f                         # главы нет вообще и пустая
    assert any(c == "uneven_length" and ch == 2 for c, ch in f)
    assert any(x.code == "cast_absent" and "Осип Градов" in x.message for x in rep.findings)
    assert rep.errors and rep.chapters_checked == 3


def test_duplicate_paragraph_across_chapters_and_recurring_phrase():
    shared = "Ветер гнал по пустой палубе обрывки бумаг, и никто не знал, откуда взялся этот странный шум в трубах станции."
    texts = {i: make_text(i, shared if i in (0, 2) else "") for i in range(6)}
    for i in range(1, 6):
        texts[i] += " Кислород и дыхание кончаются в каждой главе."
    rep = qa.check_book(texts, plans(6), CAST, n_chapters=6)
    dup = [x for x in rep.findings if x.code == "cross_duplicate"]
    assert len(dup) == 1 and "главах 1, 3" in dup[0].message
    rec = [x for x in rep.findings if x.code == "phrase_recurrence"]
    assert rec and any("кислород" in x.message for x in rec)


def test_same_openings_warn():
    texts = {i: "Ночью на станции стало тихо. " + make_text(i) for i in range(4)}
    assert any(x.code == "same_start" for x in qa.check_book(texts, plans(4), CAST, n_chapters=4).findings)


def test_markdown_report_lists_chapters_and_summary():
    texts = {0: GOOD.rstrip(".") + " и тогда", 1: make_text(1)}
    rep = qa.check_book(texts, plans(2), CAST, n_chapters=2)
    md = rep.to_markdown({0: "Первая", 1: "Вторая"})
    assert md.startswith("# Проверка книги") and "Глава 1. Первая" in md and "оборван" in md
    assert "ошибок: " in md and rep.as_dict()["errors"] >= 1
    clean = qa.check_book({0: GOOD}, {0: PLAN}, Cast(members=[CAST.members[0], CAST.members[1]]), n_chapters=1)
    assert clean.findings == [] and "Замечаний нет" in clean.to_markdown({0: "Отсек 1"})


# ---------- замечания, найденные на реальной книге ----------
LONG = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до выхода."


@pytest.mark.parametrize("text,code", [
    (GOOD + " " + LONG + " Осип Градов молчал. " + LONG, "dup_sentence"),
    (GOOD + " Она увидела tunnel впереди.", "latin"),
    (GOOD + " Она почувствовала инстинkтивный страх.", "latin"),
    ("Место действия: шлюз\nТочка зрения: Вера\n\n" + GOOD, "service_header"),
    (GOOD + " Последний кадр: Вера уходит в темноту.", "meta"),
    (GOOD.rstrip(".") + ". Вместе они начали новую эру, и впереди их ждало светлое будущее.", "cliche_ending"),
])
def test_new_chapter_problems_are_detected(text, code):
    assert code in codes(qa.check_chapter(text, PLAN, CAST, target_words=30))


def test_near_duplicate_sentence_is_found_and_dialogue_is_not_confused():
    a = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до самого выхода."
    b = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до самого выхода!"
    assert "dup_sentence" in codes(qa.check_chapter(GOOD + " " + a + " Тишина. " + b, PLAN, CAST, 30))
    assert "dup_sentence" not in codes(qa.check_chapter(GOOD, PLAN, CAST, 30))


def test_latin_names_from_cast_are_allowed():
    cast = Cast(members=[CastMember(role="Союзник", name="Tik")])
    assert "latin" not in codes(qa.check_chapter(GOOD + " Tik кивнул.", PLAN, cast, 30))


def test_dedupe_text_removes_repeated_sentences_and_paragraphs_keeps_first():
    p = "Вера Лунина медленно шла по тёмному коридору шлюза и считала шаги до выхода."
    t = f"{p} Осип молчал.\n\n{GOOD}\n\n{p} Осип молчал.\n\n{GOOD}"
    out = qa.dedupe_text(t)
    assert out.count("медленно шла") == 1 and out.count("прозвучал чужой голос") == 1 and "Осип молчал" in out
    assert qa.dedupe_text(GOOD) == GOOD


def test_strip_service_lines():
    assert qa.strip_service_lines("Место действия: шлюз\nТекст сцены.\nКадр 3: финал\nДальше.") == "Текст сцены.\nДальше."


def book_texts(n=6, end=None):
    return {i: f"Глава про шлюз номер {i}. " + GOOD + (end or "") for i in range(n)}


def test_first_person_chapters_are_flagged_only_when_book_is_third_person():
    fp = "Я вошёл в шлюз. Мне было страшно, и я не знал, что меня ждёт. Мы с Осипом пошли дальше, и нас никто не видел."
    t = book_texts()
    t[2] = fp + " " + GOOD
    rep = qa.check_book(t, {}, CAST, 6)
    assert [f.chapter for f in rep.findings if f.code == "first_person"] == [2]
    t_all = {i: fp + f" Номер {i}. " + GOOD for i in range(6)}
    assert not [f for f in qa.check_book(t_all, {}, CAST, 6).findings if f.code == "first_person"]


def test_dialogue_with_i_does_not_count_as_first_person():
    t = book_texts()
    t[1] = GOOD + ' — Я знаю, мне страшно, мы не успеем, — сказал Осип. «Я здесь, — подумала она, — мы справимся».'
    assert not [f for f in qa.check_book(t, {}, CAST, 6).findings if f.code == "first_person"]


def test_overused_word_is_reported_for_the_book():
    t = {i: f"Номер {i}. " + GOOD + " Резонанс дрожал, резонанс нарастал, и резонанс гудел в стенах. Резонанс слышали все. " * 2 for i in range(8)}
    f = [x for x in qa.check_book(t, {}, CAST, 8).findings if x.code == "word_overuse"]
    assert f and "резонанс" in f[0].message.casefold() and f[0].related and f[0].severity == "warning"


def test_cliche_endings_pattern_is_reported_once_for_book():
    end = " Вместе они начали новую эру, и впереди их ждало будущее."
    rep = qa.check_book(book_texts(6, end), {}, CAST, 6)
    assert [f.chapter for f in rep.findings if f.code == "cliche_ending"] == [None]
