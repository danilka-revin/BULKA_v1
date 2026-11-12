# -*- coding: utf-8 -*-
"""Проверки сметы робота.

Что здесь проверяется:
  * арифметика: медиана предложений, суммы строк и разделов, пересчёт в доллары;
  * согласованность таблиц docs/estimate/ и собранного docs/COST_ESTIMATE.md;
  * состав против прошивок: число плат, колёс, элементов батареи, диодов ленты;
  * фотографии: у каждой позиции есть снимок 180 × 180 и записан его источник;
  * итоги сметы, процитированные в других документах, совпадают с расчётом.

Запуск:  python -m pytest tools/estimate -q
"""

from __future__ import annotations

import copy
import csv
import json
import re
import struct
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_estimate as be  # noqa: E402

SENSOR_SKETCH = ROOT / "firmware" / "sensor_module" / "sensor_module.ino"
QUOTING_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "TECHNICAL_SPECIFICATION.md",
    ROOT / "docs" / "EXPLANATORY_NOTE_AVTOVAZ.md",
    ROOT / "docs" / "ZAPRET_SYNC.md",
)


@pytest.fixture(scope="module")
def data():
    return be.load()


@pytest.fixture(scope="module")
def result(data):
    return be.compute(data)


def _plain(text: str) -> str:
    """Неразрывные и узкие пробелы -> обычные: так удобнее искать числа."""
    return re.sub(r"[\u00a0\u202f\u2009]", " ", text)


def _quantity(result, **conditions) -> Decimal:
    """Суммарное количество по строкам сметы, подходящим под условия."""
    total = Decimal(0)
    for row in result["rows"]:
        if all(row[field] == value for field, value in conditions.items()):
            total += row["quantity"]
    return total


