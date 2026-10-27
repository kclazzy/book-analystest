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
_META_ANY = re.compile(r"как (?:языковая модель|ии\b|искусственный интеллект)|as an ai|language model|надеюсь, (?:эта|этот|вам|что)", re.IGNORECASE)
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


@dataclass
class Finding:
    severity: str            # error | warning
    code: str
    message: str
    chapter: int | None = None       # с 0; None — вся книга


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
                                        f"одно и то же предложение повторяется в главах {', '.join(str(c + 1) for c in sorted(chs))}: «{sample[k][:70]}…»"))

    grams: dict[str, set[int]] = defaultdict(set)
    for i, t in present.items():
        for g in _ngrams(t):
            grams[g].add(i)
    need = max(PHRASE_MIN_CHAPTERS, int(len(present) * PHRASE_MIN_SHARE + 0.999))
    rec = sorted(((len(c), g) for g, c in grams.items() if len(c) >= need), reverse=True)[:3]
    for n, g in rec:
        rep.findings.append(Finding("warning", "phrase_recurrence", f"оборот «{g}» встречается в {n} главах: возможно, модель повторяется"))

    starts = defaultdict(list)
    for i, t in present.items():
        starts[" ".join(x.casefold() for x in _WORD.findall(t)[:4])].append(i)
    for s, chs in starts.items():
        if len(chs) >= 3:
            rep.findings.append(Finding("warning", "same_start", f"главы {', '.join(str(c + 1) for c in sorted(chs))} начинаются одинаково: «{s}»"))
    rep.findings.sort(key=lambda f: (f.chapter is None, f.chapter or 0, f.severity != "error"))
    return rep
