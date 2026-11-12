# -*- coding: utf-8 -*-
"""Проверки сборщика PDF: то, что в PDF ломается незаметно.

Запуск:
    python3 -m venv .venv && .venv/bin/pip install pytest -r tools/pdf/requirements.txt
    .venv/bin/python -m pytest tools/pdf -q

Без reportlab проверки пропускаются.
"""
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("reportlab")

from reportlab.platypus import Paragraph  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import md2pdf  # noqa: E402

DOCS = sorted((ROOT / "docs").glob("*.md"))
PAGE_WIDTH = 493.0  # ширина полосы набора A4 при полях 18 мм, пункты


@pytest.fixture(scope="module")
def fonts():
    return md2pdf.register_fonts()


@pytest.fixture(scope="module")
def styles(fonts):
    return md2pdf.build_styles(fonts)


@pytest.fixture(scope="module")
def sans(styles):
    return styles["TableCell"].fontName


@pytest.fixture(scope="module")
def bold(styles):
    return styles["TableHead"].fontName


def _visible(markup: str) -> str:
    """Разметка reportlab -> видимый текст; индексы помечаются как _[…] и ^[…]."""
    text = markup.replace("<sub>", "_[").replace("</sub>", "]")
    text = text.replace("<super>", "^[").replace("</super>", "]")
    text = re.sub(r"</?[a-z]+[^>]*>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# --------------------------------------------------------------------------- #
# Комплект PDF
# --------------------------------------------------------------------------- #


def test_docs_exist():
    assert len(DOCS) >= 12


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_document_has_a_pdf(path):
    pdf = ROOT / "docs" / "pdf" / (path.stem + ".pdf")
    assert pdf.is_file(), f"нет PDF-копии: {pdf.relative_to(ROOT)}"
    assert pdf.read_bytes()[:5] == b"%PDF-"


def test_pdf_target_is_the_pdf_directory():
    target = md2pdf.pdf_target(str(ROOT / "docs" / "COST_ESTIMATE.md"), None)
    assert Path(target) == ROOT / "docs" / "pdf" / "COST_ESTIMATE.pdf"


# --------------------------------------------------------------------------- #
# Строки таблиц и переносы
# --------------------------------------------------------------------------- #


def test_split_row_unescapes_only_the_pipe():
    row = md2pdf.split_row(r"| a \| b | $\ge 300\times 250$ | c |")
    assert row == ["a | b", r"$\ge 300\times 250$", "c"]


def test_br_is_closed_for_reportlab(styles):
    markup = md2pdf.inline("первая<br>вторая<BR />третья")
    assert markup == "первая<br/>вторая<br/>третья"
    Paragraph(markup, styles["TableCell"])  # незакрытый <br> reportlab не принимает


def test_dollar_between_spaces_is_not_a_formula():
    assert md2pdf.inline("цена 5 $ и 7 $ за штуку") == "цена 5 $ и 7 $ за штуку"


# --------------------------------------------------------------------------- #
# Формулы
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source, expected",
    [
        (r"z_2 = 150", "z_[2] = 150"),
        (r"\ge 300\times 250", "≥ 300× 250"),
        (r"1.8^\circ", "1.8°"),
        (r"i = \frac{z_2}{z_1} = \frac{150}{20} = 7.5", "i = z_[2]/z_[1] = 150/20 = 7.5"),
        (r"1.9 \text{ Н}\cdot\text{м}", "1.9 Н·м"),
        (r"M_{turn\_resist} \approx \frac{2}{3} \cdot \mu", "M_[turn_resist] ≈ 2/3 · μ"),
        (
            r"4 \times \frac{11.5 \text{ Н}\cdot\text{м}}{0.108 \text{ м}}",
            "4 × (11.5 Н·м) / (0.108 м)",
        ),
        (r"\frac{1}{2}\sqrt{L^2 + W^2}", "(1/2)√(L^[2] + W^[2])"),
        (r"\arctan\left(\frac{V_{y, i}}{V_{x, i}}\right)", "arctan(V_[y, i]/V_[x, i])"),
        (r"48\,000 \text{ шагов}", "48\u00a0000 шагов"),
        (r"i \in \{1, 2\}", "i ∈ {1, 2}"),
        (r"14–17\%", "14–17%"),
        (r"a < b", "a < b"),
    ],
)
def test_formula_text(source, expected):
    assert _visible(md2pdf._math_markup(source)) == expected


