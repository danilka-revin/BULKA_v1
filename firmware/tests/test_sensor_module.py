"""Модуль датчиков: прошивка, симуляция и документы — без железа.

Запуск:  python3 -m pytest firmware/tests/test_sensor_module.py -q

Что проверяется:

* распиновка и правила безопасности в скетче `firmware/sensor_module` —
  они заданы разводкой платы, ошибка в пине — это сгоревший RC522;
* хост-симуляция `firmware/firmware_sim/sensor_sim.cpp`: GY-87 на I²C, RC522 на
  SPI с картой в поле, лента, реле и серво замка. Скетч собирается дважды —
  с лентой (как на роботе) и без неё (`-DSENSOR_STRIP_ENABLED=0`);
* кадры, которые печатает симулятор (`@S`, `@CARD`, `@CARDINFO`, `@EVT`,
  `@ACK`), имеют верную CRC-16/Modbus и ровно те поля, что описаны в
  `docs/SERIAL_PROTOCOL.md`;
* распиновка в `docs/ELECTRICAL_AND_PINOUTS.md` и `firmware/README.md` совпадает
  с константами скетча, число сценариев в документах — с прогоном.

Серверная часть (разбор кадров в `robot_control/sensor_module.py`, экран
«Датчики») живёт в репозитории Zapret и проверяется там.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[1]
ROOT = FIRMWARE.parent
SKETCH = FIRMWARE / "sensor_module" / "sensor_module.ino"
SIM_DIR = FIRMWARE / "firmware_sim"
FIRMWARE_README = FIRMWARE / "README.md"
ROOT_README = ROOT / "README.md"
PINOUT = ROOT / "docs" / "ELECTRICAL_AND_PINOUTS.md"
PROTOCOL = ROOT / "docs" / "SERIAL_PROTOCOL.md"

#: Версия протокола платы (поле fw= в кадре @S).
SENSOR_PROTOCOL = 4
#: Поля кадра @S в том порядке, в котором их печатает прошивка.
TELEMETRY_KEYS = ("fw", "hdg", "pitch", "roll", "gz", "press", "temp", "alt", "lock",
                  "light", "dim", "strip", "nleds", "cargo", "rfid", "cards", "link",
                  "err", "t", "crc")


def _text(path: Path = SKETCH) -> str:
    return path.read_text(encoding="utf-8")


def _const(name: str) -> str:
    match = re.search(r"\bconst\s+[\w: ]+?\b%s\s*=\s*([^;]+);" % re.escape(name), _text())
    assert match, name
    return match.group(1).strip()


def crc16_modbus(data: bytes) -> int:
    """CRC-16/Modbus — формула из docs/SERIAL_PROTOCOL.md §4."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def parse_frame(line: str) -> tuple[str, dict[str, str]]:
    """Разобрать кадр `@X ключ=значение … crc=ABCD`, проверив сумму."""
    line = line.strip()
    payload, crc = line.rsplit(" crc=", 1)
    assert re.fullmatch(r"[0-9A-F]{4}", crc), line
    assert crc16_modbus(payload.encode("utf-8")) == int(crc, 16), line
    kind, *pairs = payload.split(" ")
    fields = dict(pair.split("=", 1) for pair in pairs)
    fields["crc"] = crc
    return kind, fields


# ============================ прошивка: пины и правила ====================== #
def test_pinout_is_exactly_the_wired_one():
    text = _text()
    assert _const("RC522_SS_PIN") == "53"          # SPI Mega: SS
    assert _const("RC522_RST_PIN") == "9"
    assert _const("STRIP_PIN") == "6"
    assert _const("LOCK_SERVO_PIN") == "7"
    assert re.search(r"RELAY_PINS\[4\]\s*=\s*\{2,\s*3,\s*4,\s*5\}", text)
    # Концевик отсека (D8) не подключён и из прошивки убран.
    assert "DOOR_SWITCH_PIN" not in text and "doorTick" not in text
    # GY-87 на аппаратном I²C Mega: SDA = 20, SCL = 21 (Wire.begin() без аргументов).
    assert "Wire.begin()" in text and "Wire.begin(" not in text.replace("Wire.begin()", "")
    assert "SDA — 20" in text and "SCL — 21" in text


def test_rc522_is_powered_from_3v3_only():
    text = _text()
    assert "3.3 В" in text
    assert "RC522_VCC" not in text      # питание RC522 с пинов Mega не берётся


