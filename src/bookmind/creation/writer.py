"""Написание текста глав по готовому плану. Глава пишется частями; прогресс сохраняется после каждой части, запуск возобновляем.
После написания текст проверяется: длина, язык, повторы, имена и дословные совпадения с оригиналом."""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

from bookmind.creation import illustrations
from bookmind.creation.book_export import AUTHOR, Book, BookChapter
from bookmind.creation import qa
from bookmind.creation.originality import Check
from bookmind.creation.schemas import ChapterPlan
from bookmind.creation.service import TRANSIENT, AdaptationService
from bookmind.domain.exceptions import BookMindError, CancelledError, OllamaUnavailableError

SYSTEM_PROSE = ("Ты — талантливый писатель. Пиши художественную прозу на русском языке. "
                "Выводи только текст без заголовков, пояснений, списков и разметки.")
WORDS_PER_PART = 500
MIN_FILL = 0.5            # доля от заданной длины, ниже которой текст считается слишком коротким
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class WriteProgress:
    project_id: str
    chapter: int
    total: int
    part: int
    parts: int
    message: str = ""


@dataclass
class WriteSummary:
    project_id: str
    status: str             # completed | paused | failed
    written: int
    flagged: list[int]
    message: str = ""
    fixed: list[int] = field(default_factory=list)      # главы, переписанные по итогам проверки книги


@dataclass
class FixSummary:
    fixed: list[int]            # главы, текст которых заменён более удачным
    rounds: int
    errors: int                 # ошибок в отчёте после исправления
    warnings: int
    message: str = ""


ProgressCb = Callable[[WriteProgress], None]


def clean_prose(text: str) -> str:
    """Убирает разметку и служебные строки, которые модели любят добавлять."""
    t = text.replace("\r\n", "\n").strip()
    t = re.sub(r"^```\w*\n|\n```$", "", t)
    out = []
    for line in t.split("\n"):
        s = line.strip()
        if re.match(r"^(#{1,6}\s|[-*_]{3,}$)", s) or re.match(r"^\**\s*(часть|глава)\s*\d+", s, re.I) and len(s) < 60:
            continue
        if re.match(r"^\(?(конец|продолжение следует)", s, re.I) and len(s) < 60:
            continue
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def validate_prose(text: str, target_words: int) -> list[str]:
    issues = []
    n = count_words(text)
    if n < target_words * MIN_FILL:
        issues.append(f"текст слишком короткий: {n} слов при ориентире {target_words}")
    letters = [c for c in text if c.isalpha()]
    if letters and sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters) / len(letters) < 0.8:
        issues.append("текст написан не по-русски")
    paras = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 80]
    if len(paras) != len(set(paras)):
        issues.append("в тексте есть повторяющиеся абзацы")
    return issues


