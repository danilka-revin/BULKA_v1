#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты бэкенда основного экрана (gui/backend.py) — без внешних зависимостей.

Запуск:  python3 gui/tests/backend.test.py
Проверяет: CRC16 и кадры протокола, модель АКБ, разбор потока UART с мусором,
PIN-хранилище (попытки, блокировка, смена PIN, целостность аудита),
и HTTP API (статика, /api/state, открытие/закрытие замка, /api/audit).
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import backend as B  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  → " + str(extra)) if (extra and not cond) else ""))


class TestProtocol(unittest.TestCase):
    def test_crc_vector(self):
        check("CRC16: эталон '123456789' → 0x4B37", B.crc16(b"123456789") == 0x4B37,
              hex(B.crc16(b"123456789")))

    def test_command_frame(self):
        f = B.build_command(1, 4500, 200, enable=True, home=False)
        check("кадр команды: 10 байт, AA 55, ID, флаги, CRC", len(f) == 10 and f[0] == 0xAA and f[1] == 0x55
              and f[2] == 1 and f[7] == 0x01 and B.crc16(f[:8]) == int.from_bytes(f[8:], "big"), f.hex())

    def test_telemetry_roundtrip(self):
        body = bytes([0xBB, 0x44, 2, 0x03]) + (1500).to_bytes(2, "big", signed=True) \
            + (120).to_bytes(2, "big", signed=True) + (500).to_bytes(2, "big", signed=True) \
            + (4120).to_bytes(2, "big") + (1240).to_bytes(2, "big")
        frame = body + B.crc16(body).to_bytes(2, "big")
        t = B.parse_telemetry(frame)
        check("телеметрия: разбор 16 Б (угол, энкодер, ШИМ, U, I)",
              t and t["id"] == "FR" and abs(t["steerDeg"] - 15.0) < 1e-6
              and abs(t["volts"] - 41.2) < 1e-6 and abs(t["amps"] - 1.24) < 1e-6
              and t["homed"] and t["enabled"], t)

    def test_bad_crc(self):
        body = bytes([0xBB, 0x44, 1, 0x01]) + b"\x00" * 10
        frame = body + b"\x00\x01"
        check("телеметрия: битый CRC отбрасывается", B.parse_telemetry(frame) is None)

    def test_stream_resync(self):
        body = bytes([0xBB, 0x44, 3, 0x03]) + b"\x00" * 10
        good = body + B.crc16(body).to_bytes(2, "big")
        stream = b"\x00\x11\x22" + good[:7] + b"garbage" + good + b"\x33"
        found, rest = B.pick_telemetry(stream)
        check("поток: кадр найден после мусора", found and found["moduleId"] == 3, found)
        check("поток: хвост буфера сохранён", isinstance(rest, bytes))

    def test_extreme_clamp(self):
        f = B.build_command(4, 99999, -99999)
        steer = int.from_bytes(f[3:5], "big", signed=True)
        pwm = int.from_bytes(f[5:7], "big", signed=True)
        check("кадр: клампы угла и ШИМ", steer == 32767 and pwm == -1000, (steer, pwm))


