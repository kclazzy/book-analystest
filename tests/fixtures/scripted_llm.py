"""Подменный клиент Ollama: возвращает заранее известные ответы по главам эталонного романа.
Позволяет проверять конвейер без модели и без сети. Можно внедрять сбои по номеру вызова."""
from __future__ import annotations

import json
import re
from typing import Any

from bookmind.domain.exceptions import OllamaUnavailableError


def ent(name, type_, desc, quote, aliases=()):
    return {"name": name, "type": type_, "aliases": list(aliases), "description": desc, "quote": quote}


def fact(entity, category, statement, quote, kind="explicit"):
    return {"entity": entity, "category": category, "statement": statement, "kind": kind, "quote": quote, "confidence": 0.9}


def ev(description, participants, quote, place="", mode="linear", causes="", consequences="", story_time=""):
    return {"description": description, "participants": [{"name": n, "role": r} for n, r in participants], "causes": causes,
            "consequences": consequences, "place": place, "story_time": story_time, "narrative_mode": mode, "confidence": 0.9,
            "quote": quote}


MIRA = ent("Мира Ветрова", "character", "рыжая ученица алхимика Аларика Вейна",
           "Мира Ветрова вернулась в лабораторию на рассвете", ["Мира"])
ALARIK = ent("Аларик Вейн", "character", "алхимик, наставник Миры", "Её наставник, алхимик Аларик Вейн", ["Аларик"])

# (название книги, название главы) -> ответ этапа core
CORE: dict[tuple[str, str], dict[str, Any]] = {}