class BookWriter:
    def __init__(self, svc: AdaptationService):
        self.svc = svc
        self.db = svc.db

    # ---------- состояние ----------
    def _rows(self, pid: str) -> dict[int, dict]:
        return {r["idx"]: dict(r) for r in self.db.query("SELECT * FROM adaptation_chapters WHERE project_id = ?", (pid,))}

    def chapter_text(self, pid: str, idx: int) -> str:
        r = self.db.one("SELECT text FROM adaptation_chapters WHERE project_id = ? AND idx = ?", (pid, idx))
        return r["text"] if r else ""

    def text_status(self, pid: str) -> dict[int, str]:
        return {i: r["text_status"] for i, r in self._rows(pid).items()}

    def book(self, pid: str, only_written: bool = True) -> Book:
        pr = self.svc.premise(pid)
        rep = self.svc.report(pid)
        rows = self._rows(pid)
        params = self.svc.params(pid)
        art = params.illustrations and illustrations.available()
        total = max(1, len(rep["chapters"]) - 1)
        chapters = []
        for c in rep["chapters"]:
            r = rows.get(c["idx"], {})
            if only_written and r.get("text_status") not in ("ok", "flagged"):
                continue
            pic = illustrations.chapter_art(c["idx"] + 1, c["idx"] / total) if art else None
            chapters.append(BookChapter(c["title"], r.get("text", ""), c["idx"] + 1, image=pic))
        title = pr.title if pr and pr.title else rep["title"]
        author = params.author.strip() or AUTHOR
        cover = illustrations.cover_art(title, author, seed=len(title)) if art else None
        return Book(title=title, chapters=chapters, annotation=pr.logline if pr else "", author=author, cover=cover)

    # ---------- запуск ----------
    def write(self, pid: str, model: str, chapters: list[int] | None = None, rewrite: bool = False,
              on_progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> WriteSummary:
        """Пишет тексты глав. chapters=None — все главы, у которых ещё нет текста; rewrite=True переписывает указанные заново."""
        cancel = cancel or threading.Event()
        params = self.svc.params(pid)
        plans = self.svc.plans(pid)
        if not plans:
            raise BookMindError("Сначала нужно составить план глав")
        row = self.svc._row(pid)
        if params.pause_after_plan and not params.approved and row["status"] == "awaiting_review":
            raise BookMindError("Сначала подтвердите план глав")
        total = len(plans)
        targets = sorted(chapters) if chapters is not None else sorted(plans)
        rows = self._rows(pid)
        if rewrite:
            for i in targets:
                self.db.execute("UPDATE adaptation_chapters SET text='', text_status='none', text_parts=0, text_attempts=0, text_check_json='{}' "
                                "WHERE project_id=? AND idx=?", (pid, i))
            rows = self._rows(pid)
        self.svc._save(pid, stage="writing", status="running", model=model, error=None)
        written = 0
        try:
            for i in targets:
                if i not in plans:
                    continue
                if rows.get(i, {}).get("text_status") in ("ok", "flagged"):
                    continue
                self._write_chapter(pid, i, model, params, plans, total, on_progress, cancel)
                written += 1
        except CancelledError:
            self.svc._save(pid, status="paused")
            return self._summary(pid, "paused", written, "Остановлено; написанное сохранено")
        except OllamaUnavailableError as exc:
            self.svc._save(pid, status="paused", error=str(exc))
            return self._summary(pid, "paused", written, f"Ollama недоступен: {exc}")
        except TRANSIENT as exc:
            self.svc._save(pid, status="failed", error=str(exc))
            return self._summary(pid, "failed", written, f"Модель не вернула текст: {exc}")
        left = [i for i in plans if self._rows(pid).get(i, {}).get("text_status") not in ("ok", "flagged")]
        self.svc._save(pid, status="completed", stage="written" if not left else "writing")
        if written:
            self.check_book(pid)
        summary = self._summary(pid, "completed", written, "")
        if written and params.auto_fix and not cancel.is_set():
            fx = self.fix_book(pid, model, on_progress=on_progress, cancel=cancel, chapters=chapters if chapters is not None else None)
            summary = self._summary(pid, "completed", written, fx.message)
            summary.fixed = fx.fixed
        return summary

    # ---------- исправление по итогам проверки ----------
    @staticmethod
    def _fix_targets(rep: qa.BookReport, only: set[int] | None) -> dict[int, list[str]]:
        t: dict[int, list[str]] = {}
        for f in rep.findings:
            if f.code in ("missing_text", "cast_absent"):
                continue                                    # эти замечания правятся написанием глав / планом, а не переписыванием
            chs = [f.chapter] if f.chapter is not None else f.related[1:]   # из повторяющихся глав первая остаётся как есть
            for c in chs:
                if only is None or c in only:
                    t.setdefault(c, []).append(f.message)
        return t

    def _chapter_findings(self, pid: str, idx: int, text: str, plans, params) -> int:
        cast = self.svc.cast(pid) or qa.Cast(members=[])
        found = qa.check_chapter(text, plans.get(idx), cast, params.chapter_words)
        return sum(10 if f.severity == "error" else 1 for f in found)

    def fix_book(self, pid: str, model: str, rounds: int | None = None, chapters: list[int] | None = None,
                 on_progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> FixSummary:
        """Проверяет книгу и переписывает главы с замечаниями, передавая замечания модели. Новый текст принимается,
        только если в нём замечаний не больше, чем было; иначе остаётся прежний."""
        cancel = cancel or threading.Event()
        params, plans = self.svc.params(pid), self.svc.plans(pid)
        rounds = params.fix_rounds if rounds is None else rounds
        only = set(chapters) if chapters is not None else None
        fixed: set[int] = set()
        done = 0
        msg = ""
        try:
            for done in range(1, rounds + 1):
                rep = self.check_book(pid)
                targets = self._fix_targets(rep, only)
                if not targets:
                    done -= 1
                    break
                progress = False
                for idx, msgs in sorted(targets.items()):
                    if cancel.is_set():
                        raise CancelledError("Остановлено пользователем")
                    old = self._rows(pid)[idx]
                    old_score = self._chapter_findings(pid, idx, old["text"], plans, params)
                    self.db.execute("UPDATE adaptation_chapters SET text='', text_status='none', text_parts=0, text_attempts=0, text_check_json='{}' "
                                    "WHERE project_id=? AND idx=?", (pid, idx))
                    self._write_chapter(pid, idx, model, params, plans, len(plans), on_progress, cancel,
                                        feedback=["исправь в главе: " + m for m in msgs])
                    new = self._rows(pid)[idx]
                    if self._chapter_findings(pid, idx, new["text"], plans, params) <= old_score and new["text"].strip():
                        fixed.add(idx)
                        progress = True
                    else:
                        self.db.execute("UPDATE adaptation_chapters SET text=?, text_status=?, text_parts=?, text_attempts=?, text_check_json=? "
                                        "WHERE project_id=? AND idx=?", (old["text"], old["text_status"], old["text_parts"], old["text_attempts"],
                                                                         old["text_check_json"], pid, idx))
                if not progress:
                    break
        except CancelledError:
            msg = "Исправление остановлено; написанное сохранено"
        except OllamaUnavailableError as exc:
            msg = f"Исправление прервано: Ollama недоступен: {exc}"
        except TRANSIENT as exc:
            msg = f"Исправление прервано: модель не вернула текст: {exc}"
        rep = self.check_book(pid)
        return FixSummary(sorted(fixed), done, len(rep.errors), len(rep.warnings), msg)

    # ---------- проверка книги ----------
    def check_book(self, pid: str) -> qa.BookReport:
        """Проверяет всю книгу целиком (без модели) и сохраняет отчёт."""
        params, plans = self.svc.params(pid), self.svc.plans(pid)
        cast = self.svc.cast(pid)
        texts = {i: r.get("text", "") for i, r in self._rows(pid).items() if r.get("text_status") in ("ok", "flagged")}
        rep = qa.check_book(texts, plans, cast if cast else qa.Cast(members=[]), n_chapters=len(plans), target_words=params.chapter_words)
        self.svc._save(pid, qa_json=json.dumps(rep.as_dict(), ensure_ascii=False))
        return rep

    def book_report(self, pid: str) -> qa.BookReport | None:
        d = json.loads(self.svc._row(pid)["qa_json"] or "{}")
        if d:
            return qa.BookReport.from_dict(d)
        if any(st in ("ok", "flagged") for st in self.text_status(pid).values()):
            return self.check_book(pid)             # книга написана до появления проверки
        return None

    def _summary(self, pid: str, status: str, written: int, msg: str) -> WriteSummary:
        flagged = sorted(i for i, r in self._rows(pid).items() if r["text_status"] == "flagged")
        return WriteSummary(pid, status, written, flagged, msg)

    # ---------- глава ----------
    def _parts_for(self, words: int) -> int:
        return max(1, round(words / WORDS_PER_PART))

    def _write_chapter(self, pid, idx, model, params, plans, total, on_progress, cancel, feedback: list[str] | None = None) -> None:
        row = self._rows(pid).get(idx, {})
        parts = self._parts_for(params.chapter_words)
        words_part = max(120, params.chapter_words // parts)
        attempts = row.get("text_attempts", 0)
        if row.get("text_status") == "partial":
            text, done = row.get("text", ""), row.get("text_parts", 0)
        else:
            text, done = "", 0
        fb: list[str] = list(feedback or [])
        max_attempts = max(1, params.max_replans + 1)
        while True:
            if done == 0:
                attempts += 1
            for p in range(done, parts):
                if cancel.is_set():
                    raise CancelledError("Остановлено пользователем")
                if on_progress:
                    on_progress(WriteProgress(pid, idx, total, p, parts, f"глава {idx + 1} из {total}, часть {p + 1} из {parts}"))
                piece = self._write_part(pid, idx, p, parts, words_part, text, model, plans, fb)
                text = (text + "\n\n" + piece).strip()
                self._store(pid, idx, text, "partial", p + 1, attempts, None, [])
            issues, chk = self._check(pid, text, params)
            if not issues or attempts >= max_attempts:
                self._store(pid, idx, text, "flagged" if issues else "ok", parts, attempts, chk, issues)
                return
            fb, text, done = issues, "", 0

    def _check(self, pid: str, text: str, params) -> tuple[list[str], Check]:
        chk = self.svc._corpus(pid).check(text, params.divergence, with_shingles=True)
        critical = [f.message for f in qa.check_chapter(text, None, qa.Cast(members=[]), None) if f.severity == "error"]
        return validate_prose(text, params.chapter_words) + critical + chk.reasons, chk

    def _store(self, pid, idx, text, status, parts, attempts, chk: Check | None, issues: list[str]) -> None:
        self.db.execute(
            "UPDATE adaptation_chapters SET text=?, text_status=?, text_parts=?, text_attempts=?, text_check_json=? WHERE project_id=? AND idx=?",
            (text, status, parts, attempts, json.dumps({"check": chk.as_dict() if chk else {}, "issues": issues}, ensure_ascii=False), pid, idx))

    def _write_part(self, pid, idx, part, parts, words, text_so_far, model, plans, feedback) -> str:
        pr, cast, plan = self.svc.premise(pid), self.svc.cast(pid), plans[idx]
        if parts == 1:
            task = "Напиши всю главу целиком: начни со сцены, развей конфликт и закончи итогом и последним кадром."
        elif part == 0:
            task = "Начало главы: введи место, точку зрения и цель героя, затяни конфликт. Главу не заканчивай."
        elif part == parts - 1:
            task = "Финал главы: доведи конфликт до итога и закончи последним кадром главы."
        else:
            task = "Середина главы: развивай конфликт, добавь препятствия, диалоги и подсказки. Главу не заканчивай."
        fb = ("ЗАМЕЧАНИЯ К ПРЕДЫДУЩЕЙ ПОПЫТКЕ (учти их):\n" + "\n".join(f"- {i}" for i in feedback) + "\n") if feedback else ""
        prompt = self.svc.prompts.render(
            "write_part", part=str(part + 1), parts=str(parts), number=str(idx + 1), chapter_title=plan.title, title=pr.title,
            words=str(words), genre=pr.genre or "по замыслу", tone=pr.tone or "по замыслу", setting=pr.setting,
            world_rules="\n".join(f"- {r}" for r in pr.world_rules) or "(нет)", cast=self.svc._cast_text(cast),
            previous=self.svc._previous_text(plans, idx), summary=plan.summary, pov=plan.pov or "на ваш выбор",
            location=plan.location or "на ваш выбор", goal=plan.goal, conflict=plan.conflict, outcome=plan.outcome,
            clues="; ".join(plan.clues_planted) or "(нет)", reveals="; ".join(plan.reveals) or "(нет)", hook=plan.hook or "(на ваш выбор)",
            tail=(text_so_far[-1200:] if text_so_far else "(глава ещё не начата)"), task=task, feedback=fb)
        opts = {"num_ctx": self.svc.settings.num_ctx, "num_predict": max(self.svc.settings.num_predict, 3072),
                "temperature": self.svc.settings.creative_temperature}
        best = ""
        for _try in range(2):
            raw = self.svc.caller.llm.chat(model, [{"role": "system", "content": SYSTEM_PROSE}, {"role": "user", "content": prompt}], options=opts)
            piece = clean_prose(raw)
            if len(piece) > len(best):
                best = piece
            if not validate_prose(piece, int(words * 0.6)):
                return piece
        return best

    # ---------- ручная правка ----------
    def update_text(self, pid: str, idx: int, text: str) -> list[str]:
        params = self.svc.params(pid)
        text = text.strip()
        issues, chk = self._check(pid, text, params)
        r = self.db.one("SELECT text_attempts FROM adaptation_chapters WHERE project_id=? AND idx=?", (pid, idx))
        if r is None:
            raise BookMindError("Глава не найдена")
        self._store(pid, idx, text, "flagged" if issues else "ok", self._parts_for(params.chapter_words), r["text_attempts"], chk, issues)
        self.check_book(pid)
        return issues

    def issues(self, pid: str, idx: int) -> list[str]:
        r = self.db.one("SELECT text_check_json FROM adaptation_chapters WHERE project_id=? AND idx=?", (pid, idx))
        return json.loads(r["text_check_json"]).get("issues", []) if r else []
