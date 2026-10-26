from bookmind.ingestion.text_utils import build_index, decode_bytes, find_quote, normalize_text


def test_normalize_text_only_technical_changes():
    raw = "Привет мир\r\n\r\n\r\n\r\nВторая​ строка  \n"
    text, ops = normalize_text(raw)
    assert text == "Привет мир\n\nВторая строка"
    assert {"newlines_unified", "nbsp_to_space", "zero_width_removed", "whitespace_collapsed"} <= set(ops)


def test_normalize_does_not_change_words():
    raw = "Ёлка — это «слово», и ещё слово…"
    assert normalize_text(raw)[0] == raw


def test_decode_cp1251_and_utf8_bom():
    s = "Мира нашла ключ"
    assert decode_bytes(s.encode("cp1251")) == (s, "cp1251")
    assert decode_bytes(b"\xef\xbb\xbf" + s.encode("utf-8"))[1] == "utf-8-sig"


def test_find_quote_ignores_case_punctuation_yo():
    text = "Она сказала: «Я найду того, кто это сделал — и заставлю ЕГО заплатить!» И ушла."
    idx = build_index(text)
    r = find_quote("я найду того кто это сделал и заставлю его заплатить", idx, len(text))
    assert r is not None
    assert text[r[0]:r[1]].startswith("Я найду") and text[r[0]:r[1]].endswith("заплатить")
    assert find_quote("Ёлка", build_index("елка стояла в углу комнаты"), 25) is None  # слишком короткая цитата


def test_find_quote_missing_and_ellipsis_order():
    text = "Мира вошла в лабораторию на рассвете. Дверь была не заперта, а в воздухе стоял запах серы."
    idx = build_index(text)
    assert find_quote("Мира сбежала из города ночью", idx, len(text)) is None
    assert find_quote("Мира вошла в лабораторию ... стоял запах серы", idx, len(text)) is not None
    assert find_quote("стоял запах серы ... Мира вошла в лабораторию", idx, len(text)) is None  # нарушен порядок