B1 = "Огонь на Западной башне"
CORE[(B1, "Глава 1. Мёртвая лаборатория")] = {
    "summary": "Мира находит убитого наставника Аларика с серебряным ключом в руке и клянётся найти убийцу.",
    "entities": [MIRA, ALARIK,
                 ent("Серебряный ключ", "item", "серебряный ключ со знаком пламени",
                     "маленький серебряный ключ с вытравленным знаком пламени", ["ключ"]),
                 ent("Лаборатория", "place", "лаборатория Аларика", "вернулась в лабораторию")],
    "facts": [
        fact("Мира Ветрова", "appearance", "У Миры рыжие волосы", "Рыжие волосы выбились из-под платка"),
        fact("Аларик Вейн", "status", "Аларик Вейн убит: на шее красная полоса, будто его задушили шнуром",
             "На шее у него темнела тонкая красная полоса"),
        fact("Мира Ветрова", "desire", "Мира поклялась найти убийцу Аларика и заставить его заплатить",
             "Я найду того, кто это сделал, и заставлю его заплатить"),
        # заведомо неподтверждённый факт: цитаты нет в тексте
        fact("Мира Ветрова", "skill", "Мира владеет боевой магией", "Мира владела боевой магией огня"),
    ],
    "events": [ev("Мира находит мёртвого наставника Аларика Вейна в лаборатории с серебряным ключом в руке",
                  [("Мира Ветрова", "находит тело"), ("Аларик Вейн", "жертва")], "Аларик лежал на каменном полу между столами",
                  place="Лаборатория", consequences="Мира забирает ключ и клянётся найти убийцу")],
}
CORE[(B1, "Глава 2. Сестра")] = {
    "summary": "Мира сообщает больной сестре Лине о смерти Аларика; Лина напевает колыбельную о ключе и башне.",
    "entities": [ent("Лина", "character", "младшая сестра Миры, двенадцать лет, больна", "Лине было двенадцать лет")],
    "facts": [
        fact("Лина", "age", "Лине двенадцать лет", "Лине было двенадцать лет"),
        fact("Лина", "status", "Лина кашляет по ночам уже третью неделю", "уже третью неделю кашляла по ночам"),
        fact("Мира Ветрова", "action", "Мира спрятала ключ под половицей", "Мира спрятала ключ под половицей"),
    ],
    "events": [ev("Мира сообщает Лине о смерти Аларика и показывает серебряный ключ; Лина напевает колыбельную о башне и ключе",
                  [("Мира Ветрова", "рассказывает"), ("Лина", "слушает")], "Аларик умер, — тихо сказала Мира")],
}
CORE[(B1, "Глава 3. Гильдия")] = {
    "summary": "Мира приходит к главе Гильдии Эльдару Корну, скрывает ключ и замечает, что он не спросил о причине смерти.",
    "entities": [ent("Эльдар Корн", "character", "глава Гильдии алхимиков, бледные серые глаза",
                     "Эльдар Корн был высоким человеком с бледными серыми глазами", ["Эльдар"]),
                 ent("Гильдия алхимиков", "organization", "организация алхимиков", "Здание Гильдии алхимиков", ["Гильдия"])],
    "facts": [
        fact("Эльдар Корн", "appearance", "У Эльдара бледные серые глаза", "бледными серыми глазами и тихим голосом"),
        fact("Эльдар Корн", "status", "Эльдар Корн возглавляет Гильдию алхимиков", "потребовала встречи с главой Гильдии"),
        fact("Мира Ветрова", "action", "Мира солгала Эльдару, что Аларик ничего ей не передавал", "Мира покачала головой и соврала"),
        fact("Эльдар Корн", "knowledge", "Эльдар не спросил, как именно погиб Аларик", "Эльдар ни разу не спросил, как именно погиб Аларик"),
        fact("Эльдар Корн", "knowledge", "Эльдар, возможно, заранее знал об убийстве", "Он вёл себя так, словно уже знал ответ", "inference"),
    ],
    "events": [ev("Мира встречается с главой Гильдии Эльдаром Корном, скрывает от него ключ и начинает ему не доверять",
                  [("Мира Ветрова", "посетительница"), ("Эльдар Корн", "хозяин кабинета")],
                  "Аларик всегда любил оставлять загадки", place="Гильдия алхимиков")],
}
CORE[(B1, "Глава 4. Тихон")] = {
    "summary": "Мира рассказывает всё другу Тихону; он клянётся помочь и сообщает, что знак пламени есть на Западной башне.",
    "entities": [ent("Тихон Бреве", "character", "подмастерье кузнеца, друг детства Миры",
                     "Тихон Бреве был подмастерьем кузнеца", ["Тихон"]),
                 ent("Западная башня", "place", "старая заброшенная башня на скале у моря", "старой Западной башне", ["башня"])],
    "facts": [
        fact("Тихон Бреве", "appearance", "Тихон высокий, со шрамом на левой руке", "высоким и молчаливым парнем со шрамом на левой руке"),
        fact("Тихон Бреве", "action", "Тихон пообещал быть с Мирой до конца", "Я с тобой до конца"),
        fact("Западная башня", "knowledge", "Знак пламени встречается только на старой Западной башне", "встречался только на старой Западной башне"),
    ],
    "events": [ev("Мира рассказывает Тихону о смерти Аларика и ключе; Тихон обещает помощь",
                  [("Мира Ветрова", "рассказчица"), ("Тихон Бреве", "союзник")], "Я с тобой до конца")],
}
CORE[(B1, "Глава 5. Десять лет назад")] = {
    "summary": "Воспоминание: Аларик спас девятилетнюю Миру из пожара; уже тогда между ним и Эльдаром была трещина.",
    "entities": [ent("Огненный реестр", "item", "документ, из-за которого спорили Аларик и Эльдар",
                     "спорили из-за какого-то Огненного реестра")],
    "facts": [
        fact("Мира Ветрова", "biography", "Десять лет назад дом Миры сгорел, ей было девять лет", "Десять лет назад ей было девять"),
        fact("Аларик Вейн", "action", "Аларик вынес Миру из пожара и взял её в ученицы", "Эта девочка будет моей ученицей"),
        fact("Эльдар Корн", "biography", "Эльдар и Аларик вместе основали Гильдию", "основали Гильдию вместе"),
    ],
    "events": [ev("Аларик Вейн спасает девятилетнюю Миру из горящего дома и берёт её в ученицы",
                  [("Аларик Вейн", "спаситель"), ("Мира Ветрова", "спасённая"), ("Эльдар Корн", "свидетель")],
                  "рука подхватила её и вынесла на улицу", mode="flashback", story_time="десять лет назад")],
}
CORE[(B1, "Глава 6. Предательство")] = {
    "summary": "Мира выслеживает Тихона и видит, как он выдаёт Эльдару местонахождение ключа ради освобождения отца.",
    "entities": [],
    "facts": [
        fact("Тихон Бреве", "action", "Тихон выдал Эльдару, что ключ спрятан под половицей в доме Миры",
             "Он спрятан под половицей в её доме"),
        fact("Тихон Бреве", "desire", "Тихон предал Миру ради освобождения отца из долговой тюрьмы",
             "Твой отец скоро выйдет из долговой тюрьмы"),
        fact("Мира Ветрова", "action", "Мира перепрятала ключ", "Мира достала ключ из-под половицы и перепрятала его"),
    ],
    "events": [ev("Тихон тайно выдаёт Эльдару Корну, что серебряный ключ спрятан у Миры, и получает обещание освободить отца",
                  [("Тихон Бреве", "предатель"), ("Эльдар Корн", "заказчик"), ("Мира Ветрова", "свидетельница")],
                  "Ключ у неё", causes="отец Тихона в долговой тюрьме", consequences="дружба Миры и Тихона закончена")],
}
CORE[(B1, "Глава 7. Ультиматум")] = {
    "summary": "Эльдар даёт Мире три дня на ключ, угрожая забрать Лину; Мира откладывает месть и решает бежать.",
    "entities": [],
    "facts": [
        fact("Эльдар Корн", "action", "Эльдар дал Мире три дня, чтобы отдать ключ, иначе Лину заберут в приют Гильдии",
             "У вас три дня, чтобы отдать ключ"),
        fact("Эльдар Корн", "secret", "Эльдар не отрицал причастности к смерти Аларика", "Аларик сам выбрал свою судьбу", "inference"),
    ],
    "events": [
        ev("Эльдар Корн приходит к Мире и ставит ультиматум: ключ за три дня, иначе Лину заберут",
           [("Эльдар Корн", "угрожает"), ("Мира Ветрова", "получает ультиматум"), ("Лина", "заложница")],
           "У вас три дня, чтобы отдать ключ"),
        ev("Мира решает бежать из города вместе с Линой и ключом",
           [("Мира Ветрова", "решает"), ("Лина", "сопровождает")], "Мира решила бежать из города этой же ночью"),
    ],
}
CORE[(B1, "Глава 8. Погасший огонь")] = {
    "summary": "Мира и Лина достигают Западной башни, огонь на ней гаснет, но ключ не поворачивается в замке.",
    "entities": [],
    "facts": [fact("Западная башня", "status", "Огонь на башне горел с незапамятных времён и погас, когда подошли Мира и Лина",
                   "пламя вдруг дрогнуло и погасло")],
    "events": [ev("Мира и Лина приходят к Западной башне, огонь на ней гаснет, а ключ не поворачивается в замке железной двери",
                  [("Мира Ветрова", "пытается открыть"), ("Лина", "спутница")], "Ключ вошёл легко, но не повернулся",
                  place="Западная башня")],
}

