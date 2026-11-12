#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
md2pdf.py — генерация PDF-копий Markdown-документов проекта.

Для каждого `*.md` в целевом каталоге (по умолчанию `docs/`) создаётся
одноимённый `*.pdf` в каталоге вывода — по умолчанию `docs/pdf/`
(HTML, картинки и скрипты лежат отдельно от готовых PDF).

Поддерживаемое подмножество Markdown (то, что реально используется в docs/):
  * заголовки ATX (#..######), горизонтальные линейки (---);
  * абзацы, **жирный**, *курсив*, `моноширинный`, ссылки [текст](url);
  * таблицы GFM (в т.ч. с разметкой, переносом строки <br> и картинками
    внутри ячеек; в широких таблицах — от шести колонок — картинки
    выводятся миниатюрами);
  * маркированные и нумерованные списки с вложенностью;
  * цитаты (>) и fenced-блоки кода (```lang) с автоподбором кегля; строки,
    которые не влезают даже мелким кеглем, переносятся;
  * изображения ![alt](path): JPEG/PNG (Pillow) и SVG (svglib; фон тёмных
    схем берётся из style="background:…" корневого элемента);
  * формулы: строчные $...$ и выключные $$...$$ на одной строке. Понимается
    подмножество LaTeX: индексы, \\frac, \\sqrt, \\text, \\mathbf, греческие
    буквы и знаки (\\ge, \\times, \\approx …). Дробь печатается как a/b.

Зависимости (хватает virtualenv, системные пакеты не нужны):
    python3 -m venv .venv
    .venv/bin/pip install reportlab svglib pillow
Запуск:
    .venv/bin/python tools/pdf/md2pdf.py            # docs/*.md -> docs/pdf/*.pdf
    .venv/bin/python tools/pdf/md2pdf.py --out out  # свой каталог для PDF
    .venv/bin/python tools/pdf/md2pdf.py docs firmware   # несколько каталогов
    .venv/bin/python tools/pdf/md2pdf.py --check    # проверить актуальность PDF
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from itertools import count

from reportlab import platypus
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, StyleSheet1
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image as RLImage,
    PageTemplate,
    Paragraph,
    Table,
    TableStyle,
)
from reportlab.platypus.xpreformatted import XPreformatted

try:  # Pillow нужен для JPEG/PNG
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover
    PILImage = None

try:  # svglib нужен для SVG-диаграмм
    from svglib.svglib import svg2rlg
except ImportError:  # pragma: no cover
    svg2rlg = None

# --------------------------------------------------------------------------- #
# Шрифты (кириллица) и цвета оформления
# --------------------------------------------------------------------------- #

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/DejaVu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/local/share/fonts/dejavu",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts",
]

FONT_FILES = {
    "Sans": "DejaVuSans.ttf",
    "Sans-Bold": "DejaVuSans-Bold.ttf",
    "Serif": "DejaVuSerif.ttf",
    "Serif-Bold": "DejaVuSerif-Bold.ttf",
    "Mono": "DejaVuSansMono.ttf",
    "Mono-Bold": "DejaVuSansMono-Bold.ttf",
}

ACCENT = colors.HexColor("#1f4e79")
CODE_BG = colors.HexColor("#f4f6f8")
CODE_BORDER = colors.HexColor("#c8d0d8")
QUOTE_BG = colors.HexColor("#fbf6e9")
QUOTE_BAR = colors.HexColor("#c8a54b")
GRAY = colors.HexColor("#595959")

# Имя зарегистрированного моноширинного шрифта (подставляется в <font face=...>)
MONO_FONT_NAME = "Courier"

# --------------------------------------------------------------------------- #
# Регулярные выражения разметки
# --------------------------------------------------------------------------- #

INLINE_RE = re.compile(
    r"(?P<code>`+(?P<code_text>.+?)`+)"
    r"|(?P<image>!\[(?P<img_alt>[^\]]*)\]\((?P<img_src>[^)\s]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)\s]+)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<bold>\*\*(?P<bold_text>.+?)\*\*|__(?P<bold_text2>.+?)__)"
    r"|(?P<italic>(?<![\w*])\*(?P<italic_text>[^*\n]+?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<italic_text2>[^_\n]+?)_(?![\w_]))"
    r"|(?P<dmath>\$\$(?P<dmath_text>[^$\n]+?)\$\$)"
    # как в Pandoc: формула не начинается и не кончается пробелом, после неё не цифра —
    # иначе за формулу принимается текст между двумя знаками доллара
    r"|(?P<math>\$(?=\S)(?P<math_text>[^$\n]+?)(?<=\S)\$(?!\d))",
    re.S,
)

BR_RE = re.compile(r"<br\s*/?>", re.I)

IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"[^\"]*\")?\)", re.S)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HR_RE = re.compile(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

#: Макросы LaTeX (без обратной косой) -> символ. Формулы в документах простые,
#: поэтому хватает подстановки символов и нескольких конструкций, см. _tex().
MATH_SYMBOLS = {
    "ge": "≥", "geq": "≥", "le": "≤", "leq": "≤", "ne": "≠", "neq": "≠",
    "times": "×", "pm": "±", "approx": "≈", "cdot": "·", "in": "∈", "infty": "∞",
    "to": "→", "rightarrow": "→", "ldots": "…", "dots": "…", "circ": "°",
    "quad": " ", "qquad": " ", "left": "", "right": "",
    "Delta": "Δ", "Omega": "Ω", "Sigma": "Σ",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "eta": "η",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "omega": "ω",
}
MATH_FUNCTIONS = {
    "arctan", "arcsin", "arccos", "sin", "cos", "tan", "min", "max", "log", "ln", "exp",
}
MATH_UPRIGHT = {"text", "textrm", "mathrm", "operatorname"}
MATH_BOLD = {"mathbf", "textbf", "boldsymbol"}

#: Служебные знаки: разметка формулы собирается из них и превращается в теги
#: только после экранирования текста.
_M_UP, _M_DOWN = "\ue000", "\ue001"  # прямой (не курсивный) текст
_M_B, _M_BE = "\ue002", "\ue003"  # полужирный
_M_SUB, _M_SUBE = "\ue004", "\ue005"  # нижний индекс
_M_SUP, _M_SUPE = "\ue006", "\ue007"  # верхний индекс
_M_BREAK = "\ue008"  # перенос строки вокруг выключной формулы $$…$$
_M_TAGS = {
    _M_B: "<b>",
    _M_BE: "</b>",
    _M_SUB: "<sub>",
    _M_SUBE: "</sub>",
    _M_SUP: "<super>",
    _M_SUPE: "</super>",
}

ALIGN_MAP = {"CENTER": TA_CENTER, "RIGHT": TA_RIGHT, "LEFT": TA_LEFT}


def register_fonts() -> dict:
    """Подключает TTF-шрифты DejaVu и возвращает карту имён семейств."""
    found = {}
    for family, filename in FONT_FILES.items():
        for folder in FONT_CANDIDATES:
            path = os.path.join(folder, filename)
            if os.path.isfile(path):
                found[family] = path
                break

    names = {}
    for family, path in found.items():
        name = "Doc" + family
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            names[family] = name
        except Exception as exc:  # pragma: no cover
            print(f"  ! не удалось подключить {path}: {exc}", file=sys.stderr)

    if "Sans" not in names:
        print(
            "  ! шрифты DejaVu не найдены — кириллица в PDF может не отображаться\n"
            "    (установите, например, fonts-dejavu-core)",
            file=sys.stderr,
        )

    def get(family: str, fallback: str) -> str:
        return names.get(family, fallback)

    sans = get("Sans", "Helvetica")
    sans_bold = get("Sans-Bold", sans)
    serif = get("Serif", sans)
    serif_bold = get("Serif-Bold", sans_bold)
    mono = get("Mono", "Courier")
    mono_bold = get("Mono-Bold", mono)

    pdfmetrics.registerFontFamily(
        sans, normal=sans, bold=sans_bold, italic=serif, boldItalic=serif_bold
    )
    pdfmetrics.registerFontFamily(
        mono, normal=mono, bold=mono_bold, italic=mono, boldItalic=mono_bold
    )

    global MONO_FONT_NAME
    MONO_FONT_NAME = mono

    if svg2rlg is not None and "Sans" in found:
        try:  # кириллица внутри SVG: свой набор шрифтов DocSvgSans[-Bold]
            from svglib import fonts as svg_fonts

            fmap = svg_fonts.get_global_font_map()
            fmap.register_font("DocSvgSans", font_path=found["Sans"], rlgFontName="DocSvgSans")
            fmap.register_font(
                "DocSvgSans",
                font_path=found["Sans-Bold"],
                weight="bold",
                rlgFontName="DocSvgSans-Bold",
            )
            import svglib.svglib as _svg_mod

            _svg_mod.DEFAULT_FONT_NAME = "DocSvgSans"
        except Exception as exc:  # pragma: no cover
            print(f"  ! не удалось настроить шрифты SVG: {exc}", file=sys.stderr)

    return {"sans": sans, "sans_bold": sans_bold, "mono": mono, "serif": serif}


def build_styles(fonts: dict) -> StyleSheet1:
    sans, bold, mono, serif = (
        fonts["sans"],
        fonts["sans_bold"],
        fonts["mono"],
        fonts["serif"],
    )
    ss = StyleSheet1()

    for level, (size, lead) in enumerate(
        [(17, 21), (14, 18), (12, 15.5), (10.8, 14), (10.2, 13.2), (9.8, 12.6)],
        start=1,
    ):
        ss.add(
            ParagraphStyle(
                name=f"Heading{level}",
                fontName=bold,
                fontSize=size,
                leading=lead,
                textColor=ACCENT if level <= 3 else colors.HexColor("#2f4f6f"),
                spaceBefore=13 if level <= 2 else 9,
                spaceAfter=5,
                keepWithNext=1,
            )
        )
    ss.add(
        ParagraphStyle(
            name="Body",
            fontName=sans,
            fontSize=9.7,
            leading=13.6,
            alignment=TA_JUSTIFY,
            spaceAfter=6,
        )
    )
    ss.add(
        ParagraphStyle(
            name="ListItem",
            parent=ss["Body"],
            alignment=TA_LEFT,
            spaceAfter=2.5,
            spaceBefore=0.5,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Code",
            fontName=mono,
            fontSize=8,
            leading=10,
            backColor=CODE_BG,
            borderColor=CODE_BORDER,
            borderWidth=0.7,
            borderPadding=6,
            spaceBefore=5,
            spaceAfter=9,
        )
    )
    ss.add(
        ParagraphStyle(
            name="TableCell",
            fontName=sans,
            fontSize=8.4,
            leading=10.8,
            alignment=TA_LEFT,
        )
    )
    ss.add(
        ParagraphStyle(
            name="TableHead",
            parent=ss["TableCell"],
            fontName=bold,
            textColor=colors.white,
        )
    )
    ss.add(
        ParagraphStyle(
            name="Missing",
            fontName=sans,
            fontSize=8.4,
            leading=11,
            alignment=TA_CENTER,
            textColor=GRAY,
        )
    )
    return ss


# --------------------------------------------------------------------------- #
# Инлайн-разметка: Markdown -> разметка reportlab
# --------------------------------------------------------------------------- #


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_angles(chunk: str) -> str:
    return chunk.replace("<", "&lt;").replace(">", "&gt;")


def _tex_group(text: str, pos: int) -> tuple:
    """Аргумент макроса с позиции pos: {…} с вложенностью, макрос или один знак."""
    while pos < len(text) and text[pos] == " ":
        pos += 1
    if pos >= len(text):
        return "", pos
    if text[pos] == "\\":
        macro = re.match(r"\\(?:[A-Za-z]+|.)", text[pos:])
        return macro.group(0), pos + macro.end()
    if text[pos] != "{":
        return text[pos], pos + 1
    depth, i = 0, pos
    while i < len(text):
        if text[i] == "\\":  # экранированный знак группу не открывает и не закрывает
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i + 1
        i += 1
    return text[pos + 1 :], len(text)


def _tex_plain(text: str, scripts: bool = True) -> str:
    """Формула без служебных знаков разметки; scripts=False — ещё и без индексов."""
    if not scripts:
        text = re.sub("[\ue004\ue006][^\ue005\ue007]*[\ue005\ue007]", "", text)
    return re.sub("[\ue000-\ue008]", "", text).strip()


def _tex_fraction(num: str, den: str, tail: str) -> str:
    """a/b для простых частей, (a b) / (c d) для составных.

    Если сразу за простой дробью идёт множитель, она берётся в скобки:
    «(1/2)√(…)» читается однозначно, «1/2√(…)» — нет.
    """
    parts = (_tex_plain(num, scripts=False), _tex_plain(den, scripts=False))
    simple = all(part and not re.search(r"[\s+\-−×·/=]", part) for part in parts)
    if not simple:
        return f"({num.strip()}) / ({den.strip()})"
    macro = re.match(r"\\([A-Za-z]+)", tail)
    if macro:  # множитель-макрос: корень, дробь, текст, греческая буква
        name = macro.group(1)
        glued = (
            name in ("sqrt", "frac", "dfrac", "tfrac")
            or name in MATH_UPRIGHT
            or name in MATH_BOLD
            or MATH_SYMBOLS.get(name, "").isalpha()
        )
    else:  # множитель — буква, цифра или открывающая скобка
        glued = bool(re.match(r"[^\W_]|[(\[]", tail))
    return f"({num}/{den})" if glued else f"{num}/{den}"


def _tex(text: str) -> str:
    """Подмножество LaTeX -> текст со служебными знаками разметки."""
    out, pos = [], 0
    while pos < len(text):
        ch = text[pos]
        if ch in "_^":
            body, pos = _tex_group(text, pos + 1)
            inner = _tex(body)
            if ch == "^" and _tex_plain(inner) == "°":
                out.append("°")
            elif ch == "_":
                out.append(_M_SUB + inner + _M_SUBE)
            else:
                out.append(_M_SUP + inner + _M_SUPE)
            continue
        if ch in "{}":  # группирующие скобки без макроса не печатаются
            pos += 1
            continue
        if ch != "\\":
            out.append(ch)
            pos += 1
            continue
        macro = re.match(r"\\([A-Za-z]+)", text[pos:])
        if not macro:
            # «\,» — неразрывный пробел, «\;» «\:» «\ » — пробел, «\!» — ничего;
            # «\_» «\%» «\{» «\}» — сам знак
            mark = text[pos + 1 : pos + 2]
            if mark == ",":
                out.append("\u00a0")
            elif mark in (";", ":", " "):
                out.append(" ")
            elif mark != "!":
                out.append(mark)
            pos += 2
            continue
        name = macro.group(1)
        pos += macro.end()
        if name in MATH_UPRIGHT:
            body, pos = _tex_group(text, pos)
            out.append(_M_UP + body + _M_DOWN)
        elif name in MATH_BOLD:
            body, pos = _tex_group(text, pos)
            out.append(_M_B + _tex(body) + _M_BE)
        elif name in ("frac", "dfrac", "tfrac"):
            num, pos = _tex_group(text, pos)
            den, pos = _tex_group(text, pos)
            out.append(_tex_fraction(_tex(num), _tex(den), text[pos:]))
        elif name == "sqrt":
            body, pos = _tex_group(text, pos)
            out.append("√(" + _tex(body).strip() + ")")
        elif name in MATH_FUNCTIONS:
            out.append(_M_UP + name + _M_DOWN)
        elif name in MATH_SYMBOLS:
            out.append(MATH_SYMBOLS[name])
        else:  # неизвестный макрос: печатается имя, обратная косая в PDF не нужна
            out.append(name)
    return "".join(out)


def _math_markup(raw: str) -> str:
    """$z_2 = 150$ -> <i>z</i><sub><i>2</i></sub><i> = 150</i>; дроби, корни, \\text.

    Курсивом набирается каждый кусок текста отдельно: так теги всегда вложены
    правильно, даже когда прямой текст \\text{…} стоит внутри \\mathbf{…}.
    """
    text = re.sub(r"[ \t]+", " ", _tex(raw.strip()))
    # пробел перед \text{ …} и пробел внутри него — это один пробел, а не два
    text = re.sub(" (?=[\ue000-\ue007]* )", "", text)
    out, upright = [], 0
    for chunk in re.split("([\ue000-\ue007])", text):
        if not chunk:
            continue
        if chunk == _M_UP:
            upright += 1
        elif chunk == _M_DOWN:
            upright -= 1
        elif chunk in _M_TAGS:
            out.append(_M_TAGS[chunk])
        elif upright or not chunk.strip():
            out.append(_escape(chunk))
        else:
            out.append(f"<i>{_escape(chunk)}</i>")
    return "".join(out)


def _protect_markup(text: str) -> str:
    """Экранирует «голые» &, <, >, не трогая уже проставленные теги."""
    known = {"amp", "lt", "gt", "quot", "apos"}
    text = re.sub(
        r"&([A-Za-z#][A-Za-z0-9]*);",
        lambda m: m.group(0) if m.group(1) in known or m.group(1).startswith("#") else "&amp;",
        text,
    )
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#[0-9]+;|#x[0-9A-Fa-f]+;)", "&amp;", text)

    out, pos = [], 0
    for match in re.finditer(r"</?[a-zA-Z][^>]*>", text):
        out.append(_escape_angles(text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape_angles(text[pos:]))
    return "".join(out)


def inline(text: str) -> str:
    """Строка Markdown -> строка разметки для Paragraph."""

    def repl(match: re.Match) -> str:
        if match.group("code"):
            return f'<font face="__MONO__">{_escape(match.group("code_text"))}</font>'
        if match.group("image"):
            alt = match.group("img_alt").strip()
            return _escape(alt) if alt else ""
        if match.group("link"):
            label = inline(match.group("link_text"))
            href = match.group("link_href").replace("&", "&amp;")
            return f'<link href="{href}" color="#1a5fb4"><u>{label}</u></link>'
        if match.group("bold"):
            body = match.group("bold_text") or match.group("bold_text2") or ""
            return f"<b>{inline(body)}</b>"
        if match.group("italic"):
            body = match.group("italic_text") or match.group("italic_text2") or ""
            return f"<i>{inline(body)}</i>"
        if match.group("dmath"):  # выключная формула — на отдельной строке
            return _M_BREAK + _math_markup(match.group("dmath_text")) + _M_BREAK
        if match.group("math"):
            return _math_markup(match.group("math_text"))
        return _escape(match.group(0))

    out = _protect_markup(INLINE_RE.sub(repl, text))
    # reportlab понимает только закрытую форму переноса строки
    out = BR_RE.sub("<br/>", out)
    # вокруг выключных формул — по одному переносу, по краям абзаца — ни одного
    out = re.sub(rf"\s*{_M_BREAK}(?:\s*{_M_BREAK})*\s*", _M_BREAK, out).strip(_M_BREAK)
    out = out.replace(_M_BREAK, "<br/>")
    return out.replace("__MONO__", MONO_FONT_NAME)


# --------------------------------------------------------------------------- #
# Изображения
# --------------------------------------------------------------------------- #


def _placeholder(text: str, width: float, ss: StyleSheet1) -> Table:
    box = Paragraph(text, ss["Missing"])
    table = Table([[box]], colWidths=[width], rowHeights=[44])
    table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, CODE_BORDER),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


class _DrawingWrapper(platypus.Flowable):
    """Обёртка над Drawing из svglib (чтобы вставить SVG как flowable)."""

    def __init__(self, drawing):
        super().__init__()
        self.drawing = drawing
        self.width = drawing.width
        self.height = drawing.height

    def wrap(self, avail_width, avail_height):
        return self.width, self.height

    def draw(self):
        self.drawing.drawOn(self.canv, 0, 0)


def _svg_with_background(svg_text: str) -> str:
    """Фон из style="background:…" корневого <svg> превращает в прямоугольник.

    Браузер закрашивает таким фоном всю картинку, svglib его не видит — и
    светлые подписи тёмных схем оказываются на белой бумаге. Прямоугольник на
    весь viewBox возвращает схеме задуманный вид.
    """
    root = re.search(r"<svg\b[^>]*>", svg_text)
    if not root:
        return svg_text
    tag = root.group(0)
    color = re.search(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})", tag)
    box = re.search(r'viewBox="\s*([-\d.]+)[\s,]+([-\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"', tag)
    if not color or not box:
        return svg_text
    x, y, width, height = box.groups()
    rect = f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="{color.group(1)}"/>'
    return svg_text[: root.end()] + rect + svg_text[root.end() :]


def image_flowable(src: str, base_dir: str, max_width: float, ss: StyleSheet1):
    """Картинка по пути из Markdown. Возвращает flowable (или заглушку)."""
    path = src if os.path.isabs(src) else os.path.normpath(os.path.join(base_dir, src))
    if not os.path.isfile(path):
        return _placeholder(f"Изображение недоступно: {_escape(src)}", max_width, ss)

    try:
        if path.lower().endswith(".svg"):
            if svg2rlg is None:
                raise RuntimeError("не установлен svglib")
            # svglib не наследует font-family со <svg> — проставляем её на <text>
            with open(path, encoding="utf-8") as handle:
                svg_text = handle.read()
            patched = re.sub(
                r"<text\b((?:(?!font-family=)[^>])*?)>",
                r'<text font-family="DocSvgSans"\1>',
                svg_text,
            )
            patched = _svg_with_background(patched)
            tmp_path = path
            if patched != svg_text:
                tmp_path = path + ".svgfont.tmp"
                with open(tmp_path, "w", encoding="utf-8") as handle:
                    handle.write(patched)
            try:
                drawing = svg2rlg(tmp_path)
            finally:
                if tmp_path != path and os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            if drawing is None:
                raise RuntimeError("svglib не разобрал файл")
            scale = min(1.4, max_width / drawing.width) if drawing.width else 1.0
            drawing.scale(scale, scale)
            drawing.width *= scale
            drawing.height *= scale
            return _DrawingWrapper(drawing)
        if PILImage is None:
            raise RuntimeError("не установлен Pillow")
        with PILImage.open(path) as img:
            width, height = img.size
        if width <= 0 or height <= 0:
            raise RuntimeError("нулевой размер изображения")
        target_w = min(float(width), max_width)
        return RLImage(path, width=target_w, height=height * target_w / width)
    except Exception as exc:
        return _placeholder(
            f"Не удалось вставить {_escape(src)}: {_escape(str(exc))}", max_width, ss
        )


# --------------------------------------------------------------------------- #
# Таблицы GFM
# --------------------------------------------------------------------------- #


def split_row(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    # Экранируется только вертикальная черта. Остальные обратные косые
    # остаются в тексте: они нужны формулам вида $\\ge 300\\times 250$.
    cells, buf, pos = [], [], 0
    while pos < len(line):
        ch = line[pos]
        if ch == "\\" and line[pos + 1 : pos + 2] == "|":
            buf.append("|")
            pos += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        pos += 1
    cells.append("".join(buf).strip())
    return cells


def alignments(sep_row: str):
    result = []
    for cell in split_row(sep_row):
        left, right = cell.startswith(":"), cell.endswith(":")
        result.append("CENTER" if left and right else "RIGHT" if right else "LEFT")
    return result


def _token_metrics(text: str, font_name: str, bold_name: str | None = None) -> tuple:
    """(ширина самого широкого неделимого токена, ширина самой длинной строки).

    Токены в `обратных кавычках` считаются неделимыми и считаются
    моноширинным шрифтом; ссылки сводим к видимому тексту, картинки не
    считаем. Неразрывный пробел не делит токен: «12 345,67 ₽» переносить
    нельзя, и reportlab такую строку тоже не рвёт. <br> делит ячейку на строки.
    Ячейка с **жирным** текстом меряется жирным шрифтом: он шире обычного.
    """
    text = IMAGE_RE.sub("", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    if bold_name and "**" in text:
        font_name = bold_name
    space = pdfmetrics.stringWidth(" ", font_name, 8.4)
    max_tok, longest = 0.0, 0.0
    for line in BR_RE.split(text):
        total, count = 0.0, 0
        for match in re.finditer(r"`([^`]+)`|([^ \t\n\r]+)", line):
            if match.group(1) is not None:
                # имя в коде печатается целиком: LOCK_SERVO, а не LOCKSERVO
                token, font = match.group(1), MONO_FONT_NAME
            else:
                # знаки разметки Markdown не печатаются
                token, font = re.sub(r"[*$]", "", match.group(2)).strip("_"), font_name
            if not token:
                continue
            width = pdfmetrics.stringWidth(token, font, 8.4)
            max_tok = max(max_tok, width)
            total += width
            count += 1
        longest = max(longest, total + space * max(count - 1, 0))
    return max_tok, longest


#: Ширина картинки в ячейке: обычная и миниатюра для широких таблиц.
CELL_IMAGE_WIDTH = 118
THUMBNAIL_WIDTH = 54
THUMBNAIL_FROM_COLUMNS = 6


def _cell_image_width(n_cols: int) -> float:
    return THUMBNAIL_WIDTH if n_cols >= THUMBNAIL_FROM_COLUMNS else CELL_IMAGE_WIDTH


#: Поля ячейки слева и справа: обычные и сжатые — для таблиц, которым не
#: хватает ширины страницы на самые длинные слова.
CELL_PAD = 4.0
CELL_PAD_TIGHT = 2.5


def _column_limits(rows, avail_width: float, sans: str, bold: str, pad: float) -> tuple:
    """Для каждой колонки: минимум, желаемая ширина и признак «только картинки»."""
    n_cols = len(rows[0])
    image_width = _cell_image_width(n_cols)
    extra = 2 * pad + 2  # поля ячейки и небольшой запас на погрешность измерения
    mins, desired, fixed = [], [], []
    for col in range(n_cols):
        body = [rows[i][col] for i in range(1, len(rows))] or [rows[0][col]]
        metrics = [_token_metrics(c, sans, bold) for c in body]
        head_max, head_total = _token_metrics(rows[0][col], bold)
        col_min = max([m[0] for m in metrics] + [head_max]) + extra
        col_sum = max([m[1] for m in metrics] + [head_total]) + extra
        has_image = any(IMAGE_RE.search(r[col]) for r in rows)
        only_images = has_image and all(not IMAGE_RE.sub("", c).strip() for c in body)
        if has_image:
            col_min = max(col_min, image_width + 2 * pad)
        col_min = min(col_min, avail_width * 0.62)
        col_sum = max(min(col_sum, avail_width * 0.62), col_min, 20)
        if only_images:
            col_sum = col_min
        mins.append(col_min)
        desired.append(col_sum)
        fixed.append(only_images)
    return mins, desired, fixed


def _distribute(mins, desired, fixed, avail_width: float) -> list:
    """Делит ширину страницы между колонками с учётом их минимумов."""
    n_cols = len(mins)
    if sum(mins) >= avail_width:
        # Не помещаются даже минимумы. Сжимаются только самые широкие колонки:
        # в них длинные неделимые строки вроде путей к файлам, и reportlab
        # перенесёт их по знакам. Колонки с обычными словами остаются при своей
        # ширине, чтобы слова в них не рвались.
        widths, rest = [0.0] * n_cols, avail_width
        order = sorted(range(n_cols), key=lambda i: mins[i])
        for rank, col in enumerate(order):
            widths[col] = min(mins[col], rest / (n_cols - rank))
            rest -= widths[col]
        return widths

    slack = avail_width - sum(mins)
    wants = [0.0 if fixed[i] else desired[i] - mins[i] for i in range(n_cols)]
    if sum(wants) <= slack:
        # всем хватает места на текст в одну строку; остаток — гибким колонкам
        widths = [mins[i] + wants[i] for i in range(n_cols)]
        rest = avail_width - sum(widths)
        flexible = [i for i in range(n_cols) if not fixed[i]] or list(range(n_cols))
        base = sum(widths[i] for i in flexible) or 1.0
        for i in flexible:
            widths[i] += rest * widths[i] / base
        return widths
    return [mins[i] + slack * wants[i] / sum(wants) for i in range(n_cols)]


def _column_widths(rows, avail_width: float, sans: str, bold: str | None = None) -> tuple:
    """(ширины колонок, поле ячейки): каждой колонке — не меньше её самого длинного слова.

    Сначала колонка получает свой минимум, затем свободное место делится
    пропорционально тому, сколько колонке не хватает до текста в одну строку.
    Колонка, в которой только картинки, остаётся шириной в картинку. Если
    минимумы не помещаются, сначала сжимаются поля ячеек, и только потом
    уступают самые широкие колонки.
    """
    bold = bold or sans
    for pad in (CELL_PAD, CELL_PAD_TIGHT):
        mins, desired, fixed = _column_limits(rows, avail_width, sans, bold, pad)
        if sum(mins) <= avail_width:
            break
    return _distribute(mins, desired, fixed, avail_width), pad


def _aligned_style(style: ParagraphStyle, align: str) -> ParagraphStyle:
    if ALIGN_MAP.get(align, TA_LEFT) == style.alignment:
        return style
    return ParagraphStyle(
        name=f"{style.name}-{align}", parent=style, alignment=ALIGN_MAP.get(align, TA_LEFT)
    )


def _cell_flowables(
    raw: str, inner_w: float, base_dir: str, ss: StyleSheet1, header: bool, image_w: float
):
    if not IMAGE_RE.search(raw):
        style = ss["TableHead"] if header else ss["TableCell"]
        return Paragraph(inline(raw) or "&nbsp;", style)

    parts, pos = [], 0
    for match in IMAGE_RE.finditer(raw):
        before = raw[pos : match.start()].strip()
        if before:
            parts.append(Paragraph(inline(before), ss["TableCell"]))
        flow = image_flowable(match.group("src"), base_dir, min(inner_w, image_w), ss)
        flow.hAlign = "CENTER"
        parts.append(flow)
        pos = match.end()
    after = raw[pos:].strip()
    if after:
        parts.append(Paragraph(inline(after), ss["TableCell"]))
    return parts or [Paragraph("&nbsp;", ss["TableCell"])]


def build_table(rows, aligns, avail_width: float, base_dir: str, ss: StyleSheet1) -> Table:
    n_cols = max(len(r) for r in rows)
    rows = [r + [""] * (n_cols - len(r)) for r in rows]
    aligns = (aligns + ["LEFT"] * n_cols)[:n_cols]
    widths, pad = _column_widths(
        rows, avail_width, ss["TableCell"].fontName, ss["TableHead"].fontName
    )
    image_w = _cell_image_width(n_cols)

    data = [
        [
            _cell_flowables(row[c], widths[c] - 2 * pad, base_dir, ss, r == 0, image_w)
            for c in range(n_cols)
        ]
        for r, row in enumerate(rows)
    ]
    # выравнивание по :---: применяем к абзацам ячеек (картинки центрируются сами)
    for r in range(len(data)):
        for c in range(n_cols):
            cell = data[r][c]
            if isinstance(cell, Paragraph):
                data[r][c] = Paragraph(cell.text, _aligned_style(cell.style, aligns[c]))

    table = Table(data, colWidths=widths, repeatRows=1, hAlign="CENTER")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9fb3c8")),
                ("LEFTPADDING", (0, 0), (-1, -1), pad),
                ("RIGHTPADDING", (0, 0), (-1, -1), pad),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f2f6fa")],
                ),
            ]
        )
    )
    table.spaceBefore = 4
    table.spaceAfter = 10
    return table