class TestBattery(unittest.TestCase):
    def test_ocv_bounds(self):
        check("АКБ: 43,8 В → 100 %, 30,0 В → 0 %",
              B.soc_from_voltage(43.8) == 100 and B.soc_from_voltage(30.0) == 0,
              (B.soc_from_voltage(43.8), B.soc_from_voltage(30.0)))

    def test_monotonic(self):
        vals = [B.soc_from_voltage(v) for v in (30, 33, 35.5, 38, 40, 43.8)]
        check("АКБ: SOC монотонен по напряжению", all(a <= b for a, b in zip(vals, vals[1:])), vals)

    def test_pack_state(self):
        # 38,4 В — «номинал» пакета, но по OCV-кривой LiFePO4 это ~15 % заряда:
        # кривая плоская, поэтому проверяем на напряжении, соответствующем 60 % SOC.
        mid = B.voltage_from_soc(60)
        st = B.pack_state(mid, 12.4)
        check("АКБ: сводка на 60 % (Вт·ч, км, уровень)",
              abs(st["soc"] - 60) < 3 and 400 < st["remainingWh"] < 460
              and st["rangeKm"] > 12 and st["level"] == "НОРМА", st)
        nom = B.pack_state(38.4, 12.4)
        check("АКБ: 38,4 В по кривой — низкий заряд (плоская OCV LiFePO4)",
              10 < nom["soc"] < 25, nom["soc"])
        low = B.pack_state(34.0, 5.0)
        check("АКБ: порог 35,5/33,5 В работает", low["level"] == "НИЗКИЙ", low["level"])
        crit = B.pack_state(32.0, 5.0)
        check("АКБ: ниже 33,5 В — критический", crit["level"] == "КРИТИЧЕСКИЙ", crit["level"])


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "lock.json")
        self.now = [1000.0]
        self.v = B.Vault(self.path, "2580", max_attempts=5, lock_ms=30000,
                         clock=lambda: self.now[0])

    def test_default_pin(self):
        check("PIN: заводской 2580 работает", self.v.verify("2580")["ok"] is True)

    def test_wrong_pin(self):
        r = self.v.verify("1111")
        check("PIN: неверный отклонён, остались попытки", r["ok"] is False and r["attemptsLeft"] == 4, r)

    def test_lockout_and_expiry(self):
        for _ in range(5):
            self.v.verify("1111")
        r = self.v.verify("2580")
        check("PIN: 5 неудач → блокировка (даже верный PIN)", r["ok"] is False and r["reason"] == "blocked", r)
        self.now[0] += 31
        check("PIN: через 30 с ввод разблокирован", self.v.verify("2580")["ok"] is True)

    def test_set_pin(self):
        bad = self.v.set_pin("0000", "1234")
        check("PIN: смена с неверным текущим отклонена", bad["ok"] is False)
        good = self.v.set_pin("2580", "4321")
        check("PIN: смена PIN работает", good["ok"] is True and self.v.verify("4321")["ok"] is True)

    def test_persist(self):
        self.v.verify("2580")
        v2 = B.Vault(self.path, "2580")
        check("PIN: данные переживают перезапуск", v2.verify("2580")["ok"] is True)
        check("PIN: аудит сохранён", len(v2.audit(10)) >= 1)

    def test_audit_chain(self):
        self.v.verify("2580")
        self.v.verify("0000")
        self.v.verify("2580")
        check("аудит: цепочка валидна", self.v.verify_audit()["ok"] is True, self.v.verify_audit())
        self.v.data["audit"][0]["detail"] = "подделка"
        res = self.v.verify_audit()
        check("аудит: подделка записи обнаружена", res["ok"] is False and res["brokenAt"] == 0, res)

    def test_hardware_hook_absent(self):
        check("замок: без RUS_SLAM_LOCK_CMD работает программно", B.hardware_lock("open") == "software")