B2 = "Пепел Западной башни"
CORE[(B2, "Глава 1. Подвал башни")] = {
    "summary": "Мира замечает составной знак на двери, ключ поворачивается; в подвале лежит Огненный реестр.",
    "entities": [ent("Мира Ветрова", "character", "рыжая ученица алхимика Аларика Вейна", "Мира наконец заметила", ["Мира"]),
                 ent("Эльдар Корн", "character", "глава Гильдии", "Эльдар Корн остановился перед запертой дверью", ["Эльдар"]),
                 ent("Западная башня", "place", "башня на скале", "Подвал башни"),
                 ent("Огненный реестр", "item", "свиток с надписью рукой Аларика", "«Огненный реестр»")],
    "facts": [
        fact("Эльдар Корн", "appearance", "У Эльдара тёмные карие глаза", "У него были тёмные карие глаза"),
        fact("Мира Ветрова", "action", "Мира нажала на верхний язык пламени на двери, и ключ повернулся", "Она надавила на верхний язык пламени"),
    ],
    "events": [ev("Мира открывает дверь башни, надавив на знак пламени, и спускается с Линой в подвал",
                  [("Мира Ветрова", "открывает"), ("Лина", "спутница")], "Теперь серебряный ключ повернулся без усилия",
                  place="Западная башня")],
}
CORE[(B2, "Глава 2. Признание Тихона")] = {
    "summary": "Тихон признаётся Мире, что действовал под давлением Эльдара; Мира не прощает, но принимает его помощь.",
    "entities": [ent("Тихон Бреве", "character", "бывший друг Миры, предавший её", "Тихон нашёл их на третий день", ["Тихон"])],
    "facts": [
        fact("Тихон Бреве", "biography", "Эльдар держал отца Тихона в тюрьме", "Эльдар держал моего отца в тюрьме"),
        fact("Эльдар Корн", "secret", "Эльдар убил Аларика и сам проговорился об этом Тихону", "Я не знал, что Эльдар убил Аларика"),
    ],
    "events": [ev("Тихон приходит к Мире, признаётся в предательстве и становится её союзником",
                  [("Тихон Бреве", "кающийся"), ("Мира Ветрова", "судья")], "Я не прощаю тебя")],
}
CORE[(B2, "Глава 3. Реестр")] = {
    "summary": "Огненный реестр доказывает вину Эльдара; загадка серебряного ключа раскрыта; Мира возвращается к мести.",
    "entities": [],
    "facts": [fact("Эльдар Корн", "secret", "Аларик обвинил Эльдара Корна в продаже запретных рецептов",
                   "Он обвинял Эльдара Корна в продаже запретных рецептов")],
    "events": [ev("Мира читает Огненный реестр и узнаёт, что Эльдар убил Аларика, чтобы заставить его замолчать",
                  [("Мира Ветрова", "читает"), ("Эльдар Корн", "обвиняемый")], "Он убил Аларика, чтобы заставить его замолчать")],
}

