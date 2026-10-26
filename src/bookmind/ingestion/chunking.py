"""Сегментация главы на фрагменты по абзацам/предложениям. Фрагменты не перекрываются; контекст предыдущего
фрагмента берётся отдельно (context_before) и используется только в промпте."""
from __future__ import annotations

import re
from dataclasses import dataclass

_PARA = re.compile(r"\n\s*\n")
_SENT_END = re.compile(r"(?<=[.!?…»\"”])\s+(?=[«\"“—–\-A-ZА-ЯЁ0-9])")


@dataclass(frozen=True)
class ChunkSpan:
    start: int
    end: int


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA.finditer(text):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _split_long(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Делит слишком длинный абзац по предложениям, затем по пробелам."""
    seg = text[start:end]
    pieces: list[tuple[int, int]] = []
    pos = 0
    for m in _SENT_END.finditer(seg):
        pieces.append((pos, m.start()))
        pos = m.end()
    pieces.append((pos, len(seg)))
    out: list[tuple[int, int]] = []
    for ps, pe in pieces:
        while pe - ps > max_chars:
            cut = seg.rfind(" ", ps, ps + max_chars)
            if cut <= ps:
                cut = ps + max_chars
            out.append((ps, cut))
            ps = cut + 1 if seg[cut:cut + 1] == " " else cut
        if pe > ps:
            out.append((ps, pe))
    return [(start + s, start + e) for s, e in out if seg[s:e].strip()]


def chunk_text(text: str, min_chars: int = 1500, max_chars: int = 3000) -> list[ChunkSpan]:
    if min_chars <= 0 or max_chars < min_chars:
        raise ValueError("Некорректные размеры фрагмента")
    units: list[tuple[int, int]] = []
    for s, e in _paragraph_spans(text):
        units.extend(_split_long(text, s, e, max_chars) if e - s > max_chars else [(s, e)])
    spans: list[ChunkSpan] = []
    cur_s = cur_e = None
    for s, e in units:
        if cur_s is None:
            cur_s, cur_e = s, e
            continue
        if e - cur_s <= max_chars and (cur_e - cur_s) < min_chars:
            cur_e = e
        elif e - cur_s <= max_chars and (cur_e - cur_s) >= min_chars and (e - cur_s) <= (min_chars + max_chars) // 2:
            cur_e = e  # небольшая добавка, не нарушающая предела: уменьшает число мелких хвостов
        else:
            spans.append(ChunkSpan(cur_s, cur_e))
            cur_s, cur_e = s, e
    if cur_s is not None:
        spans.append(ChunkSpan(cur_s, cur_e))
    # маленький хвост присоединяем к предыдущему, если вместе не превышают предел
    if len(spans) >= 2 and spans[-1].end - spans[-1].start < min_chars // 2:
        a, b = spans[-2], spans[-1]
        if b.end - a.start <= max_chars:
            spans[-2:] = [ChunkSpan(a.start, b.end)]
    return spans


def context_before(chapter_text: str, chunk_start: int, overlap_chars: int) -> str:
    """Небольшой хвост предыдущего текста главы (с границы слова/предложения) для связности промпта."""
    if overlap_chars <= 0 or chunk_start <= 0:
        return ""
    begin = max(0, chunk_start - overlap_chars)
    seg = chapter_text[begin:chunk_start]
    if begin > 0:
        m = re.search(r"[.!?…»”]\s+|\s+", seg)
        if m:
            seg = seg[m.end():]
    return seg.strip()
