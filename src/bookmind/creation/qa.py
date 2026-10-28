"""Проверка написанной книги без модели: ошибки генерации (обрыв текста, иероглифы, служебные фразы, зацикливание),
соответствие плану и героям, повторы между главами. Ошибки (error) стоит исправить, предупреждения (warning) — посмотреть."""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from bookmind.creation.originality import content_stems
from bookmind.creation.schemas import Cast, ChapterPlan
from bookmind.services.search import STOPWORDS, stem

_WORD = re.compile(r"\w+", re.UNICODE)
_FOREIGN = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯؀-ۿ฀-๿]")
_META_ANY = re.compile(r"как (?:языковая модель|ии\b|искусственный интеллект)|as an ai|language model|надеюсь, (?:эта|этот|вам|что)"
                       r"|(?:последн\w+|финальн\w+|заключительн\w+)\s+кадр", re.IGNORECASE)
_LATIN = re.compile(r"(?=\w*[А-Яа-яЁё])(?=\w*[A-Za-z])\w+|\b[A-Za-z]{3,}\b")
_SERVICE = re.compile(r"^\s*[*_#\s]*(?:место действия|точка зрения|время действия|цель героя|конфликт|итог|кадр\s*\d*|сцена\s*\d*|локация|пов)\s*[:—–]", re.IGNORECASE)
_FIRST = re.compile(r"\b(?:я|мне|меня|мной|мы|нас|нам|нами|мой|моя|моё|мои)\b", re.IGNORECASE)
_CLICHE = re.compile(r"вместе они|новую? (?:эр[уы]|жизн|глав)|начало нового|всё только начинается|впереди (?:их |его |её )?ждал|"
                     r"светл\w+ будущ|ничто не (?:могло )?остановит|гармони[июя]|навсегда изменил|надежд\w+ на (?:лучшее|будущее)|"
                     r"солнце (?:вставало|восходило)|рассвет нового", re.IGNORECASE)
_META_START = re.compile(r"^\s*(?:конечно|хорошо|разумеется|вот)[,!:.]\s", re.IGNORECASE)
_NAME = re.compile(r"(?<![\w])[А-ЯЁ][а-яё]{3,}")
_END_OK = ".!?…»\"”)]*"
_SENT = re.compile(r"(?<=[.!?…])\s+")

COVERAGE_MIN = 0.25         # доля содержательных слов плана, которая должна встретиться в тексте главы
TOO_LONG = 2.5
NAME_REPEATS = 3
LOOP_REPEATS = 3
PHRASE_N = 6
PHRASE_MIN_CHAPTERS = 4
PHRASE_MIN_SHARE = 0.4
UNEVEN_LOW, UNEVEN_HIGH = 0.5, 2.0
DUP_SENTENCE_MIN_CHARS = 60
INNER_DUP_MIN_CHARS = 40     # повтор предложения внутри одной главы
INNER_DUP_JACCARD = 0.85     # почти одинаковые предложения
FIRST_PERSON_MIN = 3         # сколько местоимений первого лица вне диалогов делает главу «от первого лица»
FIRST_PERSON_MAX_SHARE = 0.5 # если от первого лица больше половины глав, это стиль книги, а не ошибка
CLICHE_BOOK_SHARE = 0.4      # если столько глав кончаются штампом, замечание одно на всю книгу
OVERUSE_PER_1000 = 2.5       # слово чаще, чем столько раз на 1000 слов
OVERUSE_MIN_SHARE = 0.4      # и встречается минимум в такой доле глав
OVERUSE_TOP = 3
OVERUSE_MIN_COUNT = 8
COMMON_WORDS = {"вокруг", "только", "теперь", "словно", "будто", "потом", "между", "через", "всегда", "иногда", "внутри", "снова"}
OVERUSE_MIN_CHAPTERS = 4     # в совсем короткой книге слова-паразиты не ищем
OVERUSE_CHAPTERS = 8         # сколько самых «насыщенных» глав передавать на исправление


@dataclass
class Finding:
    severity: str            # error | warning
    code: str
    message: str
    chapter: int | None = None       # с 0; None — вся книга
    related: list[int] = field(default_factory=list)   # для повторов между главами: все затронутые главы (с 0)


