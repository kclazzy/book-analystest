import json
import threading

import pytest

from bookmind.analysis.extractor import StructuredCaller
from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.creation import validation as val
from bookmind.creation.divergence import assign
from bookmind.creation.originality import OriginalCorpus
from bookmind.creation.schemas import AdaptParams, Cast, CastMember, ChapterPlan
from bookmind.creation.service import AdaptationService
from bookmind.creation.skeleton import SkeletonBuilder
from bookmind.domain.exceptions import BookMindError
from tests.fixtures import adapt_script as A
from tests.fixtures.scripted_llm import ScriptedLLM, unavailable

ORIGINAL = ["Мира", "Мире", "Аларик", "Эльдар", "Тихон", "Лина", "Ветрова", "Бреве", "Гильдия", "Западная башня"]


@pytest.fixture
def sid(db, book1, model, settings, data_dir):
    p = AnalysisPipeline(db, ScriptedLLM(), settings, data_dir)
    assert p.run(p.start(book1.book_id, model, stages=("core", "deep", "chapter_summary"))).status == "completed"
    return db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))


def make(db, settings, handler=None):
    llm = ScriptedLLM(extra={"adapt_handler": handler} if handler else {})
    return AdaptationService(db, StructuredCaller(llm, settings), settings), llm


# ---------- расхождение ----------
def test_assignment_is_deterministic_and_respects_chapter_count(db, sid):
    sk, _ = SkeletonBuilder(db).build(sid)
    p = AdaptParams(divergence="medium", chapters=10)
    a1, a2 = assign(sk, p, 7), assign(sk, p, 7)
    assert a1 == a2 and a1.chapters == 10 and all(0 <= b.chapter < 10 for b in a1.beats)
    assert assign(sk, p, 8) != a1


def test_weak_keeps_everything_strong_changes_more(db, sid):
    sk, _ = SkeletonBuilder(db).build(sid)
    weak = assign(sk, AdaptParams(divergence="weak"), 1)
    strong = assign(sk, AdaptParams(divergence="strong"), 1)
    assert weak.dropped == [] and weak.mutations == [] and len(weak.beats) == len(sk.beats)
    assert len(strong.dropped) > 0 and len(strong.mutations) > 0 and len(strong.beats) < len(sk.beats)
    assert len(strong.global_changes) > len(weak.global_changes)
    assert all("ИЗМЕНИТЬ ИСХОД" in b.instruction for b in strong.beats if b.mutation)


def test_strong_uses_abstract_text_weak_uses_detail(db, sid):
    sk, _ = SkeletonBuilder(db).build(sid)
    det = {b.id: b for b in sk.beats}
    weak = assign(sk, AdaptParams(divergence="weak"), 1)
    strong = assign(sk, AdaptParams(divergence="strong"), 1)
    assert all(b.text == det[b.id].detailed for b in weak.beats)
    assert all(b.text == det[b.id].abstract for b in strong.beats)


def test_keep_protects_groups(db, sid):
    sk, _ = SkeletonBuilder(db).build(sid)
    for seed in range(10):
        a = assign(sk, AdaptParams(divergence="strong", keep=["mystery", "flashback"]), seed)
        dropped_groups = {b.group for b in sk.beats if b.id in a.dropped}
        assert not any(g.startswith(("Q", "F")) for g in dropped_groups)
        assert not any(b.mutation for b in a.beats if b.group.startswith(("Q", "F")))


@pytest.mark.parametrize("level", ["weak", "medium", "strong"])
def test_mystery_order_survives_shifting(db, sid, level):
    sk, _ = SkeletonBuilder(db).build(sid)
    for seed in range(30):
        for n in (5, 8, 12):
            a = assign(sk, AdaptParams(divergence=level, chapters=n), seed)
            for g in {b.group for b in a.beats if b.group.startswith("Q")}:
                gb = [b for b in a.beats if b.group == g]
                raised = [b for b in gb if b.kind == "mystery_raised"]
                resolved = [b for b in gb if b.kind == "mystery_resolved"]
                if raised and resolved:
                    assert raised[0].chapter < resolved[0].chapter, (level, seed, n)
                    for c in (b for b in gb if b.kind == "clue"):
                        assert raised[0].chapter < c.chapter <= max(resolved[0].chapter, raised[0].chapter + 1)


# ---------- сходство ----------
@pytest.fixture
def corpus(db, sid, book1):
    return OriginalCorpus(db, sid, [book1.book_id])


