"""Нормализация текста, поиск цитат с учётом регистра/пунктуации, определение кодировки."""
from __future__ import annotations

import re
from dataclasses import dataclass

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def normalize_text(text: str) -> tuple[str, list[str]]:
    """Нормализует технические артефакты, не меняя содержание. Возвращает (текст, список применённых операций)."""
    ops: list[str] = []
    t = text
    if "\r" in t:
        t = t.replace("\r\n", "\n").replace("\r", "\n")
        ops.append("newlines_unified")
    if " " in t or " " in t or " " in t:
        t = t.replace(" ", " ").replace(" ", " ").replace(" ", " ")
        ops.append("nbsp_to_space")
    stripped = t.translate(_ZERO_WIDTH)
    if stripped != t:
        t = stripped
        ops.append("zero_width_removed")
    t2 = re.sub(r"[ \t]+\n", "\n", t)
    t2 = re.sub(r"\n{3,}", "\n\n", t2)
    if t2 != t:
        ops.append("whitespace_collapsed")
    t = t2.strip("\n ")
    if t != t2:
        ops.append("edges_trimmed")
    return t, ops


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Декодирует текстовый файл. Возвращает (текст, название кодировки)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("cp1251", "koi8-r", "cp866"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        letters = [c for c in text if c.isalpha()]
        cyr = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters)
        if letters and cyr / len(letters) > 0.5:
            return text, enc
    return data.decode("latin-1"), "latin-1"


@dataclass
class NormalizedIndex:
    """Нормализованная форма текста с отображением позиций обратно в оригинал."""
    norm: str
    index_map: list[int]  # индекс символа в norm -> индекс в оригинале


def build_index(text: str) -> NormalizedIndex:
    """casefold, ё=е, любая последовательность не-буквенно-цифровых символов -> один пробел."""
    out: list[str] = []
    imap: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        folded = ch.casefold().replace("ё", "е")
        for c in folded:
            if c.isalnum():
                out.append(c)
                imap.append(i)
                prev_space = False
            elif not prev_space:
                out.append(" ")
                imap.append(i)
                prev_space = True
    if out and out[-1] == " ":
        out.pop()
        imap.pop()
    return NormalizedIndex("".join(out), imap)


def norm_key(s: str) -> str:
    """Ключ для сравнения имён и строк."""
    return build_index(s).norm


def name_tokens(s: str) -> list[str]:
    return norm_key(s).split()


_ELLIPSIS = re.compile(r"\s*(?:\.{3,}|…|\[…\]|\[\.\.\.\])\s*")
MIN_QUOTE_NORM_CHARS = 8


def find_quote(quote: str, index: NormalizedIndex, original_len: int) -> tuple[int, int] | None:
    """Ищет цитату (фрагменты через «...» должны идти по порядку). Возвращает (start, end) в оригинале или None.
    Регистр, пунктуация и переносы строк не учитываются; слова не изменяются."""
    parts = [p for p in _ELLIPSIS.split(quote) if p.strip()]
    if not parts:
        return None
    pos = 0
    first_start: int | None = None
    last_end = 0
    for part in parts:
        needle = norm_key(part)
        if len(needle) < MIN_QUOTE_NORM_CHARS:
            return None
        at = index.norm.find(needle, pos)
        if at < 0:
            return None
        end_norm = at + len(needle) - 1
        s = index.index_map[at]
        e = index.index_map[end_norm] + 1
        if first_start is None:
            first_start = s
        last_end = min(e, original_len)
        pos = end_norm + 1
    return first_start, last_end  # type: ignore[return-value]


def token_set(s: str) -> set[str]:
    return {t for t in norm_key(s).split() if len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
