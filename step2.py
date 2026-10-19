import sys
import os
import json
import time
import html
import signal
import threading

# PySide6 импортируем первым: так меньше шансов на конфликт DLL с torch на Windows
from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QLayout

import numpy as np
import sounddevice as sd
from pynput import keyboard
from faster_whisper import WhisperModel
import argostranslate.package
import argostranslate.translate

import glossary

# ---------- настройки (позже уедут в окно настроек) ----------
SAMPLE_RATE = 16000
HOTKEY = keyboard.Key.f9       # зажми и говори
MOVE_KEY = keyboard.Key.f8     # режим перемещения окна
QUIT_KEY = keyboard.Key.f10    # выход
MODEL_SIZE = "small"

SHOW_SECONDS = 10
MAX_LINES = 5
OVERLAY_WIDTH = 520
FONT_SIZE = 20
SHOW_ORIGINAL = True           # показывать под переводом то, что распознано
COLOR_ME = "#7CFC9A"
COLOR_ALLY = "#8AB4FF"
COLOR_INFO = "#E0E0E0"
BACKGROUND = "rgba(0, 0, 0, 170)"
POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_pos.json")


class Bridge(QObject):
    """Мост: из фоновых потоков безопасно отправляем сигналы в окно."""
    message = Signal(str, str, str)   # вид, основной текст, подпись
    recording = Signal(bool)
    toggle_move = Signal()
    quit = Signal()


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.move_mode = False
        self.recording = False
        self.drag_offset = None
        self.labels = []

        self._apply_flags()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # не отбирать фокус у игры

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(4, 4, 4, 4)
        self.box.setSpacing(4)
        self.box.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        self.rec_label = self._make_label("● Запись...", "#FF5555")
        self.hint_label = self._make_label("Перетащи окно мышью.\nF8 — зафиксировать", "#FFFFFF")
        self.rec_label.hide()
        self.hint_label.hide()
        self.box.addWidget(self.rec_label)
        self.box.addWidget(self.hint_label)

        self._load_position()

    def _apply_flags(self):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                 | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        if not self.move_mode:
            flags |= Qt.WindowTransparentForInput  # клики проходят сквозь окно
        self.setWindowFlags(flags)

    def _make_label(self, text, color, rich=False):
        label = QLabel(text)
        if rich:
            label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setFixedWidth(OVERLAY_WIDTH)
        label.setStyleSheet(
            f"color: {color}; background-color: {BACKGROUND}; border-radius: 8px;"
            f"padding: 6px 10px; font-family: 'Segoe UI'; font-size: {FONT_SIZE}px;"
        )
        return label

    # ----- сообщения -----
    def add_message(self, kind, main, sub):
        color = {"me": COLOR_ME, "ally": COLOR_ALLY}.get(kind, COLOR_INFO)
        text = html.escape(main)
        if sub and SHOW_ORIGINAL:
            small = int(FONT_SIZE * 0.7)
            text += f"<br><span style='font-size:{small}px; color:#B0B0B0;'>{html.escape(sub)}</span>"
        label = self._make_label(text, color, rich=True)
        self.box.addWidget(label)
        self.labels.append(label)
        while len(self.labels) > MAX_LINES:
            self._remove(self.labels[0])
        QTimer.singleShot(SHOW_SECONDS * 1000, lambda: self._remove(label))
        self.show()

    def _remove(self, label):
        if label in self.labels:
            self.labels.remove(label)
            self.box.removeWidget(label)
            label.deleteLater()
        self._hide_if_idle()

    def _hide_if_idle(self):
        if not self.labels and not self.move_mode and not self.recording:
            self.hide()

    def set_recording(self, on):
        self.recording = on
        self.rec_label.setVisible(on)
        if on:
            self.show()
        else:
            self._hide_if_idle()

    # ----- перемещение -----
    def toggle_move_mode(self):
        self.move_mode = not self.move_mode
        pos = self.pos()
        self._apply_flags()
        self.hint_label.setVisible(self.move_mode)
        self.move(pos)
        self.show()
        if not self.move_mode:
            self._save_position()
            self._hide_if_idle()

    def mousePressEvent(self, e):
        if self.move_mode and e.button() == Qt.LeftButton:
            self.drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.move_mode and self.drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, e):
        self.drag_offset = None

    def _save_position(self):
        try:
            with open(POS_FILE, "w") as f:
                json.dump({"x": self.x(), "y": self.y()}, f)
        except Exception as e:
            print(f"[не удалось сохранить позицию: {e}]")

    def _load_position(self):
        x, y = 100, 100
        try:
            with open(POS_FILE) as f:
                data = json.load(f)
            x, y = int(data["x"]), int(data["y"])
        except Exception:
            pass
        self.move(x, y)


