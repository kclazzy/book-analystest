"""Процедурные иллюстрации и обложка для новой книги: абстрактные «звуковые» пейзажи (город-силуэт, волны, кристалл).
Картинки создаются кодом, а не нейросетью: они не изображают конкретные сцены, а задают настроение главы.
Чем дальше глава, тем теплее палитра (тишина → звук). Результат одинаков для одних и тех же аргументов."""
from __future__ import annotations

import io
import math
import random
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:                                  # Pillow необязателен: без него книга собирается без картинок
    Image = None                                     # type: ignore[assignment]

CHAPTER_SIZE = (720, 300)
COVER_SIZE = (800, 1200)
_FONTS = ("DejaVuSerif-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf", "segoeuib.ttf")
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts")


def available() -> bool:
    return Image is not None


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _palette(progress: float, rnd: random.Random):
    p = max(0.0, min(1.0, progress))
    top = _mix((10, 16, 38), (52, 20, 44), p)
    bottom = _mix((30, 66, 112), (176, 82, 56), p)
    accent = _mix((80, 190, 245), (255, 178, 64), p)
    accent2 = _mix((176, 112, 250), (255, 92, 128), p)
    shift = rnd.randint(-12, 12)
    return top, bottom, tuple(max(0, min(255, c + shift)) for c in accent), accent2


def _scene(w: int, h: int, seed: int, progress: float) -> Image.Image:
    rnd = random.Random(seed * 7919 + 13)
    top, bottom, accent, accent2 = _palette(progress, rnd)
    k = 2                                            # рисуем в двойном размере и уменьшаем: гладкие линии
    W, H = w * k, h * k
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=_mix(top, bottom, (y / H) ** 1.3))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    g = ImageDraw.Draw(glow)
    cx, cy, r = rnd.randint(W // 5, W * 4 // 5), rnd.randint(H // 5, H // 2), rnd.randint(H // 12, H // 6)
    for i in range(6, 0, -1):                        # свечение кристалла
        rr = r * (1 + i * 0.7)
        g.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=tuple(c * (7 - i) // 40 for c in accent))
    g.polygon([(cx, cy - r), (cx + r * 0.6, cy), (cx, cy + r), (cx - r * 0.6, cy)], fill=accent)
    glow = glow.filter(ImageFilter.GaussianBlur(H // 40))
    img = _add(img, glow)
    d = ImageDraw.Draw(img)
    for n in range(3):                               # волны
        amp, freq, phase = rnd.randint(H // 14, H // 5), rnd.uniform(1.0, 3.6), rnd.uniform(0, math.tau)
        base = H * (0.35 + 0.12 * n) + rnd.randint(-H // 20, H // 20)
        col = accent if n % 2 == 0 else accent2
        pts = [(x, base + amp * math.sin(freq * math.tau * x / W + phase) * (0.55 + 0.45 * math.sin(2.1 * math.tau * x / W + n))) for x in range(0, W + 8, 8)]
        d.line(pts, fill=col, width=max(2, H // 90), joint="curve")
    x = 0                                            # силуэт города из «аккордов»
    ground = _mix(top, (0, 0, 0), 0.65)
    while x < W:
        bw = rnd.randint(W // 40, W // 14)
        bh = int(H * rnd.uniform(0.12, 0.42) * (0.7 + 0.5 * math.sin(x / W * math.pi)))
        d.rectangle([x, H - bh, x + bw, H], fill=ground)
        for wy in range(H - bh + H // 30, H - H // 40, H // 22):
            for wx in range(x + bw // 5, x + bw - bw // 5, max(6, bw // 3)):
                if rnd.random() < 0.18 + 0.4 * progress:
                    d.rectangle([wx, wy, wx + 3 * k, wy + 5 * k], fill=accent)
        x += bw + rnd.randint(0, W // 120)
    fog = Image.new("RGB", (W, H), _mix(bottom, (230, 235, 245), 0.35))
    mask = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(v * (0.5 - 0.35 * progress)))
    img = Image.composite(fog, img, mask.transpose(Image.FLIP_TOP_BOTTOM))
    return img.resize((w, h), Image.LANCZOS)


def _add(a: Image.Image, b: Image.Image) -> Image.Image:
    from PIL import ImageChops
    return ImageChops.add(a, b)


def _png(img: Image.Image, colors: int = 96) -> bytes:
    buf = io.BytesIO()
    img.convert("P", palette=Image.ADAPTIVE, colors=colors).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def chapter_art(seed: int, progress: float, size: tuple[int, int] = CHAPTER_SIZE) -> bytes:
    """Картинка к главе: seed — номер главы, progress от 0 (начало) до 1 (конец книги)."""
    if Image is None:
        raise RuntimeError("Для иллюстраций нужен пакет Pillow")
    return _png(_scene(size[0], size[1], seed, progress))


def _font(size: int):
    for d in _FONT_DIRS:
        for f in _FONTS:
            p = Path(d) / f
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def cover_art(title: str, author: str, seed: int = 0, size: tuple[int, int] = COVER_SIZE) -> bytes:
    """Обложка: фон-пейзаж, название и автор."""
    if Image is None:
        raise RuntimeError("Для иллюстраций нужен пакет Pillow")
    w, h = size
    img = _scene(w, h, seed + 101, 0.35)
    shade = Image.new("RGB", (w, h), (0, 0, 0))
    img = Image.composite(shade, img, Image.linear_gradient("L").resize((w, h)).point(lambda v: int(v * 0.55)))
    d = ImageDraw.Draw(img)
    f_title, f_author = _font(w // 11), _font(w // 20)
    y = h // 9
    for line in _wrap(d, title, f_title, int(w * 0.84)):
        tw = d.textlength(line, font=f_title)
        d.text(((w - tw) / 2 + 2, y + 2), line, font=f_title, fill=(0, 0, 0))
        d.text(((w - tw) / 2, y), line, font=f_title, fill=(244, 238, 226))
        y += int(w / 11 * 1.25)
    tw = d.textlength(author, font=f_author)
    d.text(((w - tw) / 2, h - h // 9), author, font=f_author, fill=(236, 214, 170))
    return _png(img, 128)