@dataclass
class BookReport:
    findings: list[Finding] = field(default_factory=list)
    chapters_checked: int = 0
    chapters_total: int = 0

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "errors": len(self.errors), "warnings": len(self.warnings), "chapters_checked": self.chapters_checked,
            "chapters_total": self.chapters_total, "findings": [asdict(f) for f in self.findings],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BookReport:
        return cls([Finding(**f) for f in d.get("findings", [])], d.get("chapters_checked", 0), d.get("chapters_total", 0))

    def to_markdown(self, titles: dict[int, str] | None = None) -> str:
        titles = titles or {}
        head = (f"Проверено глав: {self.chapters_checked} из {self.chapters_total}. "
                f"Всего ошибок: {len(self.errors)}, предупреждений: {len(self.warnings)}.")
        L = ["# Проверка книги", "", head, ""]
        if not self.findings:
            return "\n".join(L + ["Замечаний нет."]) + "\n"
        mark = {"error": "❌", "warning": "⚠"}
        book = [f for f in self.findings if f.chapter is None]
        if book:
            L += ["## По всей книге", *[f"- {mark[f.severity]} {f.message}" for f in book], ""]
        for i in sorted({f.chapter for f in self.findings if f.chapter is not None}):
            L += [f"## Глава {i + 1}" + (f". {titles[i]}" if titles.get(i) else ""),
                  *[f"- {mark[f.severity]} {f.message}" for f in self.findings if f.chapter == i], ""]
        return "\n".join(L).rstrip() + "\n"


def _stems(s: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(s) if len(w) >= 3}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(re.sub(r"\s+", " ", text)) if s.strip()]


def _norm(s: str) -> str:
    return " ".join(w.casefold().replace("ё", "е") for w in _WORD.findall(s))


def _inner_duplicates(text: str) -> list[str]:
    """Предложения, которые в главе встречаются дважды и больше (дословно или почти дословно)."""
    seen: list[tuple[str, set[str]]] = []
    dups: list[str] = []
    for sent in _sentences(text):
        if len(sent) < INNER_DUP_MIN_CHARS:
            continue
        k = _norm(sent)
        ws = set(k.split())
        if any(k == o or len(ws & w) / max(1, len(ws | w)) >= INNER_DUP_JACCARD for o, w in seen):
            dups.append(sent)
        else:
            seen.append((k, ws))
    return dups


def dedupe_text(text: str) -> str:
    """Убирает повторы внутри главы: абзацы и предложения, которые уже были выше (остаётся первое вхождение)."""
    paras_out: list[str] = []
    seen_p: set[str] = set()
    seen_s: list[tuple[str, set[str]]] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        kp = _norm(para)
        if not kp or (len(para) >= INNER_DUP_MIN_CHARS and kp in seen_p):
            continue
        seen_p.add(kp)
        kept = []
        for sent in _sentences(para):
            k = _norm(sent)
            ws = set(k.split())
            if len(sent) >= INNER_DUP_MIN_CHARS and any(k == o or len(ws & w) / max(1, len(ws | w)) >= INNER_DUP_JACCARD for o, w in seen_s):
                continue
            seen_s.append((k, ws))
            kept.append(sent)
        if kept:
            paras_out.append(" ".join(kept) if len(kept) != len(_sentences(para)) else para.strip())
    return "\n\n".join(paras_out)


def strip_service_lines(text: str) -> str:
    """Убирает строки-заголовки вида «Место действия: …», «Кадр 3: …»."""
    return "\n".join(line for line in text.split("\n") if not _SERVICE.match(line))


def _narration(text: str) -> str:
    """Текст без прямой речи: «ёлочки» и всё от тире-реплики до конца абзаца."""
    t = re.sub(r"«[^»]*»", " ", text)
    return "\n\n".join(re.sub(r"(?:(?<=[.!?…:])|^)\s*[—–].*$", " ", para, flags=re.S) for para in re.split(r"\n\s*\n|\n", t))


def _first_person(text: str) -> int:
    return len(_FIRST.findall(_narration(text)))