# --------------------------------------------------------------------------- #
# Разбор блоков Markdown
# --------------------------------------------------------------------------- #


def parse_markdown(text: str, base_dir: str, ss: StyleSheet1, avail_width: float):
    lines = text.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    return _blocks(lines, 0, len(lines), base_dir, ss, avail_width)


#: Доля кегля, которую занимает знак моноширинного шрифта DejaVu Sans Mono.
MONO_ADVANCE = 0.602
CODE_SIZE_MAX, CODE_SIZE_MIN, CODE_SIZE_WRAPPED = 8.2, 5.4, 7.0


def _wrap_code_line(line: str, limit: int) -> list:
    """Строка кода длиннее limit знаков -> несколько строк.

    Перенос ставится по пробелу; продолжение сдвигается на четыре знака
    относительно начала строки, чтобы его было видно.
    """
    if len(line) <= limit:
        return [line]
    indent = len(line) - len(line.lstrip(" "))
    prefix = " " * min(indent + 4, limit // 2)
    out, rest = [], line
    while len(rest) > limit:
        cut = rest.rfind(" ", len(prefix) + 1, limit + 1)
        if cut <= len(prefix):  # пробела нет — режем по ширине
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = prefix + rest[cut:].lstrip()
    out.append(rest)
    return out


def _code_block(body, ss: StyleSheet1, avail_width: float) -> XPreformatted:
    """Fenced-блок: кегль подбирается так, чтобы самые длинные строки влезали.

    Если строка не влезает даже самым мелким кеглем, блок набирается читаемым
    кеглем, а длинные строки переносятся: иначе они уходят за поле страницы.
    """
    lines = "\n".join(body).rstrip("\n").split("\n")
    longest = max((len(ln) for ln in lines), default=1)
    room = avail_width - 20
    size = min(CODE_SIZE_MAX, room / max(longest * MONO_ADVANCE, 1.0))
    if size < CODE_SIZE_MIN:
        size = CODE_SIZE_WRAPPED
        limit = int(room / (MONO_ADVANCE * size))
        lines = [part for ln in lines for part in _wrap_code_line(ln, limit)]
    text = "\n".join(lines)
    style = ParagraphStyle(
        name="CodeAuto", parent=ss["Code"], fontSize=size, leading=size * 1.28
    )
    return XPreformatted(_escape(text), style)


def _list_flows(items, pos, base_dir, ss: StyleSheet1):
    """Список -> абзацы с маркерами (вложенность через отступ)."""
    flows = []
    bullets = ["\u2022", "\u2013", "\u25e6", "\u00b7"]
    stack = []  # [отступ, нумерованный?, текущий номер]
    while pos < len(items):
        ind, marker, content = items[pos]
        pos += 1
        while stack and ind < stack[-1][0]:
            stack.pop()
        if not stack or ind > stack[-1][0]:
            ordered = bool(re.match(r"\d", marker))
            stack.append([ind, ordered, int(re.match(r"\d+", marker).group(0)) if ordered else 0])
        elif stack[-1][1]:
            stack[-1][2] += 1

        depth = len(stack) - 1
        bullet = f"{stack[-1][2]}." if stack[-1][1] else bullets[min(depth, len(bullets) - 1)]
        style = ParagraphStyle(
            name=f"ListItem-d{depth}",
            parent=ss["ListItem"],
            leftIndent=15 + 14 * depth,
            firstLineIndent=-11,
            bulletIndent=2 + 14 * depth,
        )
        flows.append(Paragraph(inline(" ".join(content)), style, bulletText=bullet))
    return flows, pos


def _blocks(lines, start, end, base_dir, ss: StyleSheet1, avail_width: float):
    flowables = []
    i = start
    while i < end:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # --- fenced-блок кода
        if FENCE_RE.match(line):
            body = []
            i += 1
            while i < end and not re.match(r"^\s{0,3}(`{3,}|~{3,})\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            flowables.append(_code_block(body, ss, avail_width))
            continue

        # --- заголовок
        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            flowables.append(Paragraph(inline(heading.group(2).strip()), ss[f"Heading{level}"]))
            i += 1
            continue

        # --- горизонтальная линейка
        if HR_RE.match(line):
            flowables.append(
                HRFlowable(
                    width="100%",
                    thickness=0.8,
                    color=colors.HexColor("#b9c6d4"),
                    spaceBefore=6,
                    spaceAfter=8,
                )
            )
            i += 1
            continue

        # --- таблица GFM
        if stripped.startswith("|") and i + 1 < end and TABLE_SEP_RE.match(lines[i + 1]):
            rows = [split_row(line)]
            aligns = alignments(lines[i + 1])
            i += 2
            while i < end and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            flowables.append(build_table(rows, aligns, avail_width, base_dir, ss))
            continue

        # --- цитата
        if stripped.startswith(">"):
            body = []
            while i < end and (lines[i].strip().startswith(">") or (body and stripped)):
                body.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
                if i < end and not lines[i].strip():
                    break
            inner = _blocks(body, 0, len(body), base_dir, ss, avail_width - 20)
            quote = Table([[inner]], colWidths=[avail_width])
            quote.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), QUOTE_BG),
                        ("LINEBEFORE", (0, 0), (0, -1), 3, QUOTE_BAR),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            quote.spaceAfter = 9
            flowables.append(quote)
            continue

        # --- список
        if LIST_RE.match(line):
            items = []
            while i < end:
                match = LIST_RE.match(lines[i])
                if match:
                    items.append(
                        [len(match.group(1)), match.group(2), [match.group(3)]]
                    )
                    i += 1
                    continue
                if lines[i].strip() and items and lines[i].startswith(" "):
                    items[-1][2].append(lines[i].strip())
                    i += 1
                    continue
                break
            list_flows, _ = _list_flows(items, 0, base_dir, ss)
            flowables.extend(list_flows)
            continue

        # --- одиночное изображение
        only_image = IMAGE_RE.fullmatch(stripped)
        if only_image:
            flow = image_flowable(
                only_image.group("src"), base_dir, avail_width * 0.82, ss
            )
            flow.hAlign = "CENTER"
            flowables.append(flow)
            i += 1
            continue

        # --- абзац (склеиваем перенесённые строки)
        para = [stripped]
        i += 1
        while i < end:
            nxt = lines[i]
            if (
                not nxt.strip()
                or HEADING_RE.match(nxt)
                or HR_RE.match(nxt)
                or FENCE_RE.match(nxt)
                or LIST_RE.match(nxt)
                or nxt.strip().startswith("|")
                or nxt.strip().startswith(">")
                or IMAGE_RE.fullmatch(nxt.strip())
            ):
                break
            para.append(nxt.strip())
            i += 1
        flowables.append(Paragraph(inline(" ".join(para)), ss["Body"]))

    return flowables


