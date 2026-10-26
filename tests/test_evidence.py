from bookmind.analysis.verification import QuoteVerifier
from tests.fixtures.novel import BOOK1, BOOK2
from tests.fixtures.scripted_llm import B1, B2, CORE

BAD_QUOTE = "Мира владела боевой магией огня"


def _chapters():
    return {(B1, t): x for t, x in BOOK1} | {(B2, t): x for t, x in BOOK2}


def _quotes(resp):
    for key in ("entities", "facts", "events"):
        for item in resp.get(key, []):
            yield item["quote"]


def test_all_gold_quotes_exist_in_novel_except_the_planted_bad_one():
    bad = []
    for key, resp in CORE.items():
        text = _chapters()[key]
        v = QuoteVerifier(text)
        for q in _quotes(resp):
            if not v.check(q).verified:
                bad.append(q)
    assert bad == [BAD_QUOTE]


def test_verifier_positions_point_to_original_text():
    text = BOOK1[0][1]
    v = QuoteVerifier(text)
    r = v.check("я найду того кто это сделал и заставлю его заплатить")
    assert r.verified and text[r.start:r.end].startswith("Я найду")
    assert not v.check("").verified and not v.check("Мира сбежала из города ночью").verified


def test_gold_deep_quotes_exist_in_novel():
    from tests.fixtures.scripted_llm import DEEP
    bad = []
    for key, resp in DEEP.items():
        v = QuoteVerifier(_chapters()[key])
        for group in ("goals", "relationships", "plot_threads", "questions", "motifs"):
            for item in resp[group]:
                if not v.check(item["quote"]).verified:
                    bad.append((key[1], item["quote"]))
    assert bad == []