def test_original_names_are_detected_in_any_case(corpus):
    c = corpus.check("Осип отдал письмо Мире, а Эльдара в тот день никто не видел.")
    assert c.flagged and {"Мира", "Эльдар"} & set(c.name_hits) and any("имена" in r for r in c.reasons)


def test_common_words_do_not_trigger_names(corpus):
    c = corpus.check("В мире станции было тихо, и линия связи молчала, а башня на Земле стояла пустой.")
    assert not c.name_hits and not c.flagged


def test_multiword_place_is_hard_hit_single_word_is_soft(corpus):
    assert "Западная башня" in corpus.find_names("Они пришли к Западной башне на рассвете")[0]
    hard, soft = corpus.find_names("Совет собрался в Гильдии вечером")
    assert not hard and "Гильдия" in soft


def test_rewritten_event_is_flagged_but_new_story_passes(corpus):
    copy = "Две сестры приходят к башне, огонь на ней гаснет, а ключ не поворачивается в замке железной двери"
    c = corpus.check(copy, "medium")
    assert c.similarity >= 0.55 and c.flagged and "слишком близко" in " ".join(c.reasons)
    fresh = corpus.check(A.SCENES[0] + ". Экипаж ищет причину неисправности в шлюзе.", "medium")
    assert not fresh.flagged and fresh.similarity < 0.3


def test_threshold_depends_on_divergence(corpus):
    text = "Приходят к башне, огонь гаснет, ключ не поворачивается в замке двери"
    assert corpus.check(text, "strong").similarity == corpus.check(text, "weak").similarity


def test_shingle_overlap_detects_verbatim_copy(corpus, db, book1):
    chunk = db.scalar("SELECT text FROM chunks WHERE book_id = ? ORDER BY idx LIMIT 1", (book1.book_id,))
    assert corpus.check(chunk[:600], with_shingles=True).shingle_overlap > 0.5
    assert corpus.check(A.SCENES[3] + " " + A.SCENES[4], with_shingles=True).shingle_overlap == 0.0


# ---------- проверки плана ----------
def test_validation_unknown_character_and_empty_fields():
    cast = Cast(members=[CastMember(role="Протагонист", name="Вера Лунина")])
    bad = ChapterPlan(title="", summary="коротко", characters=["Вера", "Незнакомец"])
    issues = val.validate_chapter(bad, cast)
    assert any("нет названия" in i for i in issues) and any("Незнакомец" in i for i in issues) and not any("«Вера»" in i for i in issues)
    assert val.validate_cast(cast, ["Протагонист", "Антагонист"]) == ["нет героя для роли «Антагонист»"]
    assert val.unused_cast({0: ChapterPlan(characters=["Вера"])}, Cast(members=[CastMember(name="Вера Лунина"), CastMember(name="Осип Градов")])) == ["Осип Градов"]


# ---------- сервис ----------
def run_ok(svc, pid, model, **kw):
    return svc.run(pid, model, **kw)


def test_full_run_produces_plan_that_follows_beats(db, sid, settings, model):
    svc, llm = make(db, settings)
    pid = svc.create(sid, AdaptParams(divergence="medium", seed=3))
    s = svc.run(pid, model)
    assert s.status == "completed" and s.flagged == []
    rep = svc.report(pid)
    assert len(rep["chapters"]) == 8 and all(c["status"] == "ok" for c in rep["chapters"])
    assert llm.count("adapt_premise") == 1 and llm.count("adapt_cast") == 1 and llm.count("adapt_chapter") == 8
    sk, asg = svc.skeleton(pid)
    covered = {b for p in svc.plans(pid).values() for b in p.beats_covered}
    assert {b.id for b in asg.beats} <= covered
    assert val.validate_global(svc.plans(pid), asg) == {}
    names = {m.name for m in svc.cast(pid).members}
    assert len(names) == len(sk.roles) and not names & set(ORIGINAL)


def test_bible_has_no_original_names_and_lists_differences(db, sid, settings, model, tmp_path):
    svc, _ = make(db, settings)
    pid = svc.create(sid, AdaptParams(divergence="strong", seed=1))
    svc.run(pid, model)
    text = svc.export_bible(pid, tmp_path / "bible.md").read_text(encoding="utf-8")
    assert "Тишина на орбите" in text and "## Герои" in text and "## План глав" in text and "Чем книга отличается" in text
    for w in ["Мира ", "Эльдар", "Тихон", "Аларик", "Западн", "Гильди"]:
        assert w not in text, w