# Эталонные ответы второго прохода (deep): заполняются на этапе 6
DEEP: dict[tuple[str, str], dict[str, Any]] = {}
SUMMARIES: dict[tuple[str, str], dict[str, Any]] = {}


class ScriptedLLM:
    """Реализует интерфейс OllamaClient.chat. fail_plan: номер вызова -> исключение или сырая строка ответа."""

    def __init__(self, core=None, deep=None, summaries=None, extra=None):
        self.core, self.deep, self.summaries = core or CORE, deep if deep is not None else DEEP, summaries or SUMMARIES
        self.extra = extra or {}
        self.calls: list[tuple[str, str]] = []
        self.fail_plan: dict[int, Any] = {}
        self.prompts: list[str] = []

    @staticmethod
    def _field(prompt: str, name: str) -> str:
        m = re.search(rf"^{name}: (.+)$", prompt, re.MULTILINE)
        return m.group(1).strip() if m else ""

    def chat(self, model, messages, *, schema=None, options=None, think=False):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        n = len(self.calls) + 1
        book, chapter = self._field(prompt, "КНИГА"), self._field(prompt, "ГЛАВА")
        if "Верни JSON с полями: summary (2–4 предложения)" in prompt:
            stage, table = "core", self.core
        elif "Верни JSON с полями: goals" in prompt:
            stage, table = "deep", self.deep
        elif "Верни JSON: summary (3–6 предложений)" in prompt:
            stage, table = "chapter_summary", self.summaries
        elif "Верни JSON: summary (4–8 предложений" in prompt:
            stage, table = "arc_summary", {(book, chapter): self.extra.get("arc_default", {
                "summary": "Развитие арки.", "key_points": ["поворот"], "open_threads": []})}
        elif "Допустимые значения \"relation\"" in prompt:
            stage, table = "contradiction", self.extra.get("contradiction", {})
        elif "Верни JSON: status (open, partial, resolved)" in prompt:
            stage, table = "question_review", self.extra.get("question_review", {})
        elif "Верни JSON: answer" in prompt:
            stage, table = "answer", self.extra.get("answer", {})
        elif "Верни JSON: title, logline" in prompt:
            stage, table = "adapt_premise", {}
        elif "Верни JSON: members" in prompt:
            stage, table = "adapt_cast", {}
        elif "Верни JSON: title, summary (4–7" in prompt:
            stage, table = "adapt_chapter", {}
        elif "Верни только текст части" in prompt:
            stage, table = "write_part", {}
        else:
            stage, table = "unknown", {}
        self.calls.append((stage, chapter))
        plan = self.fail_plan.get(n)
        if isinstance(plan, Exception):
            raise plan
        if isinstance(plan, str):
            return plan
        if stage == "write_part":
            from tests.fixtures import adapt_script as A
            handler = self.extra.get("write_handler")
            return handler(prompt, A.prose) if handler else A.prose(prompt)
        if stage.startswith("adapt_"):
            from tests.fixtures import adapt_script as A
            default = {"adapt_premise": lambda p: A.premise(), "adapt_cast": A.cast, "adapt_chapter": A.chapter}[stage]
            handler = self.extra.get("adapt_handler")
            return json.dumps(handler(stage, prompt, default) if handler else default(prompt), ensure_ascii=False)
        if stage in ("contradiction", "question_review", "answer"):
            handler = table.get("handler") if isinstance(table, dict) else None
            return json.dumps(handler(prompt) if handler else table.get("default", {}), ensure_ascii=False)
        if stage == "arc_summary":
            return json.dumps(next(iter(table.values())), ensure_ascii=False)
        return json.dumps(table.get((book, chapter), {}), ensure_ascii=False)

    def count(self, stage: str) -> int:
        return sum(1 for s, _ in self.calls if s == stage)

    def cancel(self) -> None:  # совместимость с OllamaClient
        pass