def test_relay_order_matches_the_lights():
    text = _text()
    start = text.index("void applyRelays(")
    apply = text[start:start + 900]
    assert re.search(r"relayWrite\(0,\s*\(mask & LIGHT_HEAD\)", apply)
    assert re.search(r"relayWrite\(1,\s*\(mask & LIGHT_STOP\)", apply)
    assert re.search(r"relayWrite\(2,\s*\(mask & LIGHT_LEFT\)", apply)
    assert re.search(r"relayWrite\(3,\s*\(mask & LIGHT_RIGHT\)", apply)
    assert _const("RELAY_ACTIVE_LOW") == "true"


def test_board_is_safe_after_reset_and_watchdog_runs():
    text = _text()
    setup = text[text.index("void setup()"):text.index("void loop()")]
    assert "relayWrite(i, false)" in setup      # реле выключены до датчиков
    assert "lockSet(false, 0)" in setup         # замок закрыт
    assert "strip.clear()" in setup             # лента погашена
    assert "wdt_enable(WDTO_2S)" in setup
    assert "wdt_reset()" in text[text.index("void loop()"):]


def test_sketch_avoids_heap_and_blocking_prints():
    text = _text()
    assert "String " not in text
    assert "Serial.print" not in text
    assert "malloc(" not in text and "new " not in text
    assert "rcStart(" in text and "rcPoll(" in text     # обмен с картой неблокирующий


def test_protocol_version_is_four_without_the_door_switch():
    assert int(_const("SENSOR_PROTOCOL")) == SENSOR_PROTOCOL
    code = _text()
    # В самих кадрах (строках формата) признака двери нет; в комментариях
    # скетча он упоминается как убранный.
    formats = re.findall(r'PSTR\("(@[A-Z]+ [^"]*)"', code)
    assert formats, "строки формата кадров не найдены"
    assert not any("door" in fmt for fmt in formats), formats


def test_strip_is_73_leds_pointing_down_with_a_current_budget():
    text = _text()
    assert re.search(r"#ifndef SENSOR_STRIP_ENABLED\s*\n#define SENSOR_STRIP_ENABLED 1", text)
    assert re.search(r"#define SENSOR_STRIP_LEDS 73", text)
    assert "светят вниз" in text or "светят ВНИЗ" in text
    budget = int(_const("STRIP_BUDGET_MA"))
    per_led = int(_const("STRIP_WHITE_MA_PER_LED"))
    assert budget < 73 * per_led           # бюджет действительно ограничивает
    assert "digitalWrite(STRIP_PIN, LOW)" in text    # без ленты линия не висит


def test_timings_match_the_documents():
    assert int(_const("LINK_TIMEOUT_MS")) == 10000
    assert int(_const("LOCK_DEFAULT_MS")) == 3000
    assert int(_const("LOCK_MAX_MS")) == 10000
    assert int(_const("CARD_SLOTS").split("//")[0]) == 12
    readme = _text(FIRMWARE_README)
    for token in ("LINK_TIMEOUT_MS", "12 карт", "5 Гц", "1,6 Гц"):
        assert token in readme, token


# ============================== хост-симуляция ============================== #
def _compiler() -> str | None:
    return shutil.which(os.environ.get("CXX", "g++")) or shutil.which("clang++")


def _build(binary: Path, *, strip: bool) -> Path:
    compiler = _compiler()
    if not compiler:
        pytest.skip("нет g++/clang++ — симуляция модуля датчиков пропущена")
    command = [compiler, "-std=c++17", "-O1", "-w",
               "-DSENSOR_STRIP_ENABLED=%d" % (1 if strip else 0),
               "-I", str(SIM_DIR / "stubs"), str(SIM_DIR / "sensor_sim.cpp"),
               "-o", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-4000:]
    return binary


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):
    """Плата как на роботе: лента смонтирована."""
    return _build(tmp_path_factory.mktemp("sensor-sim") / "sensor_sim", strip=True)


@pytest.fixture(scope="module")
def simulator_without_strip(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("sensor-sim-bare") / "sensor_sim", strip=False)


def _run_all(binary: Path) -> tuple[int, int, str]:
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=900)
    report = result.stdout + "\n" + result.stderr[-6000:]
    assert result.returncode == 0, report
    summary = re.search(r"(\d+)/(\d+) сценариев прошли", result.stdout)
    assert summary, report
    return int(summary.group(1)), int(summary.group(2)), report


def test_all_scenarios_pass_with_the_strip(simulator):
    passed, total, report = _run_all(simulator)
    assert passed == total, report
    assert total >= 20, report


def test_all_scenarios_pass_without_the_strip(simulator_without_strip):
    passed, total, report = _run_all(simulator_without_strip)
    assert passed == total, report