def _jpeg_size(path: Path) -> tuple:
    """Размер JPEG из заголовка кадра — без Pillow."""
    blob = path.read_bytes()
    assert blob[:2] == b"\xff\xd8", f"{path.name}: не JPEG"
    pos = 2
    while pos < len(blob):
        assert blob[pos] == 0xFF, f"{path.name}: сбит поток маркеров"
        marker = blob[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (length,) = struct.unpack(">H", blob[pos + 2 : pos + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", blob[pos + 5 : pos + 9])
            return width, height
        pos += 2 + length
    raise AssertionError(f"{path.name}: не найден заголовок кадра")


# --------------------------------------------------------------------------- #
# Арифметика
# --------------------------------------------------------------------------- #


def test_generated_files_are_up_to_date():
    """COST_ESTIMATE.md и рассчитанный CSV собраны из текущих таблиц."""
    assert be.main(["--check"]) == 0


def test_unit_price_is_median_of_offers_in_use(data, result):
    by_key = {}
    for offer in data["offers"]:
        if offer["use"] == "да":
            by_key.setdefault(offer["key"], []).append(Decimal(offer["price_rub"]))
    for row in result["rows"]:
        if not row["price_key"]:
            assert row["unit_price"] == Decimal(row["manual_price_rub"])
            assert row["offers_used"] == 0
            continue
        prices = sorted(by_key[row["price_key"]])
        middle = len(prices) // 2
        expected = prices[middle] if len(prices) % 2 else (prices[middle - 1] + prices[middle]) / 2
        assert row["unit_price"] == be.money(expected), row["id"]
        assert row["offers_used"] == len(prices)
        assert row["low"] == prices[0] and row["high"] == prices[-1]


def test_known_prices(result):
    """Несколько цен, пересчитанных руками по таблице предложений."""
    price = {row["id"]: row["unit_price"] for row in result["rows"]}
    assert price["1.01"] == Decimal("7677.00")   # (7454 + 7900) / 2
    assert price["1.03"] == Decimal("2834.50")   # (2690 + 2979) / 2
    assert price["2.02"] == Decimal("413.70")    # (367.39 + 460) / 2 = 413.695, вверх
    assert price["3.04"] == Decimal("13202.50")  # (11880 + 14525) / 2
    assert price["4.01"] == Decimal("32056.00")  # середина пяти предложений


def test_rows_sections_and_totals_add_up(result):
    for row in result["rows"]:
        assert row["total"] == be.money(row["unit_price"] * row["quantity"]), row["id"]
        assert row["total_low"] <= row["total"] <= row["total_high"], row["id"]
    for section in result["sections"]:
        assert section["total"] == sum(row["total"] for row in section["rows"])
        assert section["rows"], section["id"]
    assert result["parts_total"] == sum(section["total"] for section in result["sections"])
    assert result["parts_total"] == sum(row["total"] for row in result["rows"])
    assert result["grand_total"] == result["parts_total"] + result["other_total"]
    assert result["parts_low"] < result["parts_total"] < result["parts_high"]


def test_usd_is_rub_divided_by_rate(result):
    rate = Decimal(result["config"]["usd_rate"])
    assert rate == Decimal("82.7536")
    for row in result["rows"]:
        assert row["total_usd"] == be.money(row["total"] / rate), row["id"]
    # Итог в долларах считается от итога в рублях, а не сложением строк.
    summary = be.totals(result)
    assert Decimal(summary["grand"]["usd"]) == be.money(result["grand_total"] / rate)
    assert Decimal(summary["parts"]["usd"]) == be.money(result["parts_total"] / rate)


def test_other_rate_changes_only_dollars(data, result):
    other = be.compute(data, Decimal("100"))
    assert other["grand_total"] == result["grand_total"]
    assert Decimal(be.totals(other)["grand"]["usd"]) == be.money(result["grand_total"] / 100)


def test_previous_estimate_is_reproduced(result):
    """Прежняя калькуляция: 136 200 на комплектующие + 71 000 прочих = 207 200."""
    assert result["previous_parts"] == Decimal("136200")
    assert result["other_total"] == Decimal("71000")
    assert result["previous_total"] == Decimal("207200")
    assert sum(group["now"] for group in result["groups"]) == result["parts_total"]


# --------------------------------------------------------------------------- #
# Состав
# --------------------------------------------------------------------------- #


def test_composition_matches_the_robot(result):
    """Четыре модуля колёс, пятая плата, батарея 12S3P, четыре камеры."""
    assert _quantity(result, price_key="mega2560") == 5
    assert _quantity(result, price_key="mega2560", section="pods") == 4
    assert _quantity(result, price_key="mega2560", section="sensor") == 1
    for key in ("wheel_hub_motor", "subaru_hub", "nema23", "tb6600", "bldc_driver",
                "pulley_gt2_20", "belt_gt2", "opto_endstop", "pot_10k"):
        assert _quantity(result, price_key=key) == 4, key
    assert _quantity(result, price_key="xl4015") == 5
    assert _quantity(result, price_key="usb_ab") == 5
    assert _quantity(result, price_key="lifepo4_32700") == 36  # 12S3P
    assert _quantity(result, price_key="holder_32700") == 72   # по два на элемент
    assert _quantity(result, price_key="c930e") == 4
    for key in ("lds01", "kinect360", "mini_pc", "usb_hub", "bms_12s", "estop_switch"):
        assert _quantity(result, price_key=key) == 1, key
    carriers = [row for row in result["rows"] if row["name"].startswith("Плата-носитель")]
    assert sum(row["quantity"] for row in carriers) == 5


def test_composition_matches_the_sensor_firmware(result):
    sketch = SENSOR_SKETCH.read_text(encoding="utf-8")
    leds = int(re.search(r"#define\s+SENSOR_STRIP_LEDS\s+(\d+)", sketch).group(1))
    strip = next(row for row in result["rows"] if row["price_key"] == "ws2812b")
    assert f"{leds} диод" in strip["spec"]
    # При 60 диодах на метр купленной ленты должно хватать.
    assert strip["quantity"] * 60 >= leds
    # Четыре реле света: фары, стоп-сигналы, левые и правые поворотники.
    relay = next(row for row in result["rows"] if row["price_key"] == "relay4")
    assert "4 канала" in relay["name"]
    for key in ("headlight", "stop_light", "turn_signal", "servo_mg996r", "rc522", "gy87"):
        assert _quantity(result, price_key=key) >= 1, key


def test_every_position_explains_itself(result):
    assert {row["status"] for row in result["rows"]} == set(be.STATUSES)
    for row in result["rows"]:
        assert row["basis"].strip(), row["id"]
        if row["status"] != "по документам":
            assert row["note"].strip(), row["id"]
    assert len(result["manual_rows"]) == 4
    assert all(row["status"] == "оценка" for row in result["manual_rows"])


# --------------------------------------------------------------------------- #
# Таблица предложений
# --------------------------------------------------------------------------- #


def test_offers_are_usable(data):
    assert len(data["offers"]) >= 140
    for offer in data["offers"]:
        assert offer["url"].startswith("https://") or offer["url"].startswith("http://")
        assert not re.search(r"[\s)|]", offer["url"]), offer["url"]
        assert Decimal(offer["price_rub"]) > 0
        if offer["use"] == "нет":
            assert offer["note"].strip()
    keys = {offer["key"] for offer in data["offers"]}
    used = {part["price_key"] for part in data["parts"] if part["price_key"]}
    assert used <= keys
    # Ключи без позиции допустимы, только если все их предложения исключены.
    for key in keys - used:
        assert all(o["use"] == "нет" for o in data["offers"] if o["key"] == key), key


@pytest.mark.parametrize(
    "breakage, message",
    [
        (lambda d: d["parts"].append(dict(d["parts"][0])), "повторяется"),
        (lambda d: d["parts"][0].update(price_key="no_such_key"), "нет ни одного предложения"),
        (lambda d: d["parts"][0].update(manual_price_rub="100"), "либо price_key"),
        (lambda d: d["parts"][0].update(qty="0"), "больше нуля"),
        (lambda d: d["offers"][0].update(use="может быть"), "«да» и «нет»"),
        (lambda d: d["offers"][0].update(use="нет", note=""), "нужна причина"),
        (lambda d: d["offers"][0].update(url="https://example.org/a b"), "ссылка"),
        (lambda d: d["offers"][0].update(price_rub="дорого"), "цена"),
    ],
)
def test_loader_rejects_broken_tables(monkeypatch, breakage, message):
    """Ошибка в таблице должна останавливать сборку, а не давать неверный итог."""
    original = be._read_csv

    def fake_read(path, fields):
        rows = original(path, fields)
        bucket = {"parts": rows, "offers": []} if path == be.PARTS_CSV else {"parts": [], "offers": rows}
        try:
            breakage(bucket)
        except IndexError:
            pass
        return rows

    monkeypatch.setattr(be, "_read_csv", fake_read)
    with pytest.raises(be.EstimateError, match=message):
        be.load()


# --------------------------------------------------------------------------- #
# Фотографии
# --------------------------------------------------------------------------- #


def test_every_position_has_a_photo_180x180(result):
    photos = {row["photo"] for row in result["rows"]}
    assert len(photos) >= 60
    for name in sorted(photos):
        path = be.PHOTO_DIR / name
        assert path.is_file(), f"нет фотографии {name}"
        assert _jpeg_size(path) == (180, 180), name
        assert path.stat().st_size < 40_000, f"{name}: слишком тяжёлый файл"
    on_disk = {path.name for path in be.PHOTO_DIR.iterdir()}
    assert on_disk == photos, f"лишние или недостающие файлы: {sorted(on_disk ^ photos)}"


def test_every_photo_has_a_recorded_source(result):
    sources = json.loads(be.PHOTO_JSON.read_text(encoding="utf-8"))
    photos = {row["photo"] for row in result["rows"]}
    assert set(sources) == photos
    for name, info in sources.items():
        if "repo" in info:
            assert (ROOT / info["repo"]).is_file(), name
        else:
            assert info["url"].startswith("https://"), name
            assert not re.search(r"[\s)|]", info["url"]), name
            assert info["title"].strip(), name


# --------------------------------------------------------------------------- #
# Собранные файлы
# --------------------------------------------------------------------------- #


def test_markdown_is_safe_for_github_and_pdf(result):
    text = be.OUT_MD.read_text(encoding="utf-8")
    assert "$" not in text, "знак доллара в Markdown включает формулы — пишите USD"
    assert "<img" not in text, "картинки в таблицах — только ![подпись](путь)"
    assert text.count("![") == len(result["rows"])
    for row in result["rows"]:
        assert f"| {row['id']} | ![" in text, row["id"]
    for section in result["sections"]:
        assert f"### 4.{section['number']}. {section['title']}" in text
    assert "ПРИЛОЖЕНИЕ А" in text and "ПРИЛОЖЕНИЕ Б" in text


def test_calculated_csv_matches_the_estimate(result):
    with be.OUT_CSV.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    body = [row for row in rows if row["id"]]
    assert len(body) == result["positions"] == 64
    assert sum(Decimal(row["total_rub"]) for row in body) == result["parts_total"]
    tail = {row["name"]: Decimal(row["total_rub"]) for row in rows if not row["id"]}
    assert tail["Комплектующие и материалы"] == result["parts_total"]
    assert tail["Всего на один робот"] == result["grand_total"]


def test_totals_json_is_complete(result):
    summary = be.totals(result)
    assert summary["positions"] == 64
    assert summary["offers_used"] <= summary["offers_total"]
    assert set(summary["sections"]) == {section["id"] for section in result["sections"]}
    json.dumps(summary)  # всё сериализуется


def test_tables_are_not_modified_by_compute(data):
    snapshot = copy.deepcopy(data["parts"])
    be.compute(data)
    assert data["parts"] == snapshot


# --------------------------------------------------------------------------- #
# Итоги, процитированные в других документах
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", QUOTING_DOCS, ids=lambda p: p.name)
def test_documents_quote_the_current_totals(result, path):
    """Сумма сметы в README, паспорте, записке и журнале синхронизации — текущая."""
    text = _plain(path.read_text(encoding="utf-8"))
    grand = _plain(be.fmt(result["grand_total"]))
    grand_usd = _plain(be.fmt(result["usd"](result["grand_total"])))
    assert grand in text, f"{path.name}: нет итога {grand} ₽"
    assert grand_usd in text, f"{path.name}: нет итога {grand_usd} USD"
    assert "COST_ESTIMATE.md" in text, f"{path.name}: нет ссылки на смету"
    assert "207 200 руб" not in text, f"{path.name}: остался прежний итог без пометки"


def test_explanatory_note_table_matches_sections(result):
    """Таблица затрат в пояснительной записке повторяет разделы сметы."""
    text = _plain((ROOT / "docs" / "EXPLANATORY_NOTE_AVTOVAZ.md").read_text(encoding="utf-8"))
    for section in result["sections"]:
        assert _plain(be.fmt(section["total"])) in text, section["title"]
    assert _plain(be.fmt(result["parts_total"])) in text
