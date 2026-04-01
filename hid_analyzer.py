#!/usr/bin/env python3
"""
HID Analyzer — перечисление и мониторинг HID-устройств в Windows

Usage:
    python hid_analyzer.py [--index N] [--raw]
"""

import argparse
import sys
import hid

def enumerate_devices():
    devices = hid.enumerate()
    if not devices:
        print("HID-устройства не найдены.")
        return []

    print("\n=== Список HID-устройств ===")
    for i, d in enumerate(devices):
        print(f"[{i}] VID: {d['vendor_id']:04X}, PID: {d['product_id']:04X}")
        print(f"    Manufacturer: {d.get('manufacturer_string', 'N/A')}")
        print(f"    Product:      {d.get('product_string', 'N/A')}")
        print(f"    Serial:       {d.get('serial_number', 'N/A')}")
        print(f"    Path:         {d['path']}")
        print()
    return devices

def monitor_device(device_info, raw_output=False):
    try:
        dev = hid.device()
        dev.open_path(device_info['path'])
        print(f"\nОткрыто устройство: VID={device_info['vendor_id']:04X}, PID={device_info['product_id']:04X}")
        print("Нажмите Ctrl+C для выхода.\n")
        while True:
            data = dev.read(64, timeout=1000)
            if data:
                if raw_output:
                    hex_str = ' '.join(f'{b:02X}' for b in data)
                    print(f"Получено {len(data)} байт: {hex_str}")
                else:
                    print(f"Получено {len(data)} байт: {data}")
    except KeyboardInterrupt:
        print("\nЗавершено.")
    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        if 'dev' in locals():
            dev.close()

def main():
    parser = argparse.ArgumentParser(description="Анализатор HID-устройств")
    parser.add_argument("--index", type=int, help="Индекс устройства для мониторинга (из списка)")
    parser.add_argument("--raw", action="store_true", help="Выводить данные в шестнадцатеричном виде")
    args = parser.parse_args()

    devices = enumerate_devices()
    if not devices:
        sys.exit(1)

    if args.index is not None:
        if 0 <= args.index < len(devices):
            monitor_device(devices[args.index], raw_output=args.raw)
        else:
            print(f"Неверный индекс. Доступны индексы 0..{len(devices)-1}")
    else:
        while True:
            try:
                choice = input("Введите индекс устройства для мониторинга (или q для выхода): ")
                if choice.lower() == 'q':
                    break
                idx = int(choice)
                if 0 <= idx < len(devices):
                    monitor_device(devices[idx], raw_output=args.raw)
                else:
                    print(f"Индекс вне диапазона (0..{len(devices)-1})")
            except ValueError:
                print("Введите число или q")
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    main()