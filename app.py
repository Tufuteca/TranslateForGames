"""Точка входа: собирает окна, ввод и распознавание вместе.

Здесь только «склейка». Правим по договорённости: логика лежит в speech.py и inputs.py,
внешний вид в ui_overlay.py и ui_settings.py.
"""
import signal
import sys
import threading

import logsetup

logsetup.setup_logging()   # первым делом: ошибки импорта тоже попадут в translator.log

import os

# Порядок важен: PySide6 импортируем раньше speech (там torch), иначе на Windows бывают конфликты DLL
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

import settings
from ui_overlay import Overlay
from ui_settings import SettingsWindow, make_icon
from inputs import InputManager
import speech
from version import __version__


class Bridge(QObject):
    """Мост: из фоновых потоков безопасно отправляем сигналы в окна."""
    message = Signal(str, str, str)
    recording = Signal(bool)
    toggle_move = Signal()
    toggle_overlay = Signal()
    captured = Signal(str)


def open_log():
    try:
        os.startfile(logsetup.LOG_PATH)
    except Exception as e:
        print(f"Не удалось открыть лог: {e}")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # Ctrl+C в консоли закрывает программу
    cfg, first_run = settings.load()
    save_cb = lambda: settings.save(cfg)
    print(f"GameTranslator v{__version__}")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bridge = Bridge()
    overlay = Overlay(cfg, save_cb)
    engine = speech.SpeechEngine(cfg, bridge.message.emit, bridge.recording.emit)

    def on_press(action):
        if action == "talk":
            engine.start_recording()
        elif action == "toggle_overlay":
            bridge.toggle_overlay.emit()
        elif action == "move_mode":
            bridge.toggle_move.emit()

    def on_release(action):
        if action == "talk":
            engine.stop_recording()

    inputs = InputManager(cfg, on_press, on_release, bridge.captured.emit)
    win = SettingsWindow(
        cfg, overlay, save_cb, speech.list_mics(), engine.restart_stream,
        inputs.begin_capture, inputs.end_capture,
    )

    bridge.message.connect(overlay.add_message)
    bridge.recording.connect(overlay.set_recording)
    bridge.toggle_move.connect(overlay.toggle_move_mode)
    bridge.toggle_overlay.connect(overlay.toggle_overlay)
    bridge.captured.connect(win.on_captured)

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(f"Переводчик для игр v{__version__}")
    menu = QMenu()
    menu.addAction("Настройки", lambda: (win.show(), win.raise_(), win.activateWindow()))
    menu.addAction("Показать / скрыть оверлей", overlay.toggle_overlay)
    menu.addAction("Открыть лог", open_log)
    menu.addSeparator()
    menu.addAction("Выход", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: win.show() if reason == QSystemTrayIcon.DoubleClick else None)
    tray.show()

    engine.restart_stream()
    inputs.start()
    threading.Thread(target=engine.load_models, daemon=True).start()

    def shutdown():
        inputs.stop()
        engine.shutdown()

    app.aboutToQuit.connect(shutdown)
    win.show()   # пока отлаживаем, окно настроек показываем при каждом запуске
    if first_run:
        save_cb()
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Системный трей недоступен")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