def check_chapter(text: str, plan: ChapterPlan | None, cast: Cast, target_words: int | None = None) -> list[Finding]:
    """Проверки одной главы. Ошибки генерации — error, расхождения с планом — warning."""
    out: list[Finding] = []
    text = text.strip()

    def add(sev: str, code: str, msg: str) -> None:
        out.append(Finding(sev, code, msg))

    if _FOREIGN.search(text):
        add("error", "foreign_script", "в тексте есть иероглифы или буквы другого письма: " + "".join(sorted(set(_FOREIGN.findall(text))))[:20])
    if text and text[-1] not in _END_OK:
        add("error", "truncated", "текст оборван на середине предложения")
    if _META_ANY.search(text) or _META_START.search(text):
        add("error", "meta", "в тексте служебные фразы модели («Конечно», «как языковая модель» и т. п.)")
    counts = Counter(_norm(s) for s in _sentences(text) if len(s) >= 25)
    loops = [s for s, n in counts.items() if n >= LOOP_REPEATS]
    if loops:
        add("error", "loop", f"зацикливание: одно предложение повторено {counts[loops[0]]} раза(ов): «{loops[0][:60]}…»")
    inner = _inner_duplicates(text)
    if inner:
        add("warning", "dup_sentence", f"в главе повторяются предложения ({len(inner)}): «{inner[0][:60]}…»")
    if any(_SERVICE.match(line) for line in text.split("\n")):
        add("error", "service_header", "в тексте служебные строки («Место действия:», «Кадр 1:» и т. п.)")
    allowed = set()
    for m in cast.members:
        allowed |= {w.casefold() for w in _WORD.findall(m.name)}
    if plan is not None:
        allowed |= {w.casefold() for w in _WORD.findall(f"{plan.pov} {plan.location} {' '.join(plan.characters)}")}
    lat = sorted({w for w in (m.group() for m in _LATIN.finditer(text)) if w.casefold() not in allowed})
    if lat:
        add("error", "latin", "латинские буквы в русском тексте: " + ", ".join(lat[:5]))
    last = _sentences(text)[-1:] or [""]
    if _CLICHE.search(last[0]):
        add("warning", "cliche_ending", f"штампованная концовка главы: «{last[0][:70]}»")
    if text.count("«") != text.count("»"):
        add("warning", "quotes", "не сходятся кавычки «»")
    words = len(_WORD.findall(text))
    if target_words and words > target_words * TOO_LONG:
        add("warning", "too_long", f"глава слишком длинная: {words} слов при ориентире {target_words}")
    if plan is not None and text:
        tstems = _stems(text)
        if plan.pov.strip() and not (_stems(plan.pov) & tstems):
            add("warning", "pov_missing", f"в тексте не упомянут герой точки зрения «{plan.pov}»")
        want = content_stems(plan.summary)
        if len(want) >= 5 and len(want & content_stems(text)) / len(want) < COVERAGE_MIN:
            add("warning", "plan_coverage", "в тексте почти нет событий из плана главы")
    known = set()
    for m in cast.members:
        known |= _stems(m.name)
    if plan is not None:
        known |= _stems(f"{plan.pov} {plan.location} {' '.join(plan.characters)}")
    names: Counter[str] = Counter()
    shown: dict[str, str] = {}
    for m in _NAME.finditer(text):
        before = text[:m.start()].rstrip()
        if not before or before[-1] in ".!?…—–-:\n«\"(":
            continue                                            # начало предложения: заглавная буква ни о чём не говорит
        st = stem(m.group())
        if st not in known:
            names[st] += 1
            shown.setdefault(st, m.group())
    odd = [shown[s] for s, n in names.most_common() if n >= NAME_REPEATS][:5]
    if odd:
        add("warning", "unknown_names", "возможные имена вне списка героев: " + ", ".join(odd))
    return out


def _ngrams(text: str) -> set[str]:
    w = [x.casefold().replace("ё", "е") for x in _WORD.findall(text)]
    out = set()
    for i in range(len(w) - PHRASE_N + 1):
        gram = w[i:i + PHRASE_N]
        if sum(1 for x in gram if len(x) >= 4 and x not in STOPWORDS and not x.isdigit()) >= 3 and not any(x.isdigit() for x in gram):
            out.add(" ".join(gram))
    return out


