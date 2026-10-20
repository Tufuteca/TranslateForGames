"""Лог-файл: всё, что программа печатает (и необработанные ошибки), дублируется в translator.log."""
import datetime
import faulthandler
import os
import sys
import threading
import traceback

import settings

LOG_PATH = os.path.join(settings.APP_DIR, "translator.log")


class Tee:
    """Пишет и в лог-файл, и в консоль (если она есть)."""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]
        self.encoding = "utf-8"

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def setup_logging():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")   # лог не растёт бесконечно
    except OSError:
        pass
    log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(log_file, sys.__stdout__)
    sys.stderr = Tee(log_file, sys.__stderr__)
    faulthandler.enable(file=log_file)   # запишет и «жёсткие» падения
    print(f"\n===== запуск {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====")

    def log_exception(exc_type, exc, tb):
        print("НЕПЕРЕХВАЧЕННАЯ ОШИБКА:")
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = log_exception
    threading.excepthook = lambda a: log_exception(a.exc_type, a.exc_value, a.exc_traceback)