def test_prompts_carry_beats_cast_and_previous_chapters(db, sid, settings, model):
    svc, llm = make(db, settings)
    pid = svc.create(sid, AdaptParams(divergence="weak", seed=1))
    svc.run(pid, model)
    chapter_prompts = [p for p in llm.prompts if "план главы" in p]
    assert "(это первая глава)" in chapter_prompts[0]
    assert "Глава 1 «Отсек 1»" in chapter_prompts[1] and "Вера Лунина" in chapter_prompts[1] and "[B" in chapter_prompts[1]
    skeleton_prompt = next(p for p in llm.prompts if "СКЕЛЕТ" in p)
    for w in ["Мира", "Эльдар", "Тихон", "Аларик", "башн"]:
        assert w not in skeleton_prompt, w


def test_strong_prompt_has_no_detail_weak_has(db, sid, settings, model):
    def beats_text(level):
        svc, llm = make(db, settings)
        pid = svc.create(sid, AdaptParams(divergence=level, seed=1))
        svc.run(pid, model)
        return "\n".join(llm.prompts)
    assert "обретает цель: " in beats_text("weak")
    assert "обретает цель: " not in beats_text("strong")


def test_chapter_with_original_names_is_replanned_with_feedback(db, sid, settings, model):
    seen = []
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and "план главы 3 из" in prompt:
            seen.append(prompt)
            if len(seen) == 1:
                d["summary"] = "Мира тайно встречается с Эльдаром у входа в Гильдию и передаёт ему письмо."
                d["characters"] = [d["pov"]]
        return d
    svc, _ = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1))
    s = svc.run(pid, model)
    assert s.status == "completed" and s.flagged == []
    ch = svc.report(pid)["chapters"][2]
    assert ch["attempts"] == 2 and ch["status"] == "ok" and "Мира" not in svc.plans(pid)[2].summary
    assert "ЗАМЕЧАНИЯ К ПРЕДЫДУЩЕМУ ВАРИАНТУ" in seen[1] and "имена или названия из оригинала" in seen[1]


def test_persistent_copying_is_flagged_not_hidden(db, sid, settings, model):
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and "план главы 2 из" in prompt:
            d["summary"] = "Тихон снова встречается с Эльдаром и обсуждает ключ от башни."
        return d
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1, max_replans=2))
    s = svc.run(pid, model)
    assert s.status == "completed" and s.flagged == [1]
    rep = svc.report(pid)
    assert rep["chapters"][1]["attempts"] == 3 and rep["chapters"][1]["issues"]
    assert "⚠" in svc.bible_markdown(pid)


def test_cast_missing_role_is_regenerated(db, sid, settings, model):
    calls = []
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_cast":
            calls.append(prompt)
            if len(calls) == 1:
                d["members"] = d["members"][:1]
        return d
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1))
    assert svc.run(pid, model).status == "completed"
    assert llm.count("adapt_cast") == 2 and "нет героя для роли" in calls[1]
    assert len(svc.cast(pid).members) >= 4


def test_pause_for_review_then_approve_without_new_calls(db, sid, settings, model):
    svc, llm = make(db, settings)
    pid = svc.create(sid, AdaptParams(seed=1, pause_after_plan=True))
    assert svc.run(pid, model).status == "awaiting_review"
    n = len(llm.calls)
    assert svc.run(pid, model).status == "awaiting_review" and len(llm.calls) == n
    svc.approve(pid)
    s = svc.run(pid, model)
    assert s.status == "completed" and len(llm.calls) == n


def test_cancel_and_resume_do_not_repeat_chapters(db, sid, settings, model):
    svc, llm = make(db, settings)
    pid = svc.create(sid, AdaptParams(seed=1))
    cancel = threading.Event()
    svc.run(pid, model, on_progress=lambda p: cancel.set() if p.stage == "plan" and p.done == 3 else None, cancel=cancel)
    assert svc._row(pid)["status"] == "paused"
    done = db.scalar("SELECT COUNT(*) FROM adaptation_chapters WHERE project_id = ? AND status = 'ok'", (pid,))
    assert 2 <= done < 8
    assert svc.run(pid, model).status == "completed"
    assert llm.count("adapt_chapter") == 8 and llm.count("adapt_premise") == 1 and llm.count("adapt_cast") == 1