def test_upright_text_inside_bold_is_nested_correctly(styles):
    markup = md2pdf._math_markup(r"4 \times 350 = \mathbf{1400 \text{ Вт}}")
    assert markup == "<i>4 × 350 = </i><b><i>1400</i> Вт</b>"
    Paragraph(markup, styles["Body"])  # неверную вложенность тегов reportlab не принимает


def test_display_formula_goes_on_its_own_line():
    markup = md2pdf.inline(r"Отношение: $$i = 7.5$$ $$b = 2$$ Далее $x_1$.")
    assert _visible(markup.replace("<br/>", "⏎")) == "Отношение:⏎i = 7.5⏎b = 2⏎Далее x_[1]."
    assert md2pdf.inline(r"$$b = 2$$") == "<i>b = 2</i>"


def _formulas(text: str):
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # код формул не содержит
    text = re.sub(r"`[^`\n]*`", "", text)
    for match in re.finditer(r"\$\$([^$\n]+?)\$\$|\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)", text):
        yield match.group(1) or match.group(2)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_formula_of_the_documents_is_typeset(path, styles):
    """В PDF не остаётся команд LaTeX, а разметка принимается reportlab."""
    for formula in _formulas(path.read_text(encoding="utf-8")):
        markup = md2pdf._math_markup(formula)
        Paragraph(markup, styles["TableCell"])
        visible = _visible(markup)
        assert "\\" not in visible, f"{path.name}: в формуле осталась команда: {formula}"
        assert "$" not in visible


# --------------------------------------------------------------------------- #
# Ширины колонок
# --------------------------------------------------------------------------- #


def test_code_names_are_measured_with_their_underscores(sans, bold):
    with_underscore, _ = md2pdf._token_metrics("`LOCK_SERVO`", sans, bold)
    without, _ = md2pdf._token_metrics("`LOCKSERVO`", sans, bold)
    assert with_underscore > without


def test_non_breaking_space_keeps_an_amount_in_one_token(sans):
    joined, _ = md2pdf._token_metrics("369\u00a0170,85\u00a0₽", sans)
    split, _ = md2pdf._token_metrics("369 170,85 ₽", sans)
    assert joined > split


def test_bold_cells_are_measured_with_the_bold_font(sans, bold):
    if sans == bold:
        pytest.skip("полужирный шрифт не найден")
    regular, _ = md2pdf._token_metrics("Комплектующие", sans, bold)
    strong, _ = md2pdf._token_metrics("**Комплектующие**", sans, bold)
    assert strong > regular


def test_br_splits_a_cell_into_lines(sans):
    _, one_line = md2pdf._token_metrics("среднее из 2: 980 – 1 309", sans)
    _, two_lines = md2pdf._token_metrics("среднее из 2:<br>980 – 1 309", sans)
    assert two_lines < one_line


def test_every_column_fits_its_longest_word(sans, bold):
    rows = [
        ["Статья", "Что входит", "Сумма,\u00a0₽"],
        ["**Поворотно-тяговые модули**", "4 мотор-колеса, 4 ступицы, " * 6, "83\u00a0333,20"],
    ]
    widths, pad = md2pdf._column_widths(rows, PAGE_WIDTH, sans, bold)
    assert pad == md2pdf.CELL_PAD
    assert sum(widths) == pytest.approx(PAGE_WIDTH)
    for col, width in enumerate(widths):
        longest = max(md2pdf._token_metrics(row[col], sans, bold)[0] for row in rows)
        assert width - 2 * pad >= longest


