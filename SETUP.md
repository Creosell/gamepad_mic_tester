# Gamepad Mic Tester — настройка окружения

## Зависимости Python

```bash
cd /home/qa/Downloads/gamepad_mic_tester
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Системные зависимости

```bash
sudo apt install sox alsa-utils bluez
```

## Права без полного sudo

Скрипт запускается **без sudo**. Требуется одна узкая операция с root-правами —
удаление кэша имён BlueZ после unpair. Без этого BlueZ запоминает устройство
и может подключиться к нему без режима pairing, что недопустимо при массовой инспекции.

### Создать правило sudoers (один раз):

```bash
echo "qa ALL=(ALL) NOPASSWD: /usr/bin/rm /var/lib/bluetooth/*/cache/*" \
    | sudo tee /etc/sudoers.d/gamepad-mic-tester
sudo chmod 440 /etc/sudoers.d/gamepad-mic-tester
sudo visudo -c   # проверка синтаксиса — должно вывести "parsed OK"
```

Это разрешает пользователю `qa` без пароля удалять **только** файлы кэша BlueZ.
Ничего другого правило не затрагивает.

### Проверить:

```bash
sudo rm /var/lib/bluetooth/test/cache/test 2>/dev/null; echo $?
# Должно вернуть 0 или 1 (файл не найден), но НЕ запросить пароль
```

## Запуск

```bash
.venv/bin/python franken_test.py
.venv/bin/python franken_test.py --no-unpair   # не анпейрить после теста
.venv/bin/python franken_test.py --reconnect   # подключиться к уже сбондированному
```

## Почему важен кэш BlueZ

После `RemoveDevice()` bluetoothd удаляет устройство из памяти, но оставляет
файл `/var/lib/bluetooth/<adapter>/cache/<device>`. При следующем сканировании
BlueZ распознаёт устройство по кэшу имён и может подключиться к нему без
режима pairing. Это критично в условиях инспекции — несколько геймпадов
рядом, случайное нажатие может вызвать нежелательное подключение.