def test_unavailable_ollama_pauses_and_resumes(db, sid, settings, model):
    state = {"n": 0}
    def handler(stage, prompt, default):
        if stage == "adapt_chapter":
            state["n"] += 1
            if state["n"] == 4:
                raise unavailable()
        return default(prompt)
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1))
    s = svc.run(pid, model)
    assert s.status == "paused" and "Ollama" in s.message
    assert svc.run(pid, model).status == "completed"


def test_manual_edit_is_rechecked(db, sid, settings, model):
    svc, _ = make(db, settings)
    pid = svc.create(sid, AdaptParams(seed=1))
    svc.run(pid, model)
    plan = svc.plans(pid)[0]
    plan.summary = "Тихон передаёт Эльдару ключ от Западной башни в Гильдии."
    chk = svc.update_plan(pid, 0, plan)
    assert chk.flagged and svc.report(pid)["chapters"][0]["status"] == "flagged"
    plan.summary = A.SCENES[5] + ". Экипаж изучает схему."
    assert not svc.update_plan(pid, 0, plan).flagged and svc.report(pid)["chapters"][0]["status"] == "ok"


def test_book_without_analysis_fails_with_clear_message(db, book1, settings, model):
    sid = db.scalar("SELECT series_id FROM books WHERE id = ?", (book1.book_id,))
    svc, _ = make(db, settings)
    pid = svc.create(sid, AdaptParams())
    s = svc.run(pid, model)
    assert s.status == "failed" and "проанализированных" in s.message


def test_create_validates_input_and_cascade_delete(db, sid, settings, model):
    svc, _ = make(db, settings)
    with pytest.raises(BookMindError):
        svc.create("no-such-series")
    with pytest.raises(BookMindError):
        svc.create(sid, AdaptParams.model_construct(divergence="extreme"))
    pid = svc.create(sid, AdaptParams(seed=1))
    svc.run(pid, model)
    db.execute("DELETE FROM series WHERE id = ?", (sid,))
    assert db.scalar("SELECT COUNT(*) FROM adaptation_projects") == 0 and db.scalar("SELECT COUNT(*) FROM adaptation_chapters") == 0


def test_creative_temperature_is_used(db, sid, settings, model):
    captured = []
    class Spy(ScriptedLLM):
        def chat(self, model, messages, **kw):
            captured.append((messages[-1]["content"][:40], kw["options"]["temperature"]))
            return super().chat(model, messages, **kw)
    llm = Spy()
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    pid = svc.create(sid, AdaptParams(seed=1, chapters=3))
    svc.run(pid, model)
    assert captured and all(t == settings.creative_temperature for _, t in captured)


# ---------- главы с пустым описанием ----------
def _plan_handler(mutate, chapter=3):
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and f"план главы {chapter} из" in prompt:
            mutate(d)
        return d
    return handler


def test_summary_under_another_key_is_not_lost(db, sid, settings, model):
    def mutate(d):
        d["description"] = d.pop("summary")
        d["name"] = d.pop("title")
    svc, llm = make(db, settings, _plan_handler(mutate))
    pid = svc.create(sid, AdaptParams(seed=1))
    assert svc.run(pid, model).status == "completed"
    ch = svc.report(pid)["chapters"][2]
    assert ch["status"] == "ok" and ch["attempts"] == 1 and len(ch["summary"]) > 20 and ch["title"] == "Отсек 3"
    assert llm.count("adapt_chapter") == 8                                      # переделывать не пришлось


def test_persistently_empty_summary_is_filled_from_plan_fields_and_flagged(db, sid, settings, model):
    def mutate(d):
        d["summary"] = ""
        d["goal"], d["conflict"], d["outcome"] = "найти журнал", "нехватка кислорода", "журнал найден, но страницы вырваны"
    svc, llm = make(db, settings, _plan_handler(mutate))
    pid = svc.create(sid, AdaptParams(seed=1, max_replans=2))
    s = svc.run(pid, model)
    assert s.flagged == [2] and llm.count("adapt_chapter") == 8 + 2            # две переделки, потом сдались
    ch = svc.report(pid)["chapters"][2]
    assert ch["attempts"] == 3 and "найти журнал" in ch["summary"] and "нехватка кислорода" in ch["summary"]
    assert any("составлено из полей" in i for i in ch["issues"])               # честно сказано, что описание не от модели
    assert "найти журнал" in svc.bible_markdown(pid)