def unavailable(msg: str = "Ollama недоступен") -> OllamaUnavailableError:
    return OllamaUnavailableError(msg)


# ---------------- эталон второго прохода (deep) и сводок ----------------
def goal(character, description, quote, *, status="active", goal_type="stated", motivation="", origin="", desired_result="",
         change_note="", allies="", opponents=""):
    return {"character": character, "description": description, "origin": origin, "motivation": motivation,
            "desired_result": desired_result, "obstacles": "", "methods": "", "allies": allies, "opponents": opponents,
            "goal_type": goal_type, "status": status, "change_note": change_note, "quote": quote}


def rel(a, b, rel_type, quote, *, state="", change_note="", description=""):
    return {"a": a, "b": b, "rel_type": rel_type, "state": state, "description": description, "change_note": change_note,
            "quote": quote}


def thread(title, quote, description="", status="open", events=()):
    return {"title": title, "description": description, "status": status, "events": list(events), "quote": quote}


def question(kind, text, quote, *, status="open", clues=(), hypotheses=(), resolution=""):
    return {"kind": kind, "text": text, "status": status, "clues": list(clues), "hypotheses": list(hypotheses),
            "resolution": resolution, "quote": quote}


def motif(name, kind, quote, description=""):
    return {"name": name, "kind": kind, "description": description, "quote": quote}


def D(goals=(), relationships=(), threads=(), questions=(), motifs=()):
    return {"goals": list(goals), "relationships": list(relationships), "plot_threads": list(threads),
            "questions": list(questions), "motifs": list(motifs)}