def test_documented_scenario_count_is_the_real_one(simulator):
    _, total, _ = _run_all(simulator)
    assert "**%d/%d сценариев**" % (total, total) in _text(FIRMWARE_README)
    assert re.search(r"\b%d сценари" % total, _text(ROOT_README)), total


@pytest.fixture(scope="module")
def dump(simulator):
    result = subprocess.run([str(simulator), "dump"], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_every_machine_frame_has_a_valid_crc(dump):
    kinds: dict[str, int] = {}
    for line in dump:
        if line.startswith("#"):
            assert line.startswith("# SENSORS: "), line
            kinds["#"] = kinds.get("#", 0) + 1
            continue
        kind, _ = parse_frame(line)
        kinds[kind] = kinds.get(kind, 0) + 1
    for kind in ("@S", "@CARD", "@CARDINFO", "@EVT", "@ACK", "#"):
        assert kinds.get(kind), (kind, kinds)
    assert set(kinds) == {"@S", "@CARD", "@CARDINFO", "@EVT", "@ACK", "#"}, kinds


def test_telemetry_frame_has_exactly_the_documented_fields(dump):
    frames = [parse_frame(line)[1] for line in dump if line.startswith("@S ")]
    assert frames
    for fields in frames:
        assert tuple(fields) == TELEMETRY_KEYS, fields
        assert int(fields["fw"]) == SENSOR_PROTOCOL
        assert fields["strip"] == "1" and fields["nleds"] == "73"
        assert 0 <= int(fields["hdg"]) <= 35999
        assert "door" not in fields
    # Барометр опрашивается 2 раза в секунду: в первых кадрах давления ещё нет,
    # дальше — пример расчёта из документации BMP180 (UT = 27898, UP = 23843).
    measured = [fields for fields in frames if fields["press"] != "0"]
    assert measured, "ни одного замера барометра"
    for fields in measured:
        assert fields["press"] == "69964" and fields["temp"] == "150"

    protocol = _text(PROTOCOL)
    for key in TELEMETRY_KEYS:
        assert "`%s`" % key in protocol, key


def test_cards_are_granted_and_denied_by_the_board(dump):
    cards = [parse_frame(line)[1] for line in dump if line.startswith("@CARD ")]
    denied = [card for card in cards if card["ok"] == "0"]
    granted = [card for card in cards if card["ok"] == "1"]
    assert denied and denied[0]["uid"] == "11223344" and denied[0]["why"] == "unknown"
    assert granted and granted[0]["uid"] == "A1B2C3D4" and granted[0]["role"] == "admin"
    listing = [parse_frame(line)[1] for line in dump if line.startswith("@CARDINFO ")]
    assert any(item.get("end") == "1" for item in listing)
    assert {item["role"] for item in listing if "role" in item} == {"admin", "user"}
    acks = [parse_frame(line)[1] for line in dump if line.startswith("@ACK ")]
    ids = [int(ack["id"]) for ack in acks]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)     # номера команд растут


# ================================ документы ================================= #
def test_documents_agree_with_the_sketch_on_pins():
    for doc in (PINOUT, FIRMWARE_README):
        text = _text(doc)
        for token in ("sensor_module", "RC522", "GY-87", "3.3 В", "73",
                      "SDA", "SCL", "MISO", "MOSI", "SCK"):
            assert token in text, (doc.name, token)
        # Пины из скетча: I²C 20/21, SPI 50–53, RST 9, реле 2–5, лента 6, замок 7.
        for pin in ("D20", "D21", "D50", "D51", "D52", "D53", "D9", "D6", "D7"):
            assert pin in text, (doc.name, pin)


def test_protocol_document_describes_protocol_four():
    text = _text(PROTOCOL)
    assert "sensor_module" in text
    for frame in ("@S", "@CARD", "@CARDINFO", "@EVT", "@ACK"):
        assert "`%s`" % frame in text, frame
    assert "nleds" in text
    # Концевика нет: документ не должен обещать поле состояния двери.
    assert "door=" not in text


def test_documented_frame_example_matches_the_firmware_format():
    """Пример @S в документах состоит из тех же полей, что печатает плата."""
    for doc in (PROTOCOL, FIRMWARE_README):
        examples = [line.strip() for line in _text(doc).splitlines()
                    if line.strip().startswith("@S fw=%d " % SENSOR_PROTOCOL)]
        assert examples, doc.name
        for line in examples:
            keys = tuple(pair.split("=", 1)[0] for pair in line.split(" ")[1:])
            assert keys == TELEMETRY_KEYS, (doc.name, keys)
