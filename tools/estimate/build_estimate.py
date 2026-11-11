#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_estimate.py — сборка сметы робота из таблиц каталога docs/estimate/.

Входные данные (правятся руками):
  docs/estimate/parts.csv               состав: позиция, количество, ключ цены;
  docs/estimate/price_observations.csv  найденные предложения: ключ, цена, магазин, ссылка;
  docs/estimate/estimate.json           курс, разделы, прочие затраты, прежняя калькуляция;
  docs/estimate/photo_sources.json      откуда взята каждая фотография.

Результат (руками не правится):
  docs/COST_ESTIMATE.md                     смета с фотографиями, ценами и источниками;
  docs/estimate/cost_estimate_calculated.csv  те же строки с рассчитанными суммами.

Правило цены: медиана предложений с отметкой «да» в колонке use; при двух
предложениях это их среднее. Строки без ключа цены берут manual_price_rub.

Запуск (нужен только Python 3.9+, без сторонних пакетов):
    python3 tools/estimate/build_estimate.py            # пересобрать оба файла
    python3 tools/estimate/build_estimate.py --check    # проверить, что файлы актуальны
    python3 tools/estimate/build_estimate.py --totals   # итоги в JSON, файлы не трогать
    python3 tools/estimate/build_estimate.py --rate 90  # итоги при другом курсе доллара
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EST_DIR = ROOT / "docs" / "estimate"
PARTS_CSV = EST_DIR / "parts.csv"
OBS_CSV = EST_DIR / "price_observations.csv"
CONFIG_JSON = EST_DIR / "estimate.json"
PHOTO_JSON = EST_DIR / "photo_sources.json"
PHOTO_DIR = ROOT / "docs" / "assets" / "estimate"
OUT_MD = ROOT / "docs" / "COST_ESTIMATE.md"
OUT_CSV = EST_DIR / "cost_estimate_calculated.csv"

NBSP = "\u00a0"
CENT = Decimal("0.01")
STATUSES = ("по документам", "допущение", "оценка")
PARTS_FIELDS = (
    "id", "section", "name", "spec", "qty", "unit", "price_key", "manual_price_rub",
    "status", "old_group", "photo", "basis", "note",
)
OBS_FIELDS = ("key", "price_rub", "shop", "url", "offer", "use", "note")


class EstimateError(Exception):
    """Ошибка во входных таблицах сметы."""


# --------------------------------------------------------------------------- #
# Числа и текст
# --------------------------------------------------------------------------- #


def money(value: Decimal) -> Decimal:
    """Округление до копейки (цента) по правилам арифметики."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value, places: int = 2) -> str:
    """Decimal -> «12 345,67»: неразрывный пробел между разрядами, запятая."""
    step = Decimal(1).scaleb(-places)
    number = Decimal(value).quantize(step, rounding=ROUND_HALF_UP)
    sign = "−" if number < 0 else ""
    whole, _, frac = f"{abs(number):f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    text = NBSP.join(groups)
    if places:
        text += "," + frac
    return sign + text


def fmt_qty(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text.replace(".", ",")


UNIT_FORMS = {
    "пара": ("пара", "пары", "пар"),
    "лист": ("лист", "листа", "листов"),
    "банка": ("банка", "банки", "банок"),
}


def fmt_amount(quantity: Decimal, unit: str) -> str:
    """«4 пары», «0,5 м²», «36 шт.»: количество с единицей в нужной форме."""
    forms = UNIT_FORMS.get(unit)
    if forms and quantity == quantity.to_integral_value():
        unit = plural(int(quantity), *forms)
    return f"{fmt_qty(quantity)}{NBSP}{unit}"


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def plural(number: int, one: str, few: str, many: str) -> str:
    """1 позиция, 2 позиции, 5 позиций."""
    tail = number % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def cell(text: str) -> str:
    """Текст для ячейки таблицы Markdown."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


# --------------------------------------------------------------------------- #
# Загрузка и проверка входных таблиц
# --------------------------------------------------------------------------- #


def _read_csv(path: Path, fields) -> list:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise EstimateError(f"{path.name}: ожидались колонки {', '.join(fields)}")
        return list(reader)