class TestApi(unittest.TestCase):
    """Живой HTTP-сервер на свободном порту."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        B.LOCK_FILE = os.path.join(cls.tmp, "lock.json")
        app = B.App(B.SimSource(), B.Vault(B.LOCK_FILE, "2580"))
        B.ApiHandler.app = app
        cls.server = B.ThreadingHTTPServer(("127.0.0.1", 0), B.partial(B.ApiHandler))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return r.status, json.load(r)

    def post(self, path, payload):
        req = urllib.request.Request(self.url(path), data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_01_static(self):
        req = urllib.request.Request(self.url("/"))
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        with opener.open(req, timeout=5) as r:
            body = r.read().decode("utf-8")
        check("HTTP: / без сессии отдаёт экран запуска",
              "Вход на борт" in body and "PIN" in body and "data-key" in body, body[:80])

    def test_02_state(self):
        status, body = self.get("/api/state")
        d = body.get("data", {})
        check("API: /api/state — 200 и структура", status == 200 and d.get("source") == "sim"
              and len(d.get("motors", [])) == 4 and "soc" in d.get("battery", {})
              and "lock" in d, list(d.keys()))

    def test_03_state_moves(self):
        _, a = self.get("/api/state")
        time.sleep(0.6)
        _, b = self.get("/api/state")
        check("API: показания меняются во времени",
              a["data"]["battery"]["soc"] != b["data"]["battery"]["soc"]
              or a["data"]["motors"][0]["rpm"] != b["data"]["motors"][0]["rpm"])

    def test_04_lock_wrong_pin(self):
        status, body = self.post("/api/lock/open", {"pin": "1111"})
        check("API: неверный PIN → 403 и остаток попыток",
              status == 403 and body.get("ok") is False and body.get("attemptsLeft") == 4, body)

    def test_05_lock_open(self):
        status, body = self.post("/api/lock/open", {"pin": "2580"})
        check("API: верный PIN открывает отсек", status == 200 and body.get("ok") is True
              and body["lock"]["open"] is True, body)
        _, state = self.get("/api/state")
        check("API: состояние отсека в /api/state", state["data"]["lock"]["open"] is True
              and state["data"]["cargo"]["closed"] is False)

    def test_06_lock_close(self):
        status, body = self.post("/api/lock/close", {})
        check("API: закрытие отсека", status == 200 and body["lock"]["open"] is False, body)
        _, state = self.get("/api/state")
        check("API: груз снова закрыт", state["data"]["cargo"]["closed"] is True)

    def test_07_audit(self):
        _, body = self.get("/api/audit?limit=10")
        check("API: журнал доступа и целостность", body["ok"] and body["integrity"]["ok"]
              and len(body["audit"]) >= 1, body.get("integrity"))

    def test_08_health(self):
        _, body = self.get("/api/health")
        check("API: /api/health отвечает", body["ok"] is True and body["source"] == "sim", body)

    def test_09_change_pin(self):
        status, body = self.post("/api/lock/pin", {"current": "2580", "new": "1357"})
        check("API: смена PIN через API", status == 200 and body.get("ok") is True, body)
        status2, body2 = self.post("/api/lock/open", {"pin": "1357"})
        check("API: новый PIN открывает отсек", status2 == 200 and body2.get("ok") is True, body2)


class TestGate(unittest.TestCase):
    """Экран запуска: PIN выбирает роль, роль живёт в cookie и ограничивает доступ."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        lock = os.path.join(cls.tmp, "gate-lock.json")
        app = B.App(B.SimSource(), B.Vault(lock, "2580"),
                    operator_pin="7232", user_pin="5656")
        B.ApiHandler.app = app
        cls.app = app
        cls.server = B.ThreadingHTTPServer(("127.0.0.1", 0), B.partial(B.ApiHandler))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, payload=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body, hdrs = None, dict(headers or {})
        if payload is not None:
            body = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        if cookie:
            hdrs["Cookie"] = cookie
        headers = hdrs
        conn.request(method, path, body, headers)
        res = conn.getresponse()
        raw = res.read()
        out = (res.status, res.getheader("Location"), res.getheader("Set-Cookie"),
               json.loads(raw.decode("utf-8")) if raw and (res.getheader("Content-Type") or "").startswith("application/json") else raw.decode("utf-8", "replace"))
        conn.close()
        return out

    @staticmethod
    def cookie_of(set_cookie):
        return (set_cookie or "").split(";", 1)[0]

    def login(self, pin):
        return self.call("POST", "/api/gate", {"pin": pin})

    def test_01_gate_redirect(self):
        status, where, _, _ = self.call("GET", "/")
        check("шлюз: / без сессии → gate.html", status == 302 and where == "/gate.html", (status, where))
        status, where, _, _ = self.call("GET", "/main.html")
        check("шлюз: киоск без сессии недоступен", status == 302 and where == "/gate.html", (status, where))
        status, where, _, _ = self.call("GET", "/index.html")
        check("шлюз: пульт без сессии недоступен", status == 302 and where == "/gate.html", (status, where))

    def test_02_gate_page(self):
        status, _, _, body = self.call("GET", "/gate.html")
        check("шлюз: страница с PIN-клавиатурой отдаётся",
              status == 200 and "Вход на борт" in body and "data-key" in body, status)

    def test_03_gate_wrong(self):
        status, _, _, body = self.login("1111")
        check("шлюз: неверный PIN → 403 и остаток попыток",
              status == 403 and body.get("ok") is False and body.get("attemptsLeft") == 4, body)

    def test_04_operator(self):
        status, _, cookie, body = self.login("7232")
        check("шлюз: 7232 → роль оператора", status == 200 and body.get("role") == "operator"
              and body.get("redirect") == "/main.html", body)
        check("шлюз: сессия в HttpOnly-cookie",
              "HttpOnly" in (cookie or "") and self.cookie_of(cookie).startswith("rus_role="), cookie)
        jar = self.cookie_of(cookie)
        status, where, _, _ = self.call("GET", "/", cookie=jar)
        check("оператор: / ведёт в киоск", status == 302 and where == "/main.html", (status, where))
        status, _, _, body = self.call("GET", "/main.html", cookie=jar)
        check("оператор: киоск открывается", status == 200 and "основной экран робота" in body, status)
        status, _, _, _ = self.call("GET", "/index.html", cookie=jar)
        check("оператор: инженерный пульт открывается", status == 200, status)

    def test_04b_token_fallback(self):
        _, _, _, body = self.login("7232")
        token = body.get("token")
        check("шлюз: токен возвращается странице", bool(token), body)
        status, _, _, page = self.call("GET", "/main.html?t=" + token)
        check("вход без cookie: киоск по ?t=", status == 200 and "основной экран робота" in page, status)
        status, _, _, sess = self.call("GET", "/api/session", headers={"X-Session": token})
        check("вход без cookie: X-Session даёт роль", status == 200 and sess.get("role") == "operator", sess)
        status, where, _, _ = self.call("GET", "/main.html?t=forged-token")
        check("вход без cookie: чужой токен не пускает", status == 302 and where == "/gate.html", (status, where))

    def test_05_user(self):
        status, _, cookie, body = self.login("5656")
        check("шлюз: 5656 → роль получателя", status == 200 and body.get("role") == "user"
              and body.get("redirect") == "/user.html", body)
        jar = self.cookie_of(cookie)
        status, where, _, _ = self.call("GET", "/", cookie=jar)
        check("получатель: / ведёт на панель", status == 302 and where == "/user.html", (status, where))
        status, where, _, _ = self.call("GET", "/main.html", cookie=jar)
        check("получатель: киоск оператора недоступен", status == 302 and where == "/user.html", (status, where))
        status, where, _, _ = self.call("GET", "/index.html", cookie=jar)
        check("получатель: инженерный пульт закрыт", status == 302 and where == "/gate.html", (status, where))
        status, _, _, body = self.call("GET", "/user.html", cookie=jar)
        check("получатель: панель открывается", status == 200 and "панель получателя" in body, status)
        status, _, _, _ = self.call("GET", "/api/session", cookie=jar)
        check("API: /api/session отдаёт роль", status == 200, status)

    def test_06_user_lock(self):
        _, _, cookie, _ = self.login("5656")
        jar = self.cookie_of(cookie)
        status, _, _, body = self.call("POST", "/api/lock/close", {}, cookie=jar)
        check("получатель: закрытие без PIN запрещено", status == 403 and body.get("ok") is False, body)
        status, _, _, body = self.call("POST", "/api/lock/pin",
                                       {"current": "2580", "new": "9999"}, cookie=jar)
        check("получатель: смена PIN запрещена", status == 403 and body.get("ok") is False, body)
        status, _, _, body = self.call("POST", "/api/lock/open", {"pin": "2580"}, cookie=jar)
        check("получатель: отсек открывается по PIN", status == 200 and body.get("ok") is True, body)
        status, _, _, body = self.call("POST", "/api/lock/close", {"pin": "2580"}, cookie=jar)
        check("получатель: отсек закрывается по PIN", status == 200 and body.get("ok") is True, body)

    def test_07_state_open_without_session(self):
        status, _, _, body = self.call("GET", "/api/state")
        check("API: /api/state доступен без сессии (нативные экраны)",
              status == 200 and body.get("ok") is True, status)

    def test_08_logout(self):
        _, _, cookie, _ = self.login("7232")
        jar = self.cookie_of(cookie)
        status, _, clear, body = self.call("POST", "/api/logout", {}, cookie=jar)
        check("выход: сессия сбрасывается", status == 200 and "Max-Age=0" in (clear or ""), clear)
        status, where, _, _ = self.call("GET", "/main.html", cookie=jar)
        check("выход: киоск снова за шлюзом", status == 302 and where == "/gate.html", (status, where))

    def test_09_gate_lockout(self):
        self.app.gate_fails = 0
        self.app.gate_lock_until = 0.0
        for _ in range(B.GATE_MAX_ATTEMPTS):
            status, _, _, body = self.login("0000")
        check("шлюз: 5 неверных попыток → блокировка", body.get("attemptsLeft") == 0
              and body.get("remainingMs", 0) > 0, body)
        status, _, _, body = self.login("7232")
        check("шлюз: во время блокировки верный PIN не пускает", status == 403 and body.get("ok") is False, body)
        self.app.gate_fails = 0
        self.app.gate_lock_until = 0.0