# --------------------------------------------------------------------------- #
# Шаблон документа: колонтитулы, метаданные, закладки
# --------------------------------------------------------------------------- #


class DocTemplate(BaseDocTemplate):
    def __init__(self, filename, doc_title, source, fonts=None, **kwargs):
        self.doc_title = doc_title
        self.doc_source = source
        self.header_font = (fonts or {}).get("sans", "Helvetica")
        self._bookmarks = count(1)
        super().__init__(filename, title=doc_title, **kwargs)
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates(PageTemplate(id="page", frames=[frame], onPage=self._decorate))

    def _decorate(self, canv, doc):
        canv.saveState()
        width, height = A4
        if canv.getPageNumber() > 1:
            canv.setFont(self.header_font, 7.6)
            canv.setFillColor(GRAY)
            canv.drawString(doc.leftMargin, height - 13 * mm, self.doc_title[:92])
            canv.setStrokeColor(colors.HexColor("#d7dee6"))
            canv.setLineWidth(0.5)
            canv.line(doc.leftMargin, height - 14.5 * mm, width - doc.rightMargin, height - 14.5 * mm)
        canv.setStrokeColor(colors.HexColor("#d7dee6"))
        canv.setLineWidth(0.5)
        canv.line(doc.leftMargin, 13 * mm, width - doc.rightMargin, 13 * mm)
        canv.setFont(self.header_font, 7.6)
        canv.setFillColor(GRAY)
        canv.drawString(doc.leftMargin, 9.5 * mm, self.doc_source)
        canv.drawRightString(width - doc.rightMargin, 9.5 * mm, f"Стр. {canv.getPageNumber()}")
        canv.restoreState()

    def afterFlowable(self, flowable):
        if not isinstance(flowable, Paragraph):
            return
        match = re.match(r"Heading([1-6])$", flowable.style.name)
        if not match:
            return
        level = min(int(match.group(1)) - 1, 4)
        key = f"h{next(self._bookmarks)}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(
            flowable.getPlainText()[:120], key, level=level, closed=level > 0
        )