def load() -> dict:
    config = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    parts = _read_csv(PARTS_CSV, PARTS_FIELDS)
    offers = _read_csv(OBS_CSV, OBS_FIELDS)
    photos = json.loads(PHOTO_JSON.read_text(encoding="utf-8")) if PHOTO_JSON.exists() else {}

    section_ids = [section["id"] for section in config["sections"]]
    group_ids = [group["id"] for group in config["previous"]["groups"]]

    for offer in offers:
        where = f"{OBS_CSV.name}: {offer['key']} / {offer['shop']}"
        try:
            offer["price"] = Decimal(offer["price_rub"])
        except Exception as exc:
            raise EstimateError(f"{where}: цена {offer['price_rub']!r}") from exc
        if offer["price"] <= 0:
            raise EstimateError(f"{where}: цена должна быть больше нуля")
        if offer["use"] not in ("да", "нет"):
            raise EstimateError(f"{where}: в колонке use допустимы «да» и «нет»")
        if offer["use"] == "нет" and not offer["note"].strip():
            raise EstimateError(f"{where}: для исключённого предложения нужна причина в note")
        url = offer["url"]
        if not url.startswith(("http://", "https://")) or any(ch in url for ch in " )|\t"):
            raise EstimateError(f"{where}: ссылка не годится для Markdown: {url}")
        if not offer["shop"].strip() or not offer["offer"].strip():
            raise EstimateError(f"{where}: пустой магазин или описание")

    seen = set()
    for part in parts:
        where = f"{PARTS_CSV.name}: {part['id']}"
        if part["id"] in seen:
            raise EstimateError(f"{where}: номер повторяется")
        seen.add(part["id"])
        if part["section"] not in section_ids:
            raise EstimateError(f"{where}: неизвестный раздел {part['section']}")
        if part["old_group"] not in group_ids:
            raise EstimateError(f"{where}: неизвестная статья прежней калькуляции {part['old_group']}")
        if part["status"] not in STATUSES:
            raise EstimateError(f"{where}: статус должен быть одним из: {', '.join(STATUSES)}")
        try:
            part["quantity"] = Decimal(part["qty"])
        except Exception as exc:
            raise EstimateError(f"{where}: количество {part['qty']!r}") from exc
        if part["quantity"] <= 0:
            raise EstimateError(f"{where}: количество должно быть больше нуля")
        has_key, has_manual = bool(part["price_key"]), bool(part["manual_price_rub"])
        if has_key == has_manual:
            raise EstimateError(f"{where}: нужен либо price_key, либо manual_price_rub")
        if has_manual != (part["status"] == "оценка"):
            raise EstimateError(f"{where}: статус «оценка» ставится строкам с manual_price_rub и только им")
        if part["status"] != "по документам" and not part["note"].strip():
            raise EstimateError(f"{where}: для статуса «{part['status']}» нужно пояснение в note")
        if not part["photo"].endswith(".jpg"):
            raise EstimateError(f"{where}: фотография должна быть файлом .jpg")
        for field in ("name", "spec", "unit", "basis"):
            if not part[field].strip():
                raise EstimateError(f"{where}: пустое поле {field}")

    used_keys = {part["price_key"] for part in parts if part["price_key"]}
    for key in used_keys:
        if not any(offer["key"] == key and offer["use"] == "да" for offer in offers):
            raise EstimateError(f"{PARTS_CSV.name}: для ключа {key} нет ни одного предложения в расчёте")
    for offer in offers:
        if offer["key"] not in used_keys and offer["use"] == "да":
            raise EstimateError(
                f"{OBS_CSV.name}: ключ {offer['key']} не используется в parts.csv — "
                "поставьте «нет» и причину"
            )
    return {"config": config, "parts": parts, "offers": offers, "photos": photos}


# --------------------------------------------------------------------------- #
# Расчёт
# --------------------------------------------------------------------------- #