def test_completely_empty_plan_gets_a_title_and_stays_flagged(db, sid, settings, model):
    def mutate(d):
        for k in list(d):
            d[k] = [] if isinstance(d[k], list) else ""
    svc, _ = make(db, settings, _plan_handler(mutate))
    pid = svc.create(sid, AdaptParams(seed=1, max_replans=1))
    s = svc.run(pid, model)
    assert s.flagged == [2]
    ch = svc.report(pid)["chapters"][2]
    assert ch["title"] == "Глава 3" and ch["status"] == "flagged" and any("пустое описание" in i for i in ch["issues"])


def test_premise_aliases(db, sid, settings, model):
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_premise":
            d["premise"] = d.pop("logline")
            d["conflict"] = d.pop("central_conflict")
        return d
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1))
    svc.run(pid, model)
    pr = svc.premise(pid)
    assert pr.logline and pr.central_conflict and llm.count("adapt_premise") == 1


def test_replan_fixes_chapter_without_touching_others(db, sid, settings, model):
    state = {"broken": True}

    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and "план главы 3 из" in prompt and state["broken"]:
            d["summary"] = ""
            d["goal"] = d["conflict"] = d["outcome"] = ""
        return d
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1, max_replans=1))
    assert svc.run(pid, model).flagged == [2]
    before = {i: p.summary for i, p in svc.plans(pid).items() if i != 2}
    calls = llm.count("adapt_chapter")
    state["broken"] = False
    s = svc.replan(pid, model, [2])
    assert s.flagged == [] and llm.count("adapt_chapter") == calls + 1           # переделана только одна глава
    assert len(svc.plans(pid)[2].summary) > 20 and {i: p.summary for i, p in svc.plans(pid).items() if i != 2} == before
    assert svc.report(pid)["chapters"][2]["status"] == "ok" and svc.report(pid)["chapters"][2]["attempts"] == 1
    with pytest.raises(BookMindError, match="Нет такой главы"):
        svc.replan(pid, model, [99])


def test_empty_chapters_are_found_and_replanned_in_one_go(db, sid, settings, model):
    state = {"broken": {1, 4, 6}}

    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter":
            n = int(__import__("re").search(r"план главы (\d+) из", prompt).group(1)) - 1
            if n in state["broken"]:
                d.update(summary="", goal="", conflict="", outcome="")
                if n == 4:
                    d["title"] = ""
        return d
    svc, llm = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1, max_replans=0))
    svc.run(pid, model)
    assert svc.empty_chapters(pid) == [1, 4, 6]                      # пустое описание и пустое название
    ok_before = {i: p.summary for i, p in svc.plans(pid).items() if i not in (1, 4, 6)}
    calls = llm.count("adapt_chapter")
    state["broken"] = set()
    s = svc.replan_empty(pid, model)
    assert s.flagged == [] and svc.empty_chapters(pid) == [] and llm.count("adapt_chapter") == calls + 3
    assert {i: p.summary for i, p in svc.plans(pid).items() if i not in (1, 4, 6)} == ok_before
    calls = llm.count("adapt_chapter")
    s2 = svc.replan_empty(pid, model)                                # пустых нет — модель не вызывается
    assert s2.status == "completed" and llm.count("adapt_chapter") == calls and "Пустых глав нет" in s2.message


def test_chapters_with_only_a_flag_but_real_description_are_not_empty(db, sid, settings, model):
    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and "план главы 2 из" in prompt:
            d["summary"] = "Тихон снова встречается с Эльдаром и обсуждает ключ от башни."          # имена оригинала: flagged, но описание есть
        return d
    svc, _ = make(db, settings, handler)
    pid = svc.create(sid, AdaptParams(seed=1))
    assert svc.run(pid, model).flagged == [1] and svc.empty_chapters(pid) == []


def test_chapter_plan_prompt_asks_for_through_line_and_continuity(db, sid, settings, model):
    seen = []

    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.schemas import AdaptParams
    from bookmind.creation.service import AdaptationService
    llm = ScriptedLLM()
    orig = llm.chat
    llm.chat = lambda m, msgs, **k: (seen.append(msgs[-1]["content"]) or orig(m, msgs, **k))
    svc = AdaptationService(db, StructuredCaller(llm, settings), settings)
    pid = svc.create(sid, AdaptParams(divergence="medium", seed=3, chapter_words=300))
    svc.run(pid, model)
    plans = [p for p in seen if "Составь подробный план главы" in p]
    assert plans and all("СКВОЗНАЯ ЛИНИЯ" in p and "не возвращается без объяснения" in p for p in plans)
