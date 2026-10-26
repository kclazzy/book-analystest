"""Преобразование XHTML/HTML в текст с сохранением позиций якорей и заголовков."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "li", "tr", "blockquote", "section", "article", "ul", "ol", "table", "pre", "h1", "h2", "h3",
          "h4", "h5", "h6", "br", "hr", "dd", "dt", "figure", "figcaption", "body"}
_SKIP = {"script", "style", "head", "title", "svg"}
_WS = re.compile(r"\s+")
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


@dataclass
class HtmlText:
    text: str
    anchors: dict[str, int] = field(default_factory=dict)
    headings: list[tuple[int, int, str]] = field(default_factory=list)  # (уровень, позиция, текст)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.length = 0
        self.pending_break = 0
        self.pending_anchors: list[str] = []
        self.anchors: dict[str, int] = {}
        self.headings: list[tuple[int, int, str]] = []
        self._skip_depth = 0
        self._heading_level = 0
        self._heading_start = 0
        self._heading_wait = False
        self._heading_buf: list[str] = []

    def _emit(self, s: str) -> None:
        if not s:
            return
        if self.pending_break and self.length > 0:
            sep = "\n" * self.pending_break
            self.out.append(sep)
            self.length += len(sep)
        self.pending_break = 0
        for a in self.pending_anchors:
            self.anchors.setdefault(a, self.length)
        self.pending_anchors.clear()
        if self._heading_wait:
            self._heading_start = self.length
            self._heading_wait = False
        self.out.append(s)
        self.length += len(s)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP:
            self._skip_depth += 1
            return
        for k, v in attrs:
            if k in ("id", "name") and v:
                self.pending_anchors.append(v)
        if tag in _BLOCK:
            self.pending_break = max(self.pending_break, 1 if tag in ("br", "li", "tr") else 2)
        if tag in _HEADINGS:
            self._heading_level = _HEADINGS[tag]
            self._heading_buf = []
            self._heading_wait = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK:
            self.pending_break = max(self.pending_break, 1 if tag in ("br", "li", "tr") else 2)
        if tag in _HEADINGS and self._heading_level:
            title = " ".join("".join(self._heading_buf).split())
            if title:
                self.headings.append((self._heading_level, self._heading_start, title))
            self._heading_level = 0

    def handle_data(self, data):
        if self._skip_depth:
            return
        collapsed = _WS.sub(" ", data)
        if not collapsed:
            return
        at_line_start = self.pending_break > 0 or self.length == 0 or self.out[-1].endswith("\n")
        if at_line_start:
            collapsed = collapsed.lstrip()
            if not collapsed:
                return
        if self._heading_level:
            self._heading_buf.append(collapsed)
        self._emit(collapsed)


def html_to_text(html: str) -> HtmlText:
    p = _Parser()
    p.feed(html)
    p.close()
    text = "".join(p.out)
    # финальная подрезка пробелов по краям строк без сдвига позиций: только хвост
    return HtmlText(text=text.rstrip(), anchors=p.anchors, headings=p.headings)
