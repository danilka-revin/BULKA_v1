"""Проверки прошивки колёс и документации без железа.

Запуск:  python3 -m pytest firmware/tests/test_firmware_sim.py -q
Модуль датчиков проверяется рядом: firmware/tests/test_sensor_module.py.

Что проверяется:

* скетч `firmware/motor_wheel` собирается g++ в четырёх экземплярах
  (FL/FR/RL/RR) и проходит все сценарии хост-симуляции (`firmware/firmware_sim`);
* телеметрия, которую отдаёт симулятор, соответствует описанию в
  `docs/SERIAL_PROTOCOL.md` (поля `@F` и `@TLM`, версия fw, роль, угол `pa`),
  а контрольные суммы кадров сходятся с формулой из того же документа;
* число сценариев, названное в документах, равно числу пройденных;
* распиновка в `docs/ELECTRICAL_AND_PINOUTS.md` и `firmware/README.md`
  совпадает с константами скетча — документы не должны расходиться с кодом;
* старые артефакты (удалённая бинарная прошивка) не возвращаются.

Тест не требует Arduino IDE и не трогает реальные USB-порты.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FIRMWARE = Path(__file__).resolve().parents[1]
ROOT = FIRMWARE.parent
SKETCH = FIRMWARE / "motor_wheel" / "motor_wheel.ino"
SIM_DIR = FIRMWARE / "firmware_sim"
FIRMWARE_README = FIRMWARE / "README.md"
ROOT_README = ROOT / "README.md"
PINOUT = ROOT / "docs" / "ELECTRICAL_AND_PINOUTS.md"
PROTOCOL = ROOT / "docs" / "SERIAL_PROTOCOL.md"

#: Текущая версия протокола прошивки колеса. Меняется вместе со скетчем и
#: документами: тесты ниже ловят расхождение любой из трёх сторон.
FW = 26
#: Признак «пригодного показания A0 нет» в поле pa (fw26).
POT_ANGLE_UNAVAILABLE = 32767


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _const(text: str, name: str) -> str:
    match = re.search(r"\bconst\s+[\w ]+?\b%s\s*=\s*([^;]+);" % re.escape(name), text)
    assert match, name
    return match.group(1).strip().rstrip("f").rstrip("UL").rstrip("L")


def crc16_modbus(data: bytes) -> int:
    """CRC-16/Modbus — ровно та формула, что напечатана в docs/SERIAL_PROTOCOL.md §4."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("нет g++/clang++ — симуляция прошивки пропущена")
    binary = tmp_path_factory.mktemp("firmware-sim") / "sim"
    result = subprocess.run(
        [compiler, "-std=c++17", "-O1", "-w", "-I", str(SIM_DIR / "stubs"),
         str(SIM_DIR / "sim.cpp"), "-o", str(binary)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return binary


@pytest.fixture(scope="module")
def scenario_report(simulator):
    result = subprocess.run([str(simulator)], capture_output=True, text=True, timeout=900)
    return result


def test_all_firmware_scenarios_pass(scenario_report):
    result = scenario_report
    report = result.stdout + "\n" + result.stderr[-6000:]
    assert result.returncode == 0, report
    summary = re.search(r"(\d+)/(\d+) сценариев прошли", result.stdout)
    assert summary and summary.group(1) == summary.group(2), report


def test_documented_scenario_count_is_the_real_one(scenario_report):
    """«61/61» в документах — не украшение: число берётся из прогона."""
    summary = re.search(r"(\d+)/(\d+) сценариев прошли", scenario_report.stdout)
    assert summary, scenario_report.stdout[-2000:]
    total = summary.group(2)
    assert "**%s/%s сценариев**" % (total, total) in _text(FIRMWARE_README)
    assert re.search(r"\b%s сценари" % total, _text(ROOT_README)), total


def test_fw26_scenarios_are_present(scenario_report):
    """Сценарии, пришедшие вместе с fw26: угол A0 в телеметрии и отказ датчика."""
    passed = {line.split()[0] for line in scenario_report.stdout.splitlines()
              if line.strip().endswith(" ok")}
    for name in ("pot_hold_sensor_fault", "pot_hold_memory_fault",
                 "pot_negative_reboot_and_telemetry"):
        assert name in passed, name


def test_telemetry_matches_documented_format(simulator):
    result = subprocess.run([str(simulator), "tlm_dump"], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-4000:]
    fast = [line for line in result.stdout.splitlines() if line.startswith("@F ")]
    full = [line for line in result.stdout.splitlines() if line.startswith("@TLM ")]
    assert fast, "@F не найден"
    assert full, "@TLM не найден"

    fw = int(_const(_text(SKETCH), "FIRMWARE_PROTOCOL"))
    assert fw == FW

    # @F fw26: «@F» + 20 чисел + CRC = 22 слова; роль — второе число,
    # угол A0 (pa) — последнее число перед CRC.
    for line in fast:
        fields = line.split()
        assert len(fields) == 22, line
        assert int(fields[1]) == fw
        assert int(fields[2]) in (1, 2, 3, 4)
        assert re.fullmatch(r"[0-9A-Fa-f]{4}", fields[-1]), line
        pot_angle = int(fields[-2])
        assert pot_angle == POT_ANGLE_UNAVAILABLE or -18000 <= pot_angle <= 18000, line
        payload = line.rsplit(" ", 1)[0]
        assert crc16_modbus(payload.encode("ascii")) == int(fields[-1], 16), line

    # @TLM: ключ=значение, обязательные поля на месте, pa — перед crc.
    required = ("mod=", "deg=", "tgt=", "cal=", "home=", "err=", "fw=", "pot=",
                "pm=", "ph=", "zfault=", "sc_rpm=", "sc_hz=", "sc_ppr=",
                "brake_id=", "live_id=", "pa=", "crc=")
    for line in full:
        for key in required:
            assert key in line, (key, line)
        assert "fw=%d" % fw in line
        assert re.search(r" t=\d+ pa=-?\d+ crc=[0-9A-F]{4}$", line.strip()), line
        payload, crc = line.strip().rsplit(" crc=", 1)
        assert crc16_modbus(payload.encode("utf-8")) == int(crc, 16), line


def test_pot_angle_field_is_documented_everywhere():
    """fw26: поле pa и его «нет показания» описаны в скетче и во всех документах."""
    sketch = _text(SKETCH)
    assert int(_const(sketch, "POT_ANGLE_UNAVAILABLE_C")) == POT_ANGLE_UNAVAILABLE
    assert " pa=%ld" in sketch
    for doc in (PROTOCOL, FIRMWARE_README, ROOT_README):
        text = _text(doc)
        assert "fw26" in text, doc.name
        assert "`pa`" in text, doc.name
    for doc in (PROTOCOL, FIRMWARE_README):
        assert str(POT_ANGLE_UNAVAILABLE) in _text(doc), doc.name
    # В таблице полей @F документа pa стоит перед crc.
    protocol = _text(PROTOCOL)
    assert re.search(r"<ms> <pa> <crc>", protocol)
    # Вступление документа (до первого раздела) называет текущую версию.
    intro = protocol.split("## 1.")[0]
    assert "fw26" in intro and "fw25" not in intro


def test_documented_frame_examples_have_valid_crc():
    """Примеры кадров в документах — настоящие: сумма сходится."""
    checked = 0
    for doc in (PROTOCOL, FIRMWARE_README):
        for line in _text(doc).splitlines():
            line = line.strip()
            if not line.startswith("@F %d " % FW):
                continue
            fields = line.split()
            if len(fields) != 22 or not re.fullmatch(r"[0-9A-F]{4}", fields[-1]):
                continue
            assert crc16_modbus(line.rsplit(" ", 1)[0].encode("ascii")) == int(fields[-1], 16), (doc.name, line)
            checked += 1
    assert checked >= 2, "в документах нет примера @F fw26 с верной суммой"


def test_docs_and_sketch_agree_on_pins():
    sketch = _text(SKETCH)
    expected = {
        "STEP_PUL_PIN": ("D6", "D6"),
        "STEP_DIR_PIN": ("D7", "D7"),
        "STEP_ENA_PIN": ("D8", "D8"),
        "BLDC_STOP_PIN": ("D2", "D2"),
        "BLDC_SPEED_PIN": ("D3", "D3"),
        "BLDC_DIR_PIN": ("D4", "D4"),
        "BLDC_BRAKE_PIN": ("D5", "D5"),
        "OPTO_PIN": ("D18", "D18"),
        "TACH_PIN": ("D19", "D19"),
    }
    for const, (sketch_pin, doc_pin) in expected.items():
        assert _const(sketch, const) == sketch_pin.strip("D"), const
        for doc in (PINOUT, FIRMWARE_README):
            assert doc_pin in _text(doc), (const, doc.name)

    # Потенциометр и предел хода — одно число на весь стек.
    assert _const(sketch, "POT_PIN") == "A0"
    assert "A0" in _text(PINOUT)
    assert float(_const(sketch, "STEPS_PER_DEGREE")) == pytest.approx(133.333333, rel=1e-6)
    assert "133.33" in _text(PINOUT)
    assert "133.33" in _text(FIRMWARE_README)
    assert float(_const(sketch, "STEER_LIMIT_DEG")) == 100.0
    assert "±100" in _text(FIRMWARE_README)
    assert int(_const(sketch, "DRIVE_COMMAND_TIMEOUT_MS")) == 650
    assert "650 мс" in _text(PROTOCOL)


def test_old_binary_firmware_is_gone():
    assert not (FIRMWARE / "module_controller").exists()
    for doc in (PROTOCOL,):
        text = _text(doc)
        assert "0xAA" not in text and "0xBB" not in text, doc


def test_ported_sources_keep_this_repository_layout():
    """Перенос из Zapret не должен приносить чужие пути: там tests/ и scripts/ в корне."""
    for path in (SIM_DIR / "sim.cpp", SIM_DIR / "sensor_sim.cpp", SIM_DIR / "arduino_sim.h"):
        text = _text(path)
        assert "tests/firmware_sim" not in text, path.name
    upload = _text(FIRMWARE / "scripts" / "avr_upload.py")
    assert "ROOT = Path(__file__).resolve().parents[2]" in upload
    assert '"Adafruit_NeoPixel"' in upload      # библиотека ленты модуля датчиков
    sync = _text(FIRMWARE / "scripts" / "sync_firmware_desktop.sh")
    assert 'cd "$(dirname "$0")/../.."' in sync
    assert "firmware/sensor_module/sensor_module.ino" in sync


def test_reference_sketch_is_untouched_benchmark():
    reference = FIRMWARE / "reference" / "steering_tested" / "steering_tested.ino"
    assert reference.is_file()
    text = _text(reference)
    # Пины эталона: TB6600 6/7/8, оптодатчик 18 — с ними сверяется прошивка.
    for pin in ("PUL = 6", "DIR = 7", "ENA = 8", "OPTO_PIN = 18"):
        assert pin in text, pin