def check_book(texts: dict[int, str], plans: dict[int, ChapterPlan], cast: Cast, n_chapters: int,
               target_words: int | None = None) -> BookReport:
    rep = BookReport(chapters_total=n_chapters)
    present = {i: t.strip() for i, t in texts.items() if t and t.strip() and i < n_chapters}
    rep.chapters_checked = len(present)
    words = {i: len(_WORD.findall(t)) for i, t in present.items()}
    median = statistics.median(words.values()) if words else 0
    for i in range(n_chapters):
        if i not in present:
            rep.findings.append(Finding("error", "missing_text", "текст главы не написан", i))
            continue
        for f in check_chapter(present[i], plans.get(i), cast, target_words=target_words or None):
            f.chapter = i
            rep.findings.append(f)

    whole = " ".join(present.values())
    stems_all = _stems(whole)
    for m in cast.members:
        if m.name.strip() and not (_stems(m.name) & stems_all) and present:
            rep.findings.append(Finding("warning", "cast_absent", f"герой «{m.name}» ни разу не появляется в тексте"))

    if len(words) >= 3 and median:
        for i, n in sorted(words.items()):
            if n < median * UNEVEN_LOW or n > median * UNEVEN_HIGH:
                rep.findings.append(Finding("warning", "uneven_length", f"длина главы ({n} слов) сильно отличается от обычной ({int(median)})", i))

    seen: dict[str, set[int]] = defaultdict(set)
    sample: dict[str, str] = {}
    for i, t in present.items():
        for s in _sentences(t):
            if len(s) >= DUP_SENTENCE_MIN_CHARS:
                k = _norm(s)
                seen[k].add(i)
                sample.setdefault(k, s)
    for k, chs in seen.items():
        if len(chs) >= 2:
            rep.findings.append(Finding("warning", "cross_duplicate",
                                        f"одно и то же предложение повторяется в главах {', '.join(str(c + 1) for c in sorted(chs))}: «{sample[k][:70]}…»",
                                        related=sorted(chs)))

    grams: dict[str, set[int]] = defaultdict(set)
    for i, t in present.items():
        for g in _ngrams(t):
            grams[g].add(i)
    need = max(PHRASE_MIN_CHAPTERS, int(len(present) * PHRASE_MIN_SHARE + 0.999))
    rec = sorted(((len(c), g) for g, c in grams.items() if len(c) >= need), reverse=True)[:3]
    for n, g in rec:
        rep.findings.append(Finding("warning", "phrase_recurrence", f"оборот «{g}» встречается в {n} главах: возможно, модель повторяется", related=sorted(grams[g])))

    starts = defaultdict(list)
    for i, t in present.items():
        starts[" ".join(x.casefold() for x in _WORD.findall(t)[:4])].append(i)
    for s, chs in starts.items():
        if len(chs) >= 3:
            rep.findings.append(Finding("warning", "same_start", f"главы {', '.join(str(c + 1) for c in sorted(chs))} начинаются одинаково: «{s}»", related=sorted(chs)))
    _book_style_checks(rep, present)
    rep.findings.sort(key=lambda f: (f.chapter is None, f.chapter or 0, f.severity != "error"))
    return rep


def _book_style_checks(rep: BookReport, present: dict[int, str]) -> None:
    """Проверки, где важно сравнение глав: лицо повествования, штампы в концовках, слова-паразиты."""
    if not present:
        return
    fp = sorted(i for i, t in present.items() if _first_person(t) >= FIRST_PERSON_MIN)
    if fp and len(fp) / len(present) <= FIRST_PERSON_MAX_SHARE:
        for i in fp:
            rep.findings.append(Finding("warning", "first_person", "рассказ ведётся от первого лица («я», «мы»), хотя книга написана от третьего", i))
    cl = sorted(f.chapter for f in rep.findings if f.code == "cliche_ending" and f.chapter is not None)
    if cl and len(cl) / len(present) >= CLICHE_BOOK_SHARE:
        rep.findings[:] = [f for f in rep.findings if f.code != "cliche_ending"]
        rep.findings.append(Finding("warning", "cliche_ending",
                                    f"{len(cl)} глав из {len(present)} заканчиваются похожим штампом («вместе», «новая эра», «будущее»): главы {', '.join(str(c + 1) for c in cl[:12])}",
                                    related=cl))
    total = sum(len(_WORD.findall(t)) for t in present.values())
    per_ch: dict[str, Counter[int]] = defaultdict(Counter)
    shown: dict[str, str] = {}
    for i, t in present.items():
        for w in _WORD.findall(t):
            if len(w) >= 6 and w.casefold() not in STOPWORDS and w.casefold() not in COMMON_WORDS and not w[0].isupper() and not w.isdigit():
                st = stem(w)
                per_ch[st][i] += 1
                shown.setdefault(st, w.casefold())
    over = []
    for st, c in per_ch.items():
        n = sum(c.values())
        if total and len(present) >= OVERUSE_MIN_CHAPTERS and n >= OVERUSE_MIN_COUNT and n * 1000 / total >= OVERUSE_PER_1000 and len(c) / len(present) >= OVERUSE_MIN_SHARE:
            over.append((n, st))
    for n, st in sorted(over, reverse=True)[:OVERUSE_TOP]:
        chs = [i for i, _ in per_ch[st].most_common(OVERUSE_CHAPTERS)]
        rep.findings.append(Finding("warning", "word_overuse", f"слово «{shown[st]}» (и однокоренные) встречается {n} раз — {n * 1000 // total} на 1000 слов",
                                    related=sorted(chs)))
