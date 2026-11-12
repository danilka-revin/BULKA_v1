# СИНХРОНИЗАЦИЯ С БОРТОВЫМ ПО ZAPRET

## Что перенесено из репозитория Zapret и какие сведения о роботе по нему уточнены

> **Состояние на 10.11.2026.** Репозиторий приведён к бортовому ПО
> [danilka-revin/Zapret](https://github.com/danilka-revin/Zapret) на коммите
> `973f9ba` от 09.11.2026. Предыдущая точка сверки — коммит `970134a` от
> 05.11.2026; между ними в Zapret 54 коммита без учёта слияний.

Этот репозиторий — главный по прошивке и железу. Zapret — весь бортовой софт:
веб-пульт, бортовой экран, карта, навигация, работа с лидаром, камерами и BMS.
Прошивки в Zapret лежат копией. Когда они там уходят вперёд, их нужно вернуть
сюда вместе с описанием протокола. Документ фиксирует, что именно перенесено,
какие сведения о роботе исправлены и что осталось расхождением.

---

## 1. Перенесённые файлы

Скетчи совпадают с Zapret побайтно. В симуляции и скриптах отличаются только
пути к файлам и к корню репозитория: здесь всё лежит внутри `firmware/`.

| Файл в Zapret | Файл здесь | Сравнение |
| :--- | :--- | :--- |
| `firmware/motor_wheel/motor_wheel.ino` | [`firmware/motor_wheel/motor_wheel.ino`](../firmware/motor_wheel/motor_wheel.ino) | совпадает |
| `firmware/sensor_module/sensor_module.ino` | [`firmware/sensor_module/sensor_module.ino`](../firmware/sensor_module/sensor_module.ino) | совпадает |
| `firmware/reference/steering_tested/steering_tested.ino` | [`firmware/reference/steering_tested/steering_tested.ino`](../firmware/reference/steering_tested/steering_tested.ino) | совпадает |
| `firmware/tools/avr_build.py` | [`firmware/tools/avr_build.py`](../firmware/tools/avr_build.py) | совпадает |
| `tests/firmware_sim/arduino_sim.h`, `board_prelude.h` | [`firmware/firmware_sim/`](../firmware/firmware_sim) | совпадают |
| `tests/firmware_sim/sim.cpp`, `sensor_sim.cpp` | [`firmware/firmware_sim/`](../firmware/firmware_sim) | другие пути в комментариях |
| `tests/firmware_sim/stubs/` | [`firmware/firmware_sim/stubs/`](../firmware/firmware_sim/stubs) | другие пути в комментариях |
| `scripts/avr_upload.py` | [`firmware/scripts/avr_upload.py`](../firmware/scripts/avr_upload.py) | другие пути в справке и к корню |
| `scripts/sync_firmware_desktop.sh` | [`firmware/scripts/sync_firmware_desktop.sh`](../firmware/scripts/sync_firmware_desktop.sh) | другой путь к корню |

Тесты здесь свои: [`firmware/tests/`](../firmware/tests) собирают оба скетча,
прогоняют симуляцию и сверяют документы со скетчами.

---

## 2. Что изменилось в прошивках

| Плата | Было | Стало | Главное |
| :--- | :--- | :--- | :--- |
| Колесо, 4 шт. | fw25 | **fw26** | В быстром кадре `@F` появилось поле `pa` — измеренный угол потенциометра. Кадр стал длиной 22 слова. Наклон калибровки любого знака переживает перезапуск. Удержание угла выключается при потере сигнала датчика |
| Модуль датчиков, 1 шт. | не было | **протокол 4** | Пятая плата Arduino Mega: инерциальный модуль GY-87, считыватель карт RC522, 4 реле света, лента из 73 светодиодов, сервопривод замка, до 12 карт с ролями |
| Симуляция колёс | 58 сценариев | **61 сценарий** | Три новых сценария: `pot_hold_sensor_fault`, `pot_hold_memory_fault`, `pot_negative_reboot_and_telemetry` |
| Симуляция модуля датчиков | не было | **20 сценариев** | Безопасный старт, компас, свет и лента, замок, карты и роли, сбой памяти, потеря связи, отказы датчиков |

Подробности: [`firmware/README.md`](../firmware/README.md) (модуль датчиков —
раздел 16), [`SERIAL_PROTOCOL.md`](SERIAL_PROTOCOL.md) (модуль датчиков —
раздел 7), [`ELECTRICAL_AND_PINOUTS.md`](ELECTRICAL_AND_PINOUTS.md) (разделы 6 и 7).

**Порядок обновления на роботе: сначала сервер, затем платы.** Сервер Zapret
на коммите `973f9ba` разбирает и кадры прежних прошивок. Прежний сервер кадр
`@F` с полем `pa` отбрасывает: он ждёт строку короче. Платы программа не
перепрошивает — fw26 заливается вручную в каждую из четырёх плат колёс, скетч
модуля датчиков — в пятую.

---

## 3. Сведения о роботе, уточнённые по Zapret

| Что | Было в документах | Стало | Основание в Zapret |
| :--- | :--- | :--- | :--- |
| Мотор-колесо | Xiaomi M365 Pro, 10″, 260 Вт | AOVO Pro, 8,5″ (215,9 мм), 36 В, 350 Вт | `robot_control/maneuvers.py`, комментарий к `DEFAULT_CHASSIS` |
| Суммарная мощность тяги | 1040 Вт | 1400 Вт | расчёт: 4 × 350 |
| Колёсная база и колея | 550 и 460 мм | 500 и 380 мм, измерены между осями поворота | там же |
| Радиус разворота на месте | 358,5 мм | 314,0 мм, колёса под 52,77° | расчёт; тот же угол в `maneuvers.py` |
| Тяговое усилие | 362 Н | 426 Н | расчёт при радиусе колеса 0,108 м; момент 11,5 Н·м — прежняя оценка |
| Скорость | расчётные 15 км/ч | 0,625 м/с при `Servo.write(30)` | замер оператора в `tests/test_navigation_executor.py` |
| Лидар | «LDC 01» | LDS-01 (HLS-LFCD2), 230400 бод, скан через драйвер ROS 2 | `docs/connections.md` |
| Камеры | одна камера | 4 × Logitech C930e и датчик глубины Kinect 360 | `docs/AUTONOMY.md` |
| Платы Arduino | 4 | 5: четыре колеса и модуль датчиков | `firmware/` |
| Навигация | Nav2 и SLAM Toolbox | собственная карта занятости и планировщик `robot_control/navigation.py` | `docs/AUTONOMY.md`, `docs/navigation.md` |
| Знаки и светофор | распознаются камерой | не реализовано | `robot_control/stats.py`: для знаков и светофора «нет события от перцепции» |
| Порты устройств | платы находятся сами | назначаются вручную на странице «Устройства», автопоиска нет | `README.md`, раздел «Устройства робота» |
| Доступ к грузовому отсеку | QR и RFID | карты MIFARE с ролями, замок на сервоприводе | `firmware/sensor_module` |
| Бюджет | 207 200 ₽ | 369 170,85 ₽ (4 461,09 USD) | [смета](COST_ESTIMATE.md) |

Исправления внесены в [`README.md`](../README.md),
[`TECHNICAL_SPECIFICATION.md`](TECHNICAL_SPECIFICATION.md),
[`EXPLANATORY_NOTE_AVTOVAZ.md`](EXPLANATORY_NOTE_AVTOVAZ.md),
[`ELECTRICAL_AND_PINOUTS.md`](ELECTRICAL_AND_PINOUTS.md),
[`SERIAL_PROTOCOL.md`](SERIAL_PROTOCOL.md), [`CAUSAL_MAP.md`](CAUSAL_MAP.md),
[`SOFTWARE_ARCHITECTURE.md`](SOFTWARE_ARCHITECTURE.md), [`GUI.md`](GUI.md),
[`MAIN_SCREEN.md`](MAIN_SCREEN.md), [`SERVICE_CONSOLE.md`](SERVICE_CONSOLE.md),
[`firmware/README.md`](../firmware/README.md), [`pcb/README.md`](../pcb/README.md),
[`pcb/SCHEMATIC_CIRCUIT_DESIGN.md`](../pcb/SCHEMATIC_CIRCUIT_DESIGN.md),
[`ros2_ws/README.md`](../ros2_ws/README.md) и в три схемы каталога
[`assets/images/`](assets/images): архитектура системы, сборка поворотного
модуля, плата модуля.

---

## 4. Смета

Вместе с составом пересчитана стоимость. Один робот — **369 170,85 ₽
(4 461,09 USD)** по курсу 82,7536 ₽ за доллар:

| Статья | Сумма, ₽ | Сумма, USD |
| :--- | ---: | ---: |
| Комплектующие и материалы, 64 позиции | 298 170,85 | 3 603,12 |
| Работа, амортизация оборудования, интеграция | 71 000,00 | 857,97 |
| **Всего** | **369 170,85** | **4 461,09** |

Полный расчёт с фотографиями, ценами и продавцами —
[`COST_ESTIMATE.md`](COST_ESTIMATE.md). Цены взяты из поисковой выдачи
10.11.2026, страницы магазинов не открывались. 18 позиций —
допущения о модели или количестве: они перечислены в разделе 8 сметы.

---

## 5. Что осталось расхождением

Эти пункты не исправлены. Одни нельзя проверить без робота, другие требуют
отдельной работы.

**Проверить на роботе:**

- **Питание понижающих модулей XL4015.** Их вход рассчитан на напряжение до
  36 В, а заряженная батарея даёт до 43,8 В. От какой шины запитаны платы,
  нужно проверить до включения.
- **Питание Kinect 360.** Штатный адаптер сетевой, на 220 В. Как датчик запитан
  от бортовых 12 В, в документах не описано.
- **Модель драйвера тяги.** В документах — RioRand ZS-Z11H, на рендере платы —
  ZS-X11D1, в смете цена взята по ZS-X11H.
- **Момент мотор-колеса и тип шины.** 11,5 Н·м — оценка, тип шины в документах
  не зафиксирован.
- **Габариты, масса и клиренс.** Перенесены из прежней редакции без проверки.

**Доработать:**

- **Распознавание знаков и светофора.** В рабочем ПО его нет, а регламент
  задания его требует.
- **Маршрут длиной 50 м.** Одна поездка в ПО ограничена 18 м: трассу придётся
  проходить цепочкой целей.
- **Маячок и звуковой оповещатель.** Есть в смете и на схеме питания, но
  прошивка ими не управляет.
- **Датчик двери грузового отсека.** Не подключён: закрытие отсека прошивка
  не видит.

**Справочный код этого репозитория:**

- **`gui/` и `ros2_ws/`** написаны под двоичный протокол, которого в платах
  нет, и с роботом не работают. В их коде и тестах остались прежние значения:
  радиус колеса 0,127 м, база 0,55 м и колея 0,46 м, подпись «BLDC 260 Вт» на
  основном экране.
- **`pcb/`** — архив платы v1.0. Её выводы не совпадают с прошивкой, см.
  [`pcb/STATUS.md`](../pcb/STATUS.md).

---

## 6. Как повторить сверку

```bash
git clone https://github.com/danilka-revin/Zapret.git /tmp/Zapret
Z=/tmp/Zapret

# 1. Скетчи должны совпадать побайтно
cmp $Z/firmware/motor_wheel/motor_wheel.ino     firmware/motor_wheel/motor_wheel.ino
cmp $Z/firmware/sensor_module/sensor_module.ino firmware/sensor_module/sensor_module.ino

# 2. В симуляции и скриптах допустимы только отличия путей
diff $Z/tests/firmware_sim/sim.cpp        firmware/firmware_sim/sim.cpp
diff $Z/tests/firmware_sim/sensor_sim.cpp firmware/firmware_sim/sensor_sim.cpp
diff $Z/scripts/avr_upload.py             firmware/scripts/avr_upload.py

# 3. Тесты: прошивка и документы, смета
python3 -m venv .venv-test && .venv-test/bin/pip install pytest
.venv-test/bin/python -m pytest firmware/tests tools/estimate -q

# 4. PDF-копии документов
python3 -m venv .venv && .venv/bin/pip install -r tools/pdf/requirements.txt
.venv/bin/python tools/pdf/md2pdf.py && .venv/bin/python tools/pdf/md2pdf.py --check
```

Если скетч в Zapret изменился, скопируйте его сюда и обновите
[`firmware/README.md`](../firmware/README.md) и
[`SERIAL_PROTOCOL.md`](SERIAL_PROTOCOL.md). Тесты сверяют оба документа со
скетчами и не дадут им разойтись.