def compute(data: dict, rate: Decimal | None = None) -> dict:
    config = data["config"]
    rate = Decimal(rate) if rate is not None else Decimal(config["usd_rate"])
    if rate <= 0:
        raise EstimateError("курс доллара должен быть больше нуля")

    def usd(amount: Decimal) -> Decimal:
        return money(amount / rate)

    rows = []
    for part in data["parts"]:
        if part["price_key"]:
            prices = [
                offer["price"]
                for offer in data["offers"]
                if offer["key"] == part["price_key"] and offer["use"] == "да"
            ]
            unit, low, high, count = money(median(prices)), min(prices), max(prices), len(prices)
        else:
            unit = money(Decimal(part["manual_price_rub"]))
            low, high, count = unit, unit, 0
        quantity = part["quantity"]
        total = money(unit * quantity)
        rows.append(
            {
                **part,
                "unit_price": unit,
                "total": total,
                "total_usd": usd(total),
                "offers_used": count,
                "low": low,
                "high": high,
                "total_low": money(low * quantity),
                "total_high": money(high * quantity),
            }
        )

    sections = []
    for section in config["sections"]:
        members = [row for row in rows if row["section"] == section["id"]]
        total = sum((row["total"] for row in members), Decimal(0))
        sections.append(
            {
                **section,
                "number": len(sections) + 1,
                "rows": members,
                "total": total,
                "total_usd": usd(total),
                "total_low": sum((row["total_low"] for row in members), Decimal(0)),
                "total_high": sum((row["total_high"] for row in members), Decimal(0)),
            }
        )

    parts_total = sum((section["total"] for section in sections), Decimal(0))
    other = [
        {**item, "amount": money(Decimal(item["amount_rub"]))} for item in config["other_costs"]
    ]
    other_total = sum((item["amount"] for item in other), Decimal(0))
    grand_total = parts_total + other_total
    parts_low = sum((section["total_low"] for section in sections), Decimal(0))
    parts_high = sum((section["total_high"] for section in sections), Decimal(0))

    previous = config["previous"]
    groups = []
    for group in previous["groups"]:
        now = sum((row["total"] for row in rows if row["old_group"] == group["id"]), Decimal(0))
        was = Decimal(group["amount_rub"])
        groups.append({**group, "was": was, "now": now, "delta": now - was})
    previous_parts = sum((group["was"] for group in groups), Decimal(0))
    previous_total = Decimal(previous["total_rub"])
    if previous_parts + other_total != previous_total:
        raise EstimateError("прежняя калькуляция: статьи и прочие затраты не дают прежний итог")

    used = [offer for offer in data["offers"] if offer["use"] == "да"]
    return {
        "config": config,
        "rate": rate,
        "usd": usd,
        "rows": rows,
        "sections": sections,
        "other": other,
        "groups": groups,
        "offers": data["offers"],
        "photos": data["photos"],
        "parts_total": parts_total,
        "other_total": other_total,
        "grand_total": grand_total,
        "parts_low": parts_low,
        "parts_high": parts_high,
        "grand_low": parts_low + other_total,
        "grand_high": parts_high + other_total,
        "previous_parts": previous_parts,
        "previous_total": previous_total,
        "positions": len(rows),
        "offers_total": len(data["offers"]),
        "offers_used": len(used),
        "shops": len({offer["shop"] for offer in used}),
        "manual_rows": [row for row in rows if row["status"] == "оценка"],
        "assumed_rows": [row for row in rows if row["status"] == "допущение"],
        "single_rows": [row for row in rows if row["offers_used"] == 1],
    }


def totals(result: dict) -> dict:
    """Итоги для цитирования в других документах и для тестов."""
    usd = result["usd"]

    def pair(amount: Decimal) -> dict:
        return {"rub": str(money(amount)), "usd": str(usd(amount))}

    return {
        "price_date": result["config"]["price_date"],
        "usd_rate": str(result["rate"]),
        "positions": result["positions"],
        "offers_used": result["offers_used"],
        "offers_total": result["offers_total"],
        "shops": result["shops"],
        "manual_positions": len(result["manual_rows"]),
        "assumed_positions": len(result["assumed_rows"]),
        "parts": pair(result["parts_total"]),
        "other": pair(result["other_total"]),
        "grand": pair(result["grand_total"]),
        "parts_low": pair(result["parts_low"]),
        "parts_high": pair(result["parts_high"]),
        "grand_low": pair(result["grand_low"]),
        "grand_high": pair(result["grand_high"]),
        "previous_total_rub": str(result["previous_total"]),
        "previous_parts_rub": str(result["previous_parts"]),
        "sections": {
            section["id"]: pair(section["total"]) for section in result["sections"]
        },
    }


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _price_source(row: dict) -> str:
    count = row["offers_used"]
    if count == 0:
        return "оценка"
    if count == 1:
        return "одно предложение"
    spread = f"{fmt(row['low'], 0)}{NBSP}–{NBSP}{fmt(row['high'], 0)}"
    if count == 2:
        return f"среднее из{NBSP}2:<br>{spread}"
    return f"медиана из{NBSP}{count}:<br>{spread}"