# ---------- распознавание и перевод ----------
bridge = None
model = None
translator = None
ready = False
chunks = []
recording = False
pressed = set()
stream = None
listener = None


def ensure_translation(from_code="ru", to_code="en"):
    langs = {l.code: l for l in argostranslate.translate.get_installed_languages()}
    if from_code in langs and to_code in langs:
        if langs[from_code].get_translation(langs[to_code]):
            return
    print("Скачиваю языковой пакет перевода (один раз)...")
    argostranslate.package.update_package_index()
    packages = argostranslate.package.get_available_packages()
    pkg = next(p for p in packages if p.from_code == from_code and p.to_code == to_code)
    argostranslate.package.install_from_path(pkg.download())


def get_translator(from_code="ru", to_code="en"):
    langs = {l.code: l for l in argostranslate.translate.get_installed_languages()}
    return langs[from_code].get_translation(langs[to_code])


def load_everything():
    global model, translator, ready
    try:
        bridge.message.emit("info", "Загружаю модели, подожди...", "")
        print("Загружаю Whisper...")
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=4)
        ensure_translation()
        translator = get_translator()
        translator.translate("Привет")  # прогрев
        glossary.load_glossary()
        ready = True
        print("Готово. Зажми F9, скажи фразу по-русски, отпусти.")
        bridge.message.emit("info", "Готово! Зажми F9 и говори", "")
    except Exception as e:
        print(f"Ошибка загрузки: {e}")
        bridge.message.emit("info", f"Ошибка загрузки: {e}", "")


def process(audio):
    glossary.load_glossary()
    t0 = time.time()
    segments, _ = model.transcribe(
        audio, language="ru", vad_filter=True, beam_size=1,
        initial_prompt=glossary.build_prompt(),
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        print("(ничего не распознано)")
        return
    t1 = time.time()
    fixed = glossary.apply_glossary(text)
    translated = translator.translate(fixed)
    t2 = time.time()
    print(f"RU: {text}")
    if fixed != text:
        print(f"RU (после словаря): {fixed}")
    print(f"EN: {translated}")
    print(f"[распознавание {t1 - t0:.1f} с, перевод {t2 - t1:.1f} с]\n")
    bridge.message.emit("me", translated, fixed)


def audio_callback(indata, frame_count, time_info, status):
    if recording:
        chunks.append(indata.copy())


def on_press(key):
    global recording
    if key in pressed:       # игнорируем автоповтор зажатой клавиши
        return
    pressed.add(key)
    if key == MOVE_KEY:
        bridge.toggle_move.emit()
    elif key == QUIT_KEY:
        bridge.quit.emit()
    elif key == HOTKEY and ready and not recording:
        chunks.clear()
        recording = True
        bridge.recording.emit(True)


def on_release(key):
    global recording
    pressed.discard(key)
    if key == HOTKEY and recording:
        recording = False
        bridge.recording.emit(False)
        if not chunks:
            return
        audio = np.concatenate(chunks).flatten().astype(np.float32)
        if len(audio) < SAMPLE_RATE * 0.3:
            print("(слишком коротко)")
            return
        threading.Thread(target=process, args=(audio,), daemon=True).start()


def shutdown():
    try:
        listener.stop()
        stream.stop()
        stream.close()
    except Exception:
        pass


def main():
    global bridge, stream, listener
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C в консоли закрывает программу
    app = QApplication(sys.argv)
    overlay = Overlay()
    bridge = Bridge()
    bridge.message.connect(overlay.add_message)
    bridge.recording.connect(overlay.set_recording)
    bridge.toggle_move.connect(overlay.toggle_move_mode)
    bridge.quit.connect(app.quit)

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=audio_callback)
    stream.start()
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    threading.Thread(target=load_everything, daemon=True).start()

    app.aboutToQuit.connect(shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()