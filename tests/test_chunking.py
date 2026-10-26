import re

import pytest

from bookmind.ingestion.chunking import chunk_text, context_before
from tests.fixtures.novel import BOOK1


def _letters(s):
    return re.sub(r"\s+", "", s)


def test_each_novel_chapter_is_single_chunk_by_default():
    for _title, text in BOOK1:
        assert len(chunk_text(text.strip(), 1500, 3000)) == 1


def test_chunks_cover_all_text_in_order_without_overlap():
    para = "Это длинное предложение для проверки сегментации текста на фрагменты. " * 8
    text = "\n\n".join(para.strip() for _ in range(30))
    spans = chunk_text(text, 1500, 3000)
    assert len(spans) > 3
    assert _letters("".join(text[s.start:s.end] for s in spans)) == _letters(text)
    for a, b in zip(spans, spans[1:]):
        assert a.end <= b.start
    assert all(s.end - s.start <= 3000 for s in spans)


def test_giant_paragraph_is_split_by_sentences_then_words():
    text = "Слово " * 2000
    spans = chunk_text(text.strip(), 1000, 2000)
    assert all(s.end - s.start <= 2000 for s in spans)
    assert _letters("".join(text[s.start:s.end] for s in spans)) == _letters(text)


def test_empty_text_and_bad_params():
    assert chunk_text("") == []
    with pytest.raises(ValueError):
        chunk_text("x", 100, 50)


def test_context_before_is_tail_of_previous_text():
    text = "Первое предложение. Второе предложение здесь. " * 20
    ctx = context_before(text, 500, 120)
    assert 0 < len(ctx) <= 120 and ctx in text[:500]
    assert context_before(text, 0, 120) == ""