def test_tight_padding_is_tried_before_words_are_broken(sans, bold):
    word = "Грузоподъемность"
    token, _ = md2pdf._token_metrics(word, sans, bold)
    n_cols = 5
    # ширина, при которой слова помещаются только со сжатыми полями ячеек
    width = n_cols * (token + 2 * md2pdf.CELL_PAD_TIGHT + 2) + 1
    rows = [[word] * n_cols, [word] * n_cols]
    widths, pad = md2pdf._column_widths(rows, width, sans, bold)
    assert pad == md2pdf.CELL_PAD_TIGHT
    assert all(w - 2 * pad >= token for w in widths)


def test_the_widest_columns_give_way_when_nothing_fits(sans, bold):
    path = "`firmware/reference/steering_tested/steering_tested.ino`"
    rows = [["Файл в Zapret", "Файл здесь", "Сравнение"], [path, path, "совпадает"]]
    widths, pad = md2pdf._column_widths(rows, PAGE_WIDTH, sans, bold)
    assert sum(widths) == pytest.approx(PAGE_WIDTH)
    word, _ = md2pdf._token_metrics("Сравнение", bold)
    assert widths[2] - 2 * pad >= word  # короткая колонка слов не рвёт
    assert widths[0] == pytest.approx(widths[1])  # длинные пути делят остаток поровну


def test_image_only_column_stays_as_narrow_as_the_thumbnail(sans, bold):
    header = ["№", "Фото", "Наименование", "Кол-во", "Цена", "Сумма", "USD", "Откуда цена"]
    row = ["1.01", "![x](a.jpg)", "Мотор-колесо " * 8, "4 шт.", "1", "2", "3", "медиана из 4"]
    widths, pad = md2pdf._column_widths([header, row], PAGE_WIDTH, sans, bold)
    assert widths[1] <= md2pdf.THUMBNAIL_WIDTH + 2 * pad + 20
    assert widths[2] == max(widths)


# --------------------------------------------------------------------------- #
# Код и SVG
# --------------------------------------------------------------------------- #


def test_long_code_line_is_wrapped_at_a_space():
    line = "@F " + " ".join(f"<field{i}>" for i in range(30))
    parts = md2pdf._wrap_code_line(line, 112)
    assert len(parts) > 1 and all(len(part) <= 112 for part in parts)
    assert parts[1].startswith("    <")
    assert "".join(part.strip() for part in parts).replace(" ", "") == line.replace(" ", "")
    assert md2pdf._wrap_code_line("короткая строка", 112) == ["короткая строка"]


def test_code_line_without_spaces_is_cut_by_width():
    parts = md2pdf._wrap_code_line("x" * 50, 20)
    assert all(len(part) <= 20 for part in parts)
    assert "".join(part.strip() for part in parts) == "x" * 50


def test_svg_background_becomes_a_rectangle():
    svg = '<svg xmlns="x" viewBox="0 0 950 650" style="background:#0f172a;"><text>t</text></svg>'
    patched = md2pdf._svg_with_background(svg)
    assert '<rect x="0" y="0" width="950" height="650" fill="#0f172a"/><text>' in patched
    plain = '<svg viewBox="0 0 10 10"><g/></svg>'
    assert md2pdf._svg_with_background(plain) == plain


# --------------------------------------------------------------------------- #
# Сборка целиком
# --------------------------------------------------------------------------- #


def test_convert_builds_a_pdf(tmp_path, fonts, styles):
    source = tmp_path / "docs" / "SAMPLE.md"
    source.parent.mkdir()
    source.write_text(
        "# Образец\n\n"
        "Текст с формулой $z_2 = 150$ и суммой 369\u00a0170,85\u00a0₽.\n\n"
        "- Отношение:\n  $$i = \\frac{z_2}{z_1} = 7.5$$\n\n"
        "| Статья | Сумма,\u00a0₽ |\n| :--- | ---: |\n"
        "| **Всего**<br>на один робот | **369\u00a0170,85** |\n\n"
        "```text\n" + "@F " + " ".join(f"<f{i}>" for i in range(60)) + "\n```\n",
        encoding="utf-8",
    )
    target = tmp_path / "SAMPLE.pdf"
    pages = md2pdf.convert(str(source), str(target), fonts, styles)
    assert pages >= 1
    assert target.read_bytes()[:5] == b"%PDF-"