REVENGE = "Найти убийцу Аларика и заставить его заплатить"
PROTECT = "Защитить сестру Лину"
KEY_Q = "Какой замок открывает серебряный ключ со знаком пламени?"
KILLER_Q = "Кто убил Аларика Вейна?"
DEATH_THREAD = "Расследование смерти Аларика"
ELDAR_GOAL = "Получить от Миры то, что оставил Аларик"
TIKHON_GOAL = "Освободить отца из долговой тюрьмы"

DEEP[(B1, "Глава 1. Мёртвая лаборатория")] = D(
    goals=[goal("Мира Ветрова", REVENGE, "Я найду того, кто это сделал, и заставлю его заплатить",
                motivation="горе от потери наставника", origin="смерть Аларика", desired_result="убийца наказан")],
    threads=[thread(DEATH_THREAD, "Это не несчастный случай", "Мира выясняет, кто убил Аларика")],
    questions=[question("mystery", KILLER_Q, "Это не несчастный случай", clues=["красная полоса на шее"]),
               question("mystery", KEY_Q, "маленький серебряный ключ с вытравленным знаком пламени",
                        clues=["на ключе знак пламени"])],
    motifs=[motif("Пламя", "recurring_motif", "вытравленным знаком пламени", "знак пламени на ключе")],
)
DEEP[(B1, "Глава 2. Сестра")] = D(
    goals=[goal("Мира Ветрова", PROTECT, "Ради неё Мира была готова на многое", goal_type="demonstrated",
                motivation="Лина — единственный близкий человек")],
    relationships=[rel("Мира Ветрова", "Лина", "сестра", "Лина была единственным близким человеком", state="близкие")],
    questions=[question("prophecy", "Когда огонь на башне погаснет, ключ найдёт свой замок",
                        "Когда огонь на башне погаснет, ключ найдёт свой замок")],
)
DEEP[(B1, "Глава 3. Гильдия")] = D(
    goals=[goal("Эльдар Корн", ELDAR_GOAL, "Он не передавал вам ничего перед смертью", goal_type="demonstrated")],
    relationships=[rel("Мира Ветрова", "Эльдар Корн", "недоверие", "Недоверие к Эльдару Корну поселилось в ней, как заноза",
                       state="настороженность")],
    threads=[thread(DEATH_THREAD, "Мира впервые подумала, что убийца мог быть среди тех, кто носил гильдейский знак")],
    questions=[question("mystery", "Знал ли Эльдар об убийстве заранее?", "Он вёл себя так, словно уже знал ответ")],
)
DEEP[(B1, "Глава 4. Тихон")] = D(
    goals=[goal("Тихон Бреве", "Помочь Мире разобраться с ключом", "Я с тобой до конца")],
    relationships=[rel("Мира Ветрова", "Тихон Бреве", "друг", "Они знали друг друга с детства и никогда не лгали друг другу",
                       state="доверие")],
    questions=[question("mystery", KEY_Q, "встречался только на старой Западной башне",
                        clues=["знак пламени встречается на Западной башне"])],
)
DEEP[(B1, "Глава 5. Десять лет назад")] = D(
    goals=[goal("Аларик Вейн", "Взять Миру в ученицы", "Эта девочка будет моей ученицей", status="achieved")],
    relationships=[rel("Аларик Вейн", "Эльдар Корн", "соратник", "основали Гильдию вместе", state="дружба с трещиной"),
                   rel("Аларик Вейн", "Мира Ветрова", "наставник", "Эта девочка будет моей ученицей", state="опека")],
    questions=[question("secret", "Что такое Огненный реестр и почему из-за него спорили Аларик и Эльдар?",
                        "спорили из-за какого-то Огненного реестра")],
)
DEEP[(B1, "Глава 6. Предательство")] = D(
    goals=[goal("Тихон Бреве", TIKHON_GOAL, "Твой отец скоро выйдет из долговой тюрьмы", goal_type="demonstrated",
                motivation="отец в долговой тюрьме")],
    relationships=[rel("Мира Ветрова", "Тихон Бреве", "враг", "Теперь Тихон Бреве был её врагом", state="предательство",
                       change_note="Тихон выдал ключ Эльдару"),
                   rel("Тихон Бреве", "Эльдар Корн", "шантаж", "Ты поступил правильно, мальчик", state="зависимость")],
    threads=[thread("Предательство Тихона", "Ключ у неё", "Тихон выдал местонахождение ключа")],
)
DEEP[(B1, "Глава 7. Ультиматум")] = D(
    goals=[goal("Мира Ветрова", REVENGE, "Мысль о мести она отложила на потом", status="postponed",
                change_note="защита Лины важнее мести"),
           goal("Мира Ветрова", PROTECT, "Теперь для неё важнее всего было защитить Лину", status="active"),
           goal("Эльдар Корн", ELDAR_GOAL, "У вас три дня, чтобы отдать ключ", goal_type="stated",
                desired_result="получить серебряный ключ")],
    relationships=[rel("Мира Ветрова", "Эльдар Корн", "враг", "Вы убили Аларика", state="открытая вражда",
                       change_note="ультиматум Эльдара")],
    questions=[question("mystery", KILLER_Q, "Аларик сам выбрал свою судьбу", status="partial",
                        resolution="Эльдар не отрицает причастность")],
)
DEEP[(B1, "Глава 8. Погасший огонь")] = D(
    questions=[question("mystery", KEY_Q, "Ключ вошёл легко, но не повернулся",
                        clues=["ключ входит в замок железной двери башни, но не поворачивается"]),
               question("prophecy", "Когда огонь на башне погаснет, ключ найдёт свой замок", "пламя вдруг дрогнуло и погасло",
                        status="partial", resolution="Огонь погас, но замок не открылся")],
    motifs=[motif("Пламя", "recurring_motif", "пламя вдруг дрогнуло и погасло", "огонь на башне гаснет")],
)
DEEP[(B2, "Глава 1. Подвал башни")] = D(
    questions=[question("mystery", KEY_Q, "Теперь серебряный ключ повернулся без усилия", status="resolved",
                        resolution="Ключ открывает дверь в подвал Западной башни после нажатия на знак пламени")],
)
DEEP[(B2, "Глава 2. Признание Тихона")] = D(
    goals=[goal("Тихон Бреве", TIKHON_GOAL, "Теперь Тихон потерял и отца, потому что Эльдар не сдержал слово", status="failed",
                change_note="Эльдар не сдержал слово")],
    relationships=[rel("Мира Ветрова", "Тихон Бреве", "союзник", "они снова стали союзниками", state="недоверие, но союз",
                       change_note="Тихон признался")],
)
DEEP[(B2, "Глава 3. Реестр")] = D(
    goals=[goal("Мира Ветрова", REVENGE, "Месть, которую она отложила ради сестры, снова стала целью её жизни", status="active",
                change_note="Лина в безопасности"),
           goal("Мира Ветрова", PROTECT, "Лина теперь была в безопасности", status="achieved")],
    questions=[question("mystery", KILLER_Q, "Он убил Аларика, чтобы заставить его замолчать", status="resolved",
                        resolution="Аларика убил Эльдар Корн, чтобы заставить его замолчать"),
               question("secret", "Что такое Огненный реестр и почему из-за него спорили Аларик и Эльдар?",
                        "В свитке перечислялись тайные сделки Гильдии", status="resolved",
                        resolution="Реестр содержит тайные сделки Гильдии и доказательства вины Эльдара")],
)

for (_b, _ch), _resp in CORE.items():
    SUMMARIES[(_b, _ch)] = {"summary": f"Сводка главы: {_resp['summary']}",
                            "key_points": [e["description"] for e in _resp["events"]][:3], "open_threads": []}


def _noop(self) -> None:
    pass


ScriptedLLM.reset_cancel = _noop
ScriptedLLM.close = _noop
