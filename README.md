# Gamepad Mic Tester

Набор инструментов для тестирования BLE-геймпада **Realtek G100-4722** — проверка микрофона и кнопок.

## Устройство

| Поле     | Значение                 |
|----------|--------------------------|
| BLE имя  | `GAME`                   |
| MAC      | `F4:22:7A:4A:AA:E0`      |
| Модель   | G100-4722 (Realtek BT)   |
| Прошивка | V1.4.0                   |
| USB      | Только зарядка, данных нет |

---

## Файлы

| Файл                   | Назначение                                              |
|------------------------|---------------------------------------------------------|
| `gamepad_mic_tester.py`| Тест микрофона — запись, конвертация, воспроизведение, лог |
| `gamepad_tester.py`    | Тест кнопок — HID probe/test + BLE CC-кнопки           |
| `dev_tools.py`         | Отладка — сниффинг BLE, анализ аудио                   |
| `hid_analyzer.py`      | Анализатор HID-отчётов                                 |
| `raw_input_probe.py`   | Windows Raw Input probe (тупик, оставлен для справки)  |

---

## Тест микрофона (`gamepad_mic_tester.py`)

### Алгоритм

1. **Подключение** — сканирование устройства по имени `GAME`; если не найдено сканером — прямое подключение по известному MAC (сопряжённое устройство)
2. **Информация об устройстве** — модель, серийный номер, версии FW/HW, заряд батареи (BLE Device Information Service)
3. **Прогрев** — один холостой цикл `GET_CAPS → START → STOP` (первый цикл после сопряжения аудио не даёт)
4. **Цикл записи** — повторяется до выхода пользователя:
   - Отправить `GET_CAPS` (`0x0C 0x00`) на `ab5e0002`, дождаться подтверждения на `ab5e0004`
   - Отправить `START` (`0x0A 0x00`) — устройство начинает стримить IMA ADPCM-фреймы на `ab5e0003`
   - Собирать фреймы в течение `--seconds` секунд (по умолчанию: 5)
   - Отправить `STOP` (`0x0B 0x00`)
   - Конвертировать сырой IMA → WAV через `sox` и воспроизвести
   - Запрос: `[Enter] — повтор  [Q] — выход`
5. **Логирование** — запись в `logs/history.log`; обновление `logs/tested_devices.json`

### Запуск

```bash
python gamepad_mic_tester.py            # 5-секундная запись
python gamepad_mic_tester.py --seconds 10
```

### Установка зависимостей

```bash
pip install bleak
```

`sox` должен быть в PATH (только Windows):
- Скачать с https://sourceforge.net/projects/sox/
- Добавить папку установки в системный PATH

> **Платформы:** скрипт тестировался и работает на **Windows**.
> Linux-поддержка аудио не реализована (BlueZ не даёт надёжно управлять streaming-состоянием устройства через bleak).

---

## Тест кнопок (`gamepad_tester.py`)

### Режимы

```bash
python gamepad_tester.py list          # перечисление HID-устройств
python gamepad_tester.py probe         # дамп сырых HID-отчётов
python gamepad_tester.py test          # живая панель кнопок (Windows)
python gamepad_tester.py evdev         # все кнопки включая CC (только Linux)
python gamepad_tester.py ble           # BLE-подписка + дамп сервисов
python gamepad_tester.py bleprobe      # зондирование BLE-команд
```

### Статус по платформам

| Группа кнопок                      | Windows               | Linux (режим `evdev`)          |
|------------------------------------|-----------------------|--------------------------------|
| A / B / X / Y                      | ✓ hidapi              | ✓ event13 (KEY_304–308)        |
| L1 / R1 / L2 / R2                  | ✓ hidapi              | ✓ event13                      |
| L3 / R3 / Share / Options          | ✓ hidapi              | ✓ event13                      |
| D-pad                              | ✓ hidapi              | ✓ ABS_HAT0X/Y                  |
| Левый / правый стик                | ✓ hidapi              | ✓ ABS_X/Y/Z/RZ                 |
| **Back / Home / SalutLogo / Play** | **✗ заблокировано**   | ✓ event12 (Consumer Controls)  |

> **CC-кнопки на Windows навсегда заблокированы** — BLE HID-драйвер Windows
> перехватывает весь HID-сервис (0x1812) на уровне ядра до любого user-space API.

### Настройка evdev (Linux)

```bash
sudo apt install python3-evdev
sudo usermod -aG input $USER   # перелогиниться после этого
python3 gamepad_tester.py evdev
```

---

## Логи

Все логи пишутся в папку `logs/` (создаётся автоматически):

| Файл                     | Содержимое                                                        |
|--------------------------|-------------------------------------------------------------------|
| `history.log`            | По одной строке на запись: MAC, модель, FW, батарея, фреймы, статус |
| `tested_devices.json`    | По устройству: первый/последний тест, количество тестов           |
| `mic_test_*.log`         | Отладочный лог сессии                                             |
