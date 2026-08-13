"""Точка входа (CLI).

  python run.py post <module> [city]   # опубликовать один модуль сейчас (тест)
  python run.py serve                  # запустить планировщик (программа дня)

Модули: weather, currency
Город по умолчанию: novorossiysk
"""
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from engine import post_module

USAGE = __doc__


def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE)
        return

    cmd = sys.argv[1]
    if cmd == "post":
        module = sys.argv[2] if len(sys.argv) > 2 else "currency"
        city = sys.argv[3] if len(sys.argv) > 3 else "novorossiysk"
        post_module(module, city)
    elif cmd == "serve":
        from scheduler import start
        start()
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