class TestServerNoWindows(unittest.TestCase):
    """Сервер борта сам ничего не открывает: страницы смотрят в браузере.

    Основной экран на дисплее робота показывает отдельный сервис
    (deploy/systemd/rus-slam-display.service), поэтому запуск backend.py без флагов
    не должен трогать браузер робота.
    """

    def _run_main(self, argv):
        import unittest.mock as mock
        opened = []
        with mock.patch.object(B.webbrowser, "open", lambda *a, **k: opened.append("webbrowser")), \
             mock.patch.object(B, "launch_kiosk", lambda *a, **k: opened.append("kiosk")), \
             mock.patch.object(B, "ThreadingHTTPServer") as srv:
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            code = B.main(argv)
            # даём шанс потоку-открывателю (если он есть) — заглушки ещё активны
            deadline = time.time() + 0.7
            while time.time() < deadline and not opened:
                time.sleep(0.05)
        return code, opened

    def test_01_no_browser_by_default(self):
        code, opened = self._run_main(["--port", "0"])
        check("запуск без флагов: сервер не открывает окон", opened == [], opened)
        check("запуск без флагов: код возврата 0", code == 0, code)

    def test_02_kiosk_is_explicit(self):
        code, opened = self._run_main(["--port", "0", "--kiosk", "--delay", "0"])
        check("флаг --kiosk: страница на дисплее робота открывается", "kiosk" in opened, opened)

    def test_03_open_is_explicit(self):
        code, opened = self._run_main(["--port", "0", "--open", "--delay", "0"])
        check("флаг --open: страница открывается в браузере", "webbrowser" in opened, opened)


if __name__ == "__main__":
    unittest.main(verbosity=0, exit=False)
    print("\nИТОГО бэкенд: %d passed, %d failed" % (len(OK), len(FAIL)))
    if FAIL:
        print("Провалено: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)
