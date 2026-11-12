# ROS 2 workspace

Пакеты `rus_slam_*`: кинематика и мост модулей, одометрия, лидар, восприятие,
навигация, безопасность, интерфейсы и запуск.

Мост `rus_slam_base/module_link.py` и `protocol.py` пока используют
**бинарные** кадры 10/16 Б (`0xAA 0x55` / `0xBB 0x44`), которых прошивка робота
не выдаёт. Реальная прошивка — [`firmware/motor_wheel`](../firmware/motor_wheel/motor_wheel.ino)
(fw26), протокол — [`../docs/SERIAL_PROTOCOL.md`](../docs/SERIAL_PROTOCOL.md),
план перевода моста — [`../docs/SOFTWARE_ARCHITECTURE.md`](../docs/SOFTWARE_ARCHITECTURE.md), §10.

На роботе эти пакеты не запущены: там работает бортовое ПО
[Zapret](https://github.com/danilka-revin/Zapret) со своим планировщиком, без Nav2.
Геометрия в [`robot.yaml`](src/rus_slam_bringup/config/robot.yaml) и URDF — прежняя
проектная: радиус колеса 0,127 м, база 0,55 м, колея 0,46 м. У робота колесо 8,5″
(радиус около 0,108 м), база 0,50 м, колея 0,38 м — см.
[`../docs/ZAPRET_SYNC.md`](../docs/ZAPRET_SYNC.md). Перед запуском стека на роботе
эти значения нужно заменить.

Сборка (если нужна для справки):

```bash
cd ros2_ws
colcon build --symlink-install && source install/setup.bash
ros2 launch rus_slam_bringup robot.launch.py sim:=true
```