def _photo(row: dict) -> str:
    if (PHOTO_DIR / row["photo"]).is_file():
        return f"![{cell(row['name'])}](assets/estimate/{row['photo']})"
    return "—"


def _ids_by_key(rows) -> dict:
    result = {}
    for row in rows:
        if row["price_key"]:
            result.setdefault(row["price_key"], []).append(row["id"])
    return result


def render_markdown(result: dict) -> str:
    config = result["config"]
    usd = result["usd"]
    rows = result["rows"]
    out = []
    add = out.append

    positions = result["positions"]
    pos_word = plural(positions, "позиция", "позиции", "позиций")
    manual, assumed = len(result["manual_rows"]), len(result["assumed_rows"])
    excluded = result["offers_total"] - result["offers_used"]

    add(f"# {config['title']}")
    add("")
    add(f"## {config['subtitle']}")
    add("")
    add(
        "> Этот файл собирает генератор "
        "[`tools/estimate/build_estimate.py`](../tools/estimate/build_estimate.py) "
        "из таблиц каталога [`docs/estimate/`](estimate/). Руками его не правят: "
        "меняют таблицы и запускают генератор заново."
    )
    add("")
    add("---")
    add("")
    add("## 1. ИТОГ")
    add("")
    add("| Статья | Сумма, ₽ | Сумма, USD |")
    add("| :--- | ---: | ---: |")
    add(
        f"| Комплектующие и материалы, {positions}{NBSP}{pos_word} "
        f"| {fmt(result['parts_total'])} | {fmt(usd(result['parts_total']))} |"
    )
    add(
        "| Работа, амортизация оборудования, интеграция "
        f"| {fmt(result['other_total'])} | {fmt(usd(result['other_total']))} |"
    )
    add(
        f"| **Всего на один робот** | **{fmt(result['grand_total'])}** "
        f"| **{fmt(usd(result['grand_total']))}** |"
    )
    add("")
    add(
        f"Цены собраны {config['price_date']}. Пересчёт в доллары США — по курсу "
        f"{fmt(result['rate'], 4)}{NBSP}₽ за доллар: это "
        f"[{config['usd_rate_source']}]({config['usd_rate_url']}) на {config['usd_rate_date']}."
    )
    add("")
    add("Что нужно знать, прежде чем пользоваться итогом:")
    add("")
    add(
        "* **Цены взяты из поисковой выдачи, а не со страниц магазинов.** Каждая цена — "
        f"то, что показал поиск {config['price_date']}. Страницы товаров не открывались, "
        "наличие и цена на день покупки не проверялись. Доставка в цены не входит."
    )
    add(
        "* **Цена позиции — середина найденных предложений.** Если по каждой позиции брать "
        f"самое дешёвое из найденного, комплектующие обойдутся в {fmt(result['parts_low'])}{NBSP}₽, "
        f"если самое дорогое — в {fmt(result['parts_high'])}{NBSP}₽ (раздел 6)."
    )
    add(
        f"* **Часть состава — допущения.** У {assumed}{NBSP}"
        f"{plural(assumed, 'позиции', 'позиций', 'позиций')} модель или количество в документах "
        f"проекта не названы и подобраны по смыслу, ещё {manual}{NBSP}"
        f"{plural(manual, 'позиция оценена', 'позиции оценены', 'позиций оценены')} без поиска цен. "
        "Все они перечислены в разделе 8."
    )
    add(
        "* **Работа, амортизация и интеграция не пересчитывались:** суммы перенесены из "
        "прежней калькуляции."
    )
    add("")
    add("---")
    add("")
    add("## 2. КАК ПОЛУЧЕНЫ ЦЕНЫ")
    add("")
    add(
        "1. **Состав.** Взят из документов этого репозитория, из прошивок "
        "[`firmware/motor_wheel`](../firmware/motor_wheel/motor_wheel.ino) и "
        "[`firmware/sensor_module`](../firmware/sensor_module/sensor_module.ino) и из "
        "документации бортового ПО [Zapret](https://github.com/danilka-revin/Zapret). "
        "В таблицах под названием каждой позиции указано, на что она опирается."
    )
    add(
        f"2. **Поиск.** По каждой позиции {config['price_date']} выполнен поисковый запрос "
        "с названием и ключевыми параметрами. Из выдачи выписаны предложения, в которых "
        "однозначно видны товар и цена в рублях: "
        f"{result['offers_total']}{NBSP}{plural(result['offers_total'], 'предложение', 'предложения', 'предложений')}. "
        "Цены в другой валюте и строки, где цену нельзя уверенно отнести к товару, "
        "не выписывались."
    )
    add(
        "3. **Цена позиции.** Медиана выписанных предложений; при двух предложениях это их "
        f"среднее. В расчёте {result['offers_used']}{NBSP}"
        f"{plural(result['offers_used'], 'предложение', 'предложения', 'предложений')} "
        f"от {result['shops']}{NBSP}{plural(result['shops'], 'продавца', 'продавцов', 'продавцов')}; "
        f"{excluded}{NBSP}{plural(excluded, 'предложение', 'предложения', 'предложений')} "
        "в расчёт не взяты, причины указаны в приложении А."
    )
    add(
        "4. **Суммы.** Сумма по строке — цена, умноженная на количество, с округлением до "
        "копейки. Доллары — рубли, делённые на курс, с округлением до цента. Итоги в "
        "долларах считаются от итогов в рублях, а не сложением строк."
    )
    add(
        "5. **Пересчёт.** Чтобы обновить смету, исправьте "
        "[`parts.csv`](estimate/parts.csv) или "
        "[`price_observations.csv`](estimate/price_observations.csv) и запустите "
        "`python3 tools/estimate/build_estimate.py`. Курс доллара задаётся в "
        "[`estimate.json`](estimate/estimate.json)."
    )
    add("")
    add("---")
    add("")
    add("## 3. СВОДКА ПО РАЗДЕЛАМ")
    add("")
    add("| № | Раздел | Позиций | Сумма, ₽ | Сумма, USD | Доля |")
    add("| :---: | :--- | :---: | ---: | ---: | ---: |")
    for section in result["sections"]:
        share = section["total"] * 100 / result["parts_total"]
        add(
            f"| {section['number']} | {section['title']} | {len(section['rows'])} "
            f"| {fmt(section['total'])} | {fmt(section['total_usd'])} | {fmt(share, 1)}{NBSP}% |"
        )
    add(
        f"| | **Комплектующие и материалы** | **{positions}** | **{fmt(result['parts_total'])}** "
        f"| **{fmt(usd(result['parts_total']))}** | **100,0{NBSP}%** |"
    )
    add("")
    add("---")
    add("")
    add("## 4. СПЕЦИФИКАЦИЯ С ЦЕНАМИ")
    add("")
    add(
        "В колонке «Откуда цена» указано, сколько предложений вошло в расчёт и в каких "
        "пределах лежат их цены в рублях. Сами предложения со ссылками — в приложении А. "
        "Фотографии показывают тип изделия, а не конкретный купленный экземпляр; их "
        "источники — в приложении Б."
    )
    add("")
    for section in result["sections"]:
        add(f"### 4.{section['number']}. {section['title']}")
        add("")
        add(section["lead"])
        add("")
        add("| № | Фото | Наименование и параметры | Кол-во | Цена, ₽ | Сумма, ₽ | Сумма, USD | Откуда цена |")
        add("| :---: | :---: | :--- | :---: | ---: | ---: | ---: | :--- |")
        for row in section["rows"]:
            name = (
                f"**{cell(row['name'])}**<br>{cell(row['spec'])}"
                f"<br>*Основание: {cell(row['basis'])}*"
            )
            add(
                f"| {row['id']} | {_photo(row)} | {name} "
                f"| {fmt_amount(row['quantity'], row['unit'])} | {fmt(row['unit_price'])} "
                f"| {fmt(row['total'])} | {fmt(row['total_usd'])} | {_price_source(row)} |"
            )
        add(
            f"| | | **Итого по разделу** | | | **{fmt(section['total'])}** "
            f"| **{fmt(section['total_usd'])}** | |"
        )
        add("")
        notes = [row for row in section["rows"] if row["note"].strip()]
        if notes:
            add("Примечания к разделу:")
            add("")
            for row in notes:
                add(f"* **{row['id']}.** {row['note'].strip()}")
            add("")
    add("---")
    add("")
    add("## 5. ПРОЧИЕ ЗАТРАТЫ И ПОЛНЫЙ ИТОГ")
    add("")
    add(
        "Эти три статьи перенесены из прежней калькуляции без изменений: по ним нет "
        "новых данных."
    )
    add("")
    add("| Статья | Что входит | Сумма, ₽ | Сумма, USD |")
    add("| :--- | :--- | ---: | ---: |")
    for item in result["other"]:
        add(
            f"| {item['name']} | {item['description']} | {fmt(item['amount'])} "
            f"| {fmt(usd(item['amount']))} |"
        )
    add(
        f"| **Прочие затраты** | | **{fmt(result['other_total'])}** "
        f"| **{fmt(usd(result['other_total']))}** |"
    )
    add(
        f"| Комплектующие и материалы | разделы 4.1–4.{len(result['sections'])} "
        f"| {fmt(result['parts_total'])} | {fmt(usd(result['parts_total']))} |"
    )
    add(
        f"| **Всего на один робот** | | **{fmt(result['grand_total'])}** "
        f"| **{fmt(usd(result['grand_total']))}** |"
    )
    add("")
    add("---")
    add("")
    add("## 6. РАЗБРОС ЦЕН")
    add("")
    add(
        "Одна и та же позиция у разных продавцов стоит по-разному. В таблице — сумма "
        "раздела, если по каждой позиции взять самое дешёвое, принятое в смете и самое "
        "дорогое из найденных предложений. Позиции с одним предложением и оценки в "
        "разброс ничего не добавляют."
    )
    add("")
    add("| Раздел | По самым дешёвым, ₽ | Принято в смете, ₽ | По самым дорогим, ₽ |")
    add("| :--- | ---: | ---: | ---: |")
    for section in result["sections"]:
        add(
            f"| {section['title']} | {fmt(section['total_low'])} | {fmt(section['total'])} "
            f"| {fmt(section['total_high'])} |"
        )
    add(
        f"| **Комплектующие и материалы** | **{fmt(result['parts_low'])}** "
        f"| **{fmt(result['parts_total'])}** | **{fmt(result['parts_high'])}** |"
    )
    add(
        f"| **Всего с прочими затратами** | **{fmt(result['grand_low'])}** "
        f"| **{fmt(result['grand_total'])}** | **{fmt(result['grand_high'])}** |"
    )
    add("")
    add(
        f"В долларах США полный итог лежит между {fmt(usd(result['grand_low']))} и "
        f"{fmt(usd(result['grand_high']))}; принятое значение — {fmt(usd(result['grand_total']))}."
    )
    add("")
    add("---")
    add("")
    add("## 7. СРАВНЕНИЕ С ПРЕЖНЕЙ КАЛЬКУЛЯЦИЕЙ")
    add("")
    add(
        f"Прежняя калькуляция ({config['previous']['source']}) состояла из шести укрупнённых "
        f"статей на комплектующие и давала итог {fmt(result['previous_total'], 0)}{NBSP}₽. "
        "Ниже позиции новой сметы разложены по тем же статьям."
    )
    add("")
    add("| Статья прежней калькуляции | Было, ₽ | Стало, ₽ | Разница, ₽ | Что в неё входит теперь |")
    add("| :--- | ---: | ---: | ---: | :--- |")
    for group in result["groups"]:
        delta = group["delta"]
        sign = "+" if delta >= 0 else ""
        add(
            f"| {group['name']} | {fmt(group['was'], 0)} | {fmt(group['now'])} "
            f"| {sign}{fmt(delta)} | {group['content']} |"
        )
    delta_parts = result["parts_total"] - result["previous_parts"]
    add(
        f"| **Комплектующие и материалы** | **{fmt(result['previous_parts'], 0)}** "
        f"| **{fmt(result['parts_total'])}** | **+{fmt(delta_parts)}** | |"
    )
    add(
        f"| Работа, амортизация, интеграция | {fmt(result['other_total'], 0)} "
        f"| {fmt(result['other_total'])} | 0,00 | перенесены без пересчёта |"
    )
    delta_total = result["grand_total"] - result["previous_total"]
    add(
        f"| **Всего** | **{fmt(result['previous_total'], 0)}** | **{fmt(result['grand_total'])}** "
        f"| **+{fmt(delta_total)}** | |"
    )
    add("")
    ratio = result["grand_total"] / result["previous_total"]
    add(
        f"Новый итог больше прежнего в {fmt(ratio, 2)} раза. Основная часть разницы — позиции, "
        "которых в прежней калькуляции не было: модуль датчиков со светом и замком, три "
        "камеры из четырёх, сенсор глубины, дисплей, принтер, зарядное устройство, "
        "преобразователи напряжения и аварийный выключатель. Лидар и компьютер в прежней "
        "калькуляции были, но оценены ниже найденных сейчас цен."
    )
    add("")
    add("---")
    add("")
    add("## 8. ДОПУЩЕНИЯ И ЧТО НЕ ВОШЛО")
    add("")
    add("### 8.1. Позиции, подобранные по допущению")
    add("")
    add(
        "В документах проекта у этих позиций не названа модель или не указано количество. "
        "Перед закупкой их нужно сверить с собранным роботом."
    )
    add("")
    add("| № | Позиция | В чём допущение | Сумма, ₽ |")
    add("| :---: | :--- | :--- | ---: |")
    for row in result["assumed_rows"]:
        add(f"| {row['id']} | {cell(row['name'])} | {cell(row['note'])} | {fmt(row['total'])} |")
    assumed_total = sum((row["total"] for row in result["assumed_rows"]), Decimal(0))
    add(f"| | **Всего по допущениям** | | **{fmt(assumed_total)}** |")
    add("")
    add("### 8.2. Позиции, оценённые без поиска цен")
    add("")
    add("| № | Позиция | Пояснение | Сумма, ₽ |")
    add("| :---: | :--- | :--- | ---: |")
    for row in result["manual_rows"]:
        add(f"| {row['id']} | {cell(row['name'])} | {cell(row['note'])} | {fmt(row['total'])} |")
    manual_total = sum((row["total"] for row in result["manual_rows"]), Decimal(0))
    add(f"| | **Всего по оценкам** | | **{fmt(manual_total)}** |")
    add("")
    add("### 8.3. Что в смету не вошло")
    add("")
    for item in config["not_included"]:
        add(f"* {item};")
    add(
        "* ведомые шкивы на 150 зубьев отдельной строкой: они печатаются из пластика, "
        "который учтён в разделе 4.7."
    )
    add("")
    add("### 8.4. На что обратить внимание при закупке")
    add("")
    add(
        "* **Понижающий модуль XL4015 рассчитан на вход до 36 В,** а полностью заряженная "
        "батарея 12S даёт до 43,8 В. Питать такой модуль можно от шины 24 В, но не прямо "
        "от батареи. Подробнее — в [`ELECTRICAL_AND_PINOUTS.md`](ELECTRICAL_AND_PINOUTS.md)."
    )
    add(
        "* **Преобразователи, подключённые к батарее, должны допускать вход не ниже 48 В.** "
        "В смету включены модели с входом до 60–72 В."
    )
    add(
        "* **Считыватель RC522 питается только от 3,3 В.** Поэтому в составе модуля "
        "датчиков есть преобразователи уровней."
    )
    add("")
    add("---")
    add("")
    add("## ПРИЛОЖЕНИЕ А. НАЙДЕННЫЕ ПРЕДЛОЖЕНИЯ")
    add("")
    add(
        f"Все предложения, выписанные из поисковой выдачи {config['price_date']}. Ссылка "
        "ведёт на страницу, которую показал поиск; сама страница не открывалась. "
        "В колонке «В расчёте» — «да» или причина, по которой предложение не учтено."
    )
    add("")
    add("| Позиции | Цена, ₽ | Продавец | Что предлагается | В расчёте |")
    add("| :--- | ---: | :--- | :--- | :--- |")
    ids_by_key = _ids_by_key(rows)
    order = []
    for row in rows:
        if row["price_key"] and row["price_key"] not in order:
            order.append(row["price_key"])
    unused = sorted({offer["key"] for offer in result["offers"]} - set(order))
    for key in order + unused:
        ids = ", ".join(ids_by_key.get(key, [])) or "—"
        for offer in sorted(
            (item for item in result["offers"] if item["key"] == key),
            key=lambda item: item["price"],
        ):
            verdict = "да" if offer["use"] == "да" else f"нет: {cell(offer['note'])}"
            if offer["use"] == "да" and offer["note"].strip():
                verdict = f"да; {cell(offer['note'])}"
            add(
                f"| {ids} | {fmt(offer['price'])} | [{cell(offer['shop'])}]({offer['url']}) "
                f"| {cell(offer['offer'])} | {verdict} |"
            )
    add("")
    add("---")
    add("")
    add("## ПРИЛОЖЕНИЕ Б. ИСТОЧНИКИ ФОТОГРАФИЙ")
    add("")
    add(
        "Фотографии уменьшены до 180 × 180 точек и лежат в "
        "[`docs/assets/estimate/`](assets/estimate/). Они иллюстрируют тип изделия. "
        "Снимки из сети найдены поиском по картинкам; права на них принадлежат их "
        "владельцам."
    )
    add("")
    add("| Файл | Позиции | Источник |")
    add("| :--- | :--- | :--- |")
    by_photo = {}
    for row in rows:
        by_photo.setdefault(row["photo"], []).append(row["id"])
    for photo, ids in by_photo.items():
        info = result["photos"].get(photo)
        if not info:
            source = "источник не записан"
        elif info.get("repo"):
            source = f"файл репозитория [`{info['repo']}`](../{info['repo']})"
        else:
            source = f"[{cell(info.get('title') or info['url'])}]({info['url']})"
        add(f"| `{photo}` | {', '.join(ids)} | {source} |")
    add("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CSV с рассчитанными суммами
# --------------------------------------------------------------------------- #


def render_csv(result: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "id", "section", "name", "spec", "qty", "unit", "unit_price_rub", "total_rub",
            "total_usd", "offers_used", "offer_min_rub", "offer_max_rub", "status",
        ]
    )
    titles = {section["id"]: section["title"] for section in result["sections"]}
    for row in result["rows"]:
        writer.writerow(
            [
                row["id"], titles[row["section"]], row["name"], row["spec"],
                f"{row['quantity'].normalize():f}", row["unit"], row["unit_price"], row["total"],
                row["total_usd"], row["offers_used"], money(row["low"]), money(row["high"]),
                row["status"],
            ]
        )
    usd = result["usd"]
    writer.writerow(
        ["", "", "Комплектующие и материалы", "", "", "", "", money(result["parts_total"]),
         usd(result["parts_total"]), "", "", "", ""]
    )
    writer.writerow(
        ["", "", "Работа, амортизация, интеграция", "", "", "", "", money(result["other_total"]),
         usd(result["other_total"]), "", "", "", ""]
    )
    writer.writerow(
        ["", "", "Всего на один робот", "", "", "", "", money(result["grand_total"]),
         usd(result["grand_total"]), "", "", "", ""]
    )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Командная строка
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сборка сметы робота из таблиц docs/estimate/.")
    parser.add_argument("--check", action="store_true", help="только проверить, что файлы актуальны")
    parser.add_argument("--totals", action="store_true", help="напечатать итоги в JSON")
    parser.add_argument("--rate", help="пересчитать итоги при другом курсе доллара, файлы не менять")
    args = parser.parse_args(argv)

    try:
        data = load()
        if args.rate:
            what_if = compute(data, Decimal(args.rate.replace(",", ".")))
            print(json.dumps(totals(what_if), ensure_ascii=False, indent=2))
            return 0
        result = compute(data)
    except EstimateError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    if args.totals:
        print(json.dumps(totals(result), ensure_ascii=False, indent=2))
        return 0

    outputs = {OUT_MD: render_markdown(result), OUT_CSV: render_csv(result)}
    if args.check:
        stale = [
            path for path, text in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != text
        ]
        for path in stale:
            print(f"Устарел: {path.relative_to(ROOT)}", file=sys.stderr)
        if stale:
            print("Запустите: python3 tools/estimate/build_estimate.py", file=sys.stderr)
            return 1
        print(f"Смета актуальна: {fmt(result['grand_total'])} ₽")
        return 0

    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
        print(f"Записан {path.relative_to(ROOT)}")
    missing = sorted({row["photo"] for row in result["rows"] if not (PHOTO_DIR / row["photo"]).is_file()})
    if missing:
        print(f"Нет фотографий: {', '.join(missing)}", file=sys.stderr)
    print(
        f"Комплектующие: {fmt(result['parts_total'])} ₽; всего: {fmt(result['grand_total'])} ₽ "
        f"= {fmt(result['usd'](result['grand_total']))} USD"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
