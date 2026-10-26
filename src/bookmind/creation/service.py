"""Создание новой книги: скелет → предпосылка → герои → план глав → проверки. Каждый шаг сохраняется; запуск возобновляем."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bookmind.analysis.extractor import StructuredCaller
from bookmind.config import Settings
from bookmind.creation import validation as val
from bookmind.creation.divergence import Assignment, PlannedBeat, assign
from bookmind.creation.originality import Check, OriginalCorpus
from bookmind.creation.schemas import DIVERGENCE_LABELS, AdaptParams, Cast, ChapterPlan, Premise
from bookmind.creation.skeleton import Skeleton, SkeletonBuilder
from bookmind.domain.exceptions import (
    BookMindError, CancelledError, ModelNotInstalledError, ModelOutputError, OllamaRequestError, OllamaUnavailableError,
)
from bookmind.llm.prompts import PromptStore
from bookmind.storage.database import Database, new_id, now_iso

TRANSIENT = (ModelOutputError, OllamaRequestError, ModelNotInstalledError)
STAGES = ("skeleton", "premise", "cast", "plan", "review", "done")


@dataclass
class AdaptProgress:
    project_id: str
    stage: str
    done: int
    total: int
    message: str = ""


@dataclass
class AdaptSummary:
    project_id: str
    status: str            # completed | awaiting_review | paused | failed
    stage: str
    flagged: list[int]
    message: str = ""


ProgressCb = Callable[[AdaptProgress], None]


def _feedback(items: list[str]) -> str:
    return ("ЗАМЕЧАНИЯ К ПРЕДЫДУЩЕМУ ВАРИАНТУ (исправь их):\n" + "\n".join(f"- {i}" for i in items) + "\n") if items else ""


class AdaptationService:
    def __init__(self, db: Database, caller: StructuredCaller, settings: Settings, prompts: PromptStore | None = None):
        self.db, self.caller, self.settings = db, caller, settings
        self.prompts = prompts or PromptStore(None)
        self._corpora: dict[str, OriginalCorpus] = {}

    # ---------- проекты ----------
    def create(self, series_id: str, params: AdaptParams | None = None, title: str = "") -> str:
        params = params or AdaptParams()
        if not self.db.one("SELECT 1 FROM series WHERE id = ?", (series_id,)):
            raise BookMindError("Серия не найдена")
        if params.divergence not in DIVERGENCE_LABELS:
            raise BookMindError("Неизвестный уровень расхождения")
        pid = new_id()
        self.db.insert("adaptation_projects", {
            "id": pid, "series_id": series_id, "title": title.strip() or "Новая книга по мотивам",
            "params_json": params.model_dump_json(), "created_at": now_iso(), "updated_at": now_iso()})
        return pid

    def list_projects(self, series_id: str) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT id, title, status, stage, created_at FROM adaptation_projects WHERE series_id = ? ORDER BY created_at DESC", (series_id,))]

    def delete(self, pid: str) -> None:
        self.db.execute("DELETE FROM adaptation_projects WHERE id = ?", (pid,))
        self._corpora.pop(pid, None)

    def _row(self, pid: str):
        r = self.db.one("SELECT * FROM adaptation_projects WHERE id = ?", (pid,))
        if r is None:
            raise BookMindError("Проект не найден")
        return r

    def params(self, pid: str) -> AdaptParams:
        return AdaptParams(**json.loads(self._row(pid)["params_json"]))

    def _save(self, pid: str, **cols) -> None:
        cols["updated_at"] = now_iso()
        self.db.update("adaptation_projects", pid, cols)

    def approve(self, pid: str) -> None:
        p = self.params(pid)
        p.approved = True
        self._save(pid, params_json=p.model_dump_json())

    # ---------- чтение состояния ----------
    def skeleton(self, pid: str) -> tuple[Skeleton, Assignment] | None:
        d = json.loads(self._row(pid)["skeleton_json"])
        return (Skeleton(**d["skeleton"]), Assignment(**d["assignment"])) if d else None

    def premise(self, pid: str) -> Premise | None:
        d = json.loads(self._row(pid)["premise_json"])
        return Premise(**d) if d else None

    def cast(self, pid: str) -> Cast | None:
        d = json.loads(self._row(pid)["cast_json"])
        return Cast(**d) if d else None

    def plans(self, pid: str) -> dict[int, ChapterPlan]:
        return {r["idx"]: ChapterPlan(**json.loads(r["plan_json"])) for r in self.db.query(
            "SELECT idx, plan_json FROM adaptation_chapters WHERE project_id = ? AND plan_json != '{}' ORDER BY idx", (pid,))}

    def _corpus(self, pid: str) -> OriginalCorpus:
        if pid not in self._corpora:
            r = self._row(pid)
            books = self.params(pid).source_book_ids or [b["id"] for b in self.db.query(
                "SELECT id FROM books WHERE series_id = ? ORDER BY series_order", (r["series_id"],))]
            self._corpora[pid] = OriginalCorpus(self.db, r["series_id"], books)
        return self._corpora[pid]

    # ---------- запуск ----------
    def run(self, pid: str, model: str, on_progress: ProgressCb | None = None,
            cancel: threading.Event | None = None) -> AdaptSummary:
        cancel = cancel or threading.Event()
        params = self.params(pid)
        self._save(pid, status="running", model=model, error=None)

        def report(stage: str, done: int = 0, total: int = 0, msg: str = "") -> None:
            if on_progress:
                on_progress(AdaptProgress(pid, stage, done, total, msg))

        def check_cancel() -> None:
            if cancel.is_set():
                raise CancelledError("Остановлено пользователем")

        try:
            self._stage_skeleton(pid, params, report)
            check_cancel()
            self._stage_premise(pid, model, params, report, check_cancel)
            self._stage_cast(pid, model, params, report, check_cancel)
            self._stage_plan(pid, model, params, report, check_cancel)
        except CancelledError:
            self._save(pid, status="paused")
            return self._summary(pid, "paused", "Остановлено; прогресс сохранён")
        except OllamaUnavailableError as exc:
            self._save(pid, status="paused", error=str(exc))
            return self._summary(pid, "paused", f"Ollama недоступен: {exc}")
        except TRANSIENT as exc:
            self._save(pid, status="failed", error=str(exc))
            return self._summary(pid, "failed", f"Не удалось получить корректный ответ модели: {exc}")
        except (BookMindError, ValueError) as exc:
            self._save(pid, status="failed", error=str(exc))
            return self._summary(pid, "failed", str(exc))
        if params.pause_after_plan and not self.params(pid).approved:
            self._save(pid, status="awaiting_review", stage="review")
            return self._summary(pid, "awaiting_review", "План готов: проверьте его и подтвердите продолжение")
        self._save(pid, status="completed", stage="done")
        return self._summary(pid, "completed", "")

    def _summary(self, pid: str, status: str, msg: str) -> AdaptSummary:
        flagged = [r["idx"] for r in self.db.query(
            "SELECT idx FROM adaptation_chapters WHERE project_id = ? AND status = 'flagged' ORDER BY idx", (pid,))]
        return AdaptSummary(pid, status, self._row(pid)["stage"], flagged, msg)

    # ---------- этапы ----------
    def _stage_skeleton(self, pid: str, params: AdaptParams, report) -> None:
        if json.loads(self._row(pid)["skeleton_json"]):
            return
        report("skeleton", 0, 1, "строю скелет оригинала")
        r = self._row(pid)
        sk, _anon = SkeletonBuilder(self.db).build(r["series_id"], params.source_book_ids or None)
        seed = params.seed if params.seed is not None else int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16)
        asg = assign(sk, params, seed)
        self._save(pid, stage="skeleton", skeleton_json=json.dumps(
            {"skeleton": sk.model_dump(), "assignment": asg.model_dump(), "seed": seed}, ensure_ascii=False))
        report("skeleton", 1, 1, "скелет готов")

    def _skeleton_text(self, sk: Skeleton, asg: Assignment) -> str:
        roles = "\n".join(f"- {r.label}" + (f": {r.arc}" if r.arc else "") for r in sk.roles)
        beats = "\n".join(f"- (глава ~{b.chapter + 1} из {asg.chapters}) {b.text}" + (f" [{b.instruction}]" if b.instruction else "")
                          for b in asg.beats[:40])
        return f"Роли:\n{roles}\n\nПовороты по ходу книги:\n{beats}"

    def _attempts(self, params: AdaptParams) -> int:
        return max(1, params.max_replans + 1)

    def _stage_premise(self, pid, model, params, report, check_cancel) -> None:
        if json.loads(self._row(pid)["premise_json"]):
            return
        sk, asg = self.skeleton(pid)
        feedback: list[str] = []
        best: Premise | None = None
        for attempt in range(self._attempts(params)):
            check_cancel()
            report("premise", attempt, self._attempts(params), "придумываю предпосылку")
            prompt = self.prompts.render(
                "adapt_premise", skeleton=self._skeleton_text(sk, asg), changes="\n".join(f"- {c}" for c in asg.global_changes),
                genre=params.genre or "на твой выбор, отличный от оригинала", setting=params.setting or "придумай новый мир",
                chapters=str(asg.chapters), feedback=_feedback(feedback))
            pr, _raw, _iss = self.caller.call(model, prompt, Premise, temperature=self.settings.creative_temperature)
            best = pr
            feedback = val.validate_premise(pr)
            chk = self._corpus(pid).check(" ".join([pr.title, pr.logline, pr.setting, pr.central_conflict, *pr.world_rules]), params.divergence)
            if chk.name_hits:
                feedback += chk.reasons
            if not feedback:
                break
        self._save(pid, stage="premise", premise_json=best.model_dump_json())

    def _roles_text(self, sk: Skeleton) -> str:
        return "\n".join(f"- {r.label}" + (f" (арка: {r.arc})" if r.arc else "") for r in sk.roles)

    def _stage_cast(self, pid, model, params, report, check_cancel) -> None:
        if json.loads(self._row(pid)["cast_json"]):
            return
        sk, asg = self.skeleton(pid)
        pr = self.premise(pid)
        feedback: list[str] = []
        best: Cast | None = None
        for attempt in range(self._attempts(params)):
            check_cancel()
            report("cast", attempt, self._attempts(params), "придумываю героев")
            prompt = self.prompts.render(
                "adapt_cast", title=pr.title, logline=pr.logline, setting=pr.setting, conflict=pr.central_conflict, tone=pr.tone,
                roles=self._roles_text(sk), changes="\n".join(f"- {c}" for c in asg.global_changes), feedback=_feedback(feedback))
            cast, _raw, _iss = self.caller.call(model, prompt, Cast, temperature=self.settings.creative_temperature)
            best = cast
            feedback = val.validate_cast(cast, sk.role_labels())
            text = " ".join(f"{m.name} {m.appearance} {m.backstory} {m.secret} {m.goal}" for m in cast.members)
            chk = self._corpus(pid).check(text, params.divergence)
            if chk.name_hits:
                feedback += chk.reasons
            if not feedback:
                break
        self._save(pid, stage="cast", cast_json=best.model_dump_json())

    # ---------- план глав ----------
    def _cast_text(self, cast: Cast) -> str:
        return "\n".join(f"- {m.name} ({m.role}): цель — {m.goal}; слабость — {m.weakness}; тайна — {m.secret}" for m in cast.members)

    def _beat_line(self, b: PlannedBeat) -> str:
        who = f" (роли: {', '.join(b.roles)})" if b.roles else ""
        return f"[{b.id}]{who} {b.text}" + (f"\n    {b.instruction}" if b.instruction else "")

    def _previous_text(self, plans: dict[int, ChapterPlan], idx: int) -> str:
        lines = []
        for j in range(max(0, idx - 5), idx):
            if j in plans:
                p = plans[j]
                lines.append(f"Глава {j + 1} «{p.title}»: {p.summary[:240]}")
        return "\n".join(lines) or "(это первая глава)"

    def _plan_prompt(self, pid: str, idx: int, plans: dict[int, ChapterPlan], feedback: list[str]) -> str:
        sk, asg = self.skeleton(pid)
        pr, cast = self.premise(pid), self.cast(pid)
        nxt = [self._beat_line(b).split("\n")[0] for b in asg.for_chapter(idx + 1)][:4]
        return self.prompts.render(
            "adapt_chapter", number=str(idx + 1), total=str(asg.chapters), title=pr.title, logline=pr.logline,
            setting=pr.setting, tone=pr.tone, world_rules="\n".join(f"- {r}" for r in pr.world_rules) or "(нет)",
            cast=self._cast_text(cast), previous=self._previous_text(plans, idx),
            beats="\n".join(self._beat_line(b) for b in asg.for_chapter(idx)) or "(свободная глава: развитие героев и мира, подготовка следующих поворотов)",
            next_beats="\n".join(nxt) or "(нет)", changes="\n".join(f"- {c}" for c in asg.global_changes), feedback=_feedback(feedback))

    def _check_plan(self, pid: str, plan: ChapterPlan, divergence: str) -> tuple[list[str], Check]:
        chk = self._corpus(pid).check(plan.text(), divergence)
        issues = val.validate_chapter(plan, self.cast(pid)) + chk.reasons
        return issues, chk

    def _store_chapter(self, pid, idx, plan, status, attempts, chk: Check | None, issues: list[str]) -> None:
        self.db.execute(
            "INSERT INTO adaptation_chapters(project_id, idx, plan_json, status, attempts, check_json) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(project_id, idx) DO UPDATE SET plan_json=excluded.plan_json, status=excluded.status, "
            "attempts=excluded.attempts, check_json=excluded.check_json",
            (pid, idx, plan.model_dump_json(), status, attempts,
             json.dumps({"check": chk.as_dict() if chk else {}, "issues": issues}, ensure_ascii=False)))

    def _plan_one(self, pid, idx, model, params, plans, feedback, check_cancel, attempts_done) -> int:
        """Составляет главу, проверяет и при необходимости переделывает. Возвращает число использованных попыток."""
        attempts = attempts_done
        fb = list(feedback)
        while True:
            check_cancel()
            attempts += 1
            prompt = self._plan_prompt(pid, idx, plans, fb)
            plan, _raw, _iss = self.caller.call(model, prompt, ChapterPlan, temperature=self.settings.creative_temperature)
            issues, chk = self._check_plan(pid, plan, params.divergence)
            plans[idx] = plan
            if not issues or attempts >= self._attempts(params):
                self._store_chapter(pid, idx, plan, "flagged" if issues else "ok", attempts, chk, issues)
                return attempts
            self._store_chapter(pid, idx, plan, "pending", attempts, chk, issues)
            fb = issues

    def _stage_plan(self, pid, model, params, report, check_cancel) -> None:
        sk, asg = self.skeleton(pid)
        plans = self.plans(pid)
        rows = {r["idx"]: r for r in self.db.query("SELECT * FROM adaptation_chapters WHERE project_id = ?", (pid,))}
        total = asg.chapters
        for idx in range(total):
            if idx in rows and rows[idx]["status"] in ("ok", "flagged"):
                continue
            report("plan", idx, total, f"план главы {idx + 1} из {total}")
            self._plan_one(pid, idx, model, params, plans, [], check_cancel, rows[idx]["attempts"] if idx in rows else 0)
            self._save(pid, stage="plan")
        # сквозная проверка: покрытие битов, порядок загадок
        for _round in range(self._attempts(params) - 1):
            problems = val.validate_global(plans, asg)
            redo = {i: v for i, v in problems.items() if self._chapter_attempts(pid, i) < self._attempts(params)}
            if not redo:
                break
            for i, issues in sorted(redo.items()):
                report("plan", i, total, f"переделываю главу {i + 1}: {issues[0][:60]}")
                self._plan_one(pid, i, model, params, plans, issues, check_cancel, self._chapter_attempts(pid, i))
        problems = val.validate_global(plans, asg)
        warnings = [f"Глава {i + 1}: {m}" for i, v in sorted(problems.items()) for m in v]
        warnings += [f"Герой «{n}» не используется ни в одной главе" for n in val.unused_cast(plans, self.cast(pid))]
        self._save(pid, stage="plan", issues_json=json.dumps(warnings, ensure_ascii=False))
        report("plan", total, total, "план готов")

    def _chapter_attempts(self, pid: str, idx: int) -> int:
        return self.db.scalar("SELECT attempts FROM adaptation_chapters WHERE project_id = ? AND idx = ?", (pid, idx)) or 0

    # ---------- ручная правка ----------
    def update_plan(self, pid: str, idx: int, plan: ChapterPlan) -> Check:
        params = self.params(pid)
        issues, chk = self._check_plan(pid, plan, params.divergence)
        self._store_chapter(pid, idx, plan, "flagged" if issues else "ok", self._chapter_attempts(pid, idx), chk, issues)
        return chk

    # ---------- отчёты ----------
    def report(self, pid: str) -> dict:
        r = self._row(pid)
        sk_asg = self.skeleton(pid)
        chapters = []
        for row in self.db.query("SELECT * FROM adaptation_chapters WHERE project_id = ? ORDER BY idx", (pid,)):
            plan = ChapterPlan(**json.loads(row["plan_json"]))
            chk = json.loads(row["check_json"])
            beats = [b for b in sk_asg[1].for_chapter(row["idx"])] if sk_asg else []
            chapters.append({"idx": row["idx"], "title": plan.title, "summary": plan.summary, "status": row["status"],
                             "attempts": row["attempts"], "issues": chk.get("issues", []), "check": chk.get("check", {}),
                             "source_chapters": sorted({b.source_chapter for b in beats}), "beats": [b.id for b in beats]})
        return {"id": pid, "title": r["title"], "status": r["status"], "stage": r["stage"], "error": r["error"],
                "divergence": self.params(pid).divergence, "chapters": chapters, "warnings": json.loads(r["issues_json"]),
                "flagged": [c["idx"] for c in chapters if c["status"] == "flagged"]}

    def bible_markdown(self, pid: str) -> str:
        rep, pr, cast = self.report(pid), self.premise(pid), self.cast(pid)
        sk_asg = self.skeleton(pid)
        L = [f"# {pr.title if pr and pr.title else rep['title']}", ""]
        if pr:
            L += [f"*{pr.logline}*", "", f"**Жанр:** {pr.genre}  ", f"**Тон:** {pr.tone}  ", f"**Мир:** {pr.setting}  ",
                  f"**Центральный конфликт:** {pr.central_conflict}", ""]
            if pr.themes:
                L += ["**Темы:** " + ", ".join(pr.themes), ""]
            if pr.world_rules:
                L += ["## Правила мира", *[f"- {x}" for x in pr.world_rules], ""]
        if cast:
            L += ["## Герои", ""]
            for m in cast.members:
                L += [f"### {m.name} — {m.role}", f"Внешность: {m.appearance}  ", f"Характер: {m.personality}  ", f"Цель: {m.goal}  ",
                      f"Слабость: {m.weakness}  ", f"Тайна: {m.secret}  ", f"Предыстория: {m.backstory}", ""]
        if sk_asg:
            L += ["## Чем книга отличается от оригинала", f"Уровень расхождения: {DIVERGENCE_LABELS[rep['divergence']]}",
                  *[f"- {c}" for c in sk_asg[1].global_changes],
                  f"- Выброшено поворотов оригинала: {len(sk_asg[1].dropped)}; изменён исход у: {len(sk_asg[1].mutations)}", ""]
        L += ["## План глав", ""]
        for c in rep["chapters"]:
            flag = "" if c["status"] == "ok" else " ⚠"
            L += [f"### Глава {c['idx'] + 1}. {c['title']}{flag}", c["summary"], ""]
            plan = self.plans(pid).get(c["idx"])
            if plan:
                L += [f"- Точка зрения: {plan.pov}; место: {plan.location}", f"- Цель: {plan.goal}", f"- Конфликт: {plan.conflict}",
                      f"- Итог: {plan.outcome}", f"- Крючок: {plan.hook}"]
                L += [f"- Подсказки: {'; '.join(plan.clues_planted)}"] if plan.clues_planted else []
                L += [f"- Раскрытия: {'; '.join(plan.reveals)}"] if plan.reveals else []
            L += [f"- Сходство с оригиналом: {c['check'].get('similarity', 0):.0%}" if c["check"] else "", *[f"- ⚠ {i}" for i in c["issues"]], ""]
        if rep["warnings"]:
            L += ["## Предупреждения", *[f"- {w}" for w in rep["warnings"]], ""]
        return "\n".join(x for x in L if x is not None).rstrip() + "\n"

    def export_bible(self, pid: str, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.bible_markdown(pid), encoding="utf-8")
        return path