def convert(md_path: str, pdf_path: str, fonts: dict, ss: StyleSheet1) -> int:
    """Собирает PDF рядом с Markdown; возвращает число страниц."""
    with open(md_path, encoding="utf-8") as handle:
        text = handle.read()

    match = re.search(r"^#\s+(.+)$", text, re.M)
    title = match.group(1).strip() if match else os.path.splitext(os.path.basename(md_path))[0]
    title = re.sub(r"[*_`]", "", title)

    doc = DocTemplate(
        pdf_path,
        doc_title=title,
        source=os.path.relpath(
            os.path.abspath(md_path),
            os.path.dirname(os.path.dirname(os.path.abspath(md_path))),
        ),
        fonts=fonts,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        author="rus_slam",
        subject=f"PDF-копия {os.path.basename(md_path)}",
        creator="tools/pdf/md2pdf.py (reportlab)",
    )
    flowables = parse_markdown(
        text, os.path.dirname(os.path.abspath(md_path)), ss, doc.width
    )
    doc.build(flowables)
    return doc.page


def collect_targets(paths):
    targets = []
    for path in paths:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith(".md"):
                    targets.append(os.path.join(path, name))
        elif path.lower().endswith(".md"):
            targets.append(path)
    return targets


def pdf_target(md_path: str, out_dir: str | None) -> str:
    """Куда положить PDF для исходного .md.

    Без ``--out`` PDF ложится в ``docs/pdf/`` для документов из ``docs/`` и рядом
    с .md для остальных каталогов — так готовые PDF не смешиваются с исходниками.
    """
    stem = os.path.splitext(os.path.basename(md_path))[0] + ".pdf"
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        return os.path.join(out_dir, stem)
    md_dir = os.path.abspath(os.path.dirname(md_path) or ".")
    docs_dir = os.path.abspath("docs")
    if md_dir == docs_dir:
        target = os.path.join(docs_dir, "pdf", stem)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target
    return os.path.splitext(md_path)[0] + ".pdf"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Markdown -> PDF: дублирует .md-документацию в .pdf"
    )
    parser.add_argument(
        "paths", nargs="*", default=["docs"], help="каталоги или файлы .md (по умолчанию docs)"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="каталог для PDF (по умолчанию docs/pdf для docs/, рядом с .md для остальных)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="не собирать, а проверить, что PDF существует и новее исходного .md",
    )
    args = parser.parse_args(argv)

    targets = collect_targets(args.paths or ["docs"])
    if not targets:
        print("Markdown-файлы не найдены.", file=sys.stderr)
        return 1

    if args.check:
        stale = []
        for md in targets:
            pdf = pdf_target(md, args.out)
            if not os.path.isfile(pdf) or os.path.getmtime(pdf) < os.path.getmtime(md):
                stale.append(md)
        if stale:
            print("PDF не актуальны: " + ", ".join(stale))
            return 1
        print(f"PDF актуальны для {len(targets)} файл(ов).")
        return 0

    fonts = register_fonts()
    ss = build_styles(fonts)

    failed = 0
    for md in targets:
        pdf = pdf_target(md, args.out)
        try:
            pages = convert(md, pdf, fonts, ss)
            size_kb = os.path.getsize(pdf) / 1024
            print(f"  {md} -> {pdf}  ({pages} стр., {size_kb:.0f} КБ)")
        except Exception as exc:
            failed += 1
            print(f"  ! {md}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
