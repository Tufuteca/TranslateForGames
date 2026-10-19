import sys
import time
import html
import signal
import threading

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLayout, QPushButton,
    QComboBox, QSpinBox, QSlider, QCheckBox, QGroupBox, QColorDialog,
    QSystemTrayIcon, QMenu, QGridLayout,
)

import numpy as np
import sounddevice as sd
from pynput import keyboard, mouse
from faster_whisper import WhisperModel
import argostranslate.package
import argostranslate.translate

import glossary
import settings

SAMPLE_RATE = 16000

MOUSE_NAMES = {
    "middle": "колёсико мыши",
    "right": "правая кнопка мыши",
    "x1": "боковая кнопка 1 (назад)",
    "x2": "боковая кнопка 2 (вперёд)",
}


def key_id(key):
    if isinstance(key, keyboard.Key):
        return f"key:{key.name}"
    vk = getattr(key, "vk", None)
    return f"vk:{vk}" if vk is not None else None


def mouse_id(button):
    name = getattr(button, "name", None)
    if not name or name in ("unknown", "left"):   # левую кнопку не назначаем
        return None
    return f"mouse:{name}"


def bind_display(iid):
    if not iid:
        return "— не назначено —"
    kind, _, name = iid.partition(":")
    if kind == "mouse":
        return MOUSE_NAMES.get(name, f"мышь: {name}")
    if kind == "vk":
        vk = int(name)
        return chr(vk) if 0x30 <= vk <= 0x5A else f"клавиша (код {vk})"
    return name.upper()


class Bridge(QObject):
    """Мост: из фоновых потоков безопасно отправляем сигналы в окна."""
    message = Signal(str, str, str)
    recording = Signal(bool)
    toggle_move = Signal()
    toggle_overlay = Signal()
    captured = Signal(str)


# ======================= ОВЕРЛЕЙ =======================
class Overlay(QWidget):
    def __init__(self, cfg, save_cb):
        super().__init__()
        self.cfg = cfg
        self.save_cb = save_cb
        self.move_mode = False
        self.recording = False
        self.user_hidden = False
        self.drag_offset = None
        self.items = []   # (label, вид, основной текст, подпись)

        self._apply_flags()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # не отбирать фокус у игры

        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(4, 4, 4, 4)
        self.box.setSpacing(4)
        self.box.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        self.rec_label = QLabel("● Запись...")
        self.hint_label = QLabel("Перетащи окно мышью.\nПотом нажми кнопку перемещения ещё раз")
        for lb in (self.rec_label, self.hint_label):
            lb.setWordWrap(True)
            self.box.addWidget(lb)
            lb.hide()

        self.refresh_all()
        o = cfg["overlay"]
        self.move(o["x"], o["y"])

    def _apply_flags(self):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if not self.move_mode:
            flags |= Qt.WindowTransparentForInput   # клики проходят сквозь окно
        self.setWindowFlags(flags)

    # ----- оформление -----
    def _style(self, color):
        o = self.cfg["overlay"]
        bg = QColor(o["bg_color"])
        alpha = int(o["bg_opacity"] * 255 / 100)
        return (
            f"color: {color}; background-color: rgba({bg.red()}, {bg.green()}, {bg.blue()}, {alpha});"
            f"border-radius: 8px; padding: 6px 10px; font-family: 'Segoe UI'; font-size: {o['font_size']}px;"
        )

    def _render(self, kind, main, sub):
        o = self.cfg["overlay"]
        color = {"me": o["color_me"], "ally": o["color_ally"]}.get(kind, o["color_info"])
        text = html.escape(main)
        if sub and o["show_original"]:
            small = max(8, int(o["font_size"] * 0.7))
            text += f"<br><span style='font-size:{small}px; color:#B0B0B0;'>{html.escape(sub)}</span>"
        return text, color

    def refresh_all(self):
        o = self.cfg["overlay"]
        for lb, color in ((self.rec_label, "#FF5555"), (self.hint_label, "#FFFFFF")):
            lb.setFixedWidth(o["width"])
            lb.setStyleSheet(self._style(color))
        for label, kind, main, sub in self.items:
            text, color = self._render(kind, main, sub)
            label.setText(text)
            label.setFixedWidth(o["width"])
            label.setStyleSheet(self._style(color))
        self.adjustSize()

    # ----- сообщения -----
    def add_message(self, kind, main, sub):
        o = self.cfg["overlay"]
        label = QLabel()
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        self.items.append((label, kind, main, sub))
        self.box.addWidget(label)
        while len(self.items) > o["max_lines"]:
            self._remove(self.items[0][0])
        self.refresh_all()
        QTimer.singleShot(o["show_seconds"] * 1000, lambda: self._remove(label))
        self._show()

    def _remove(self, label):
        for item in self.items:
            if item[0] is label:
                self.items.remove(item)
                self.box.removeWidget(label)
                label.deleteLater()
                break
        self.adjustSize()
        self._hide_if_idle()

    def _show(self):
        if not self.user_hidden:
            self.show()

    def _hide_if_idle(self):
        if not self.items and not self.move_mode and not self.recording:
            self.hide()

    def set_recording(self, on):
        self.recording = on
        self.rec_label.setVisible(on)
        self.adjustSize()
        if on:
            self._show()
        else:
            self._hide_if_idle()

    def toggle_overlay(self):
        self.user_hidden = not self.user_hidden
        if self.user_hidden:
            self.hide()
        elif self.items or self.move_mode or self.recording:
            self.show()

    # ----- перемещение -----
    def toggle_move_mode(self):
        self.move_mode = not self.move_mode
        pos = self.pos()
        self._apply_flags()
        self.hint_label.setVisible(self.move_mode)
        self.adjustSize()
        self.move(pos)
        self.user_hidden = False
        self.show()
        if not self.move_mode:
            self.cfg["overlay"]["x"], self.cfg["overlay"]["y"] = self.x(), self.y()
            self.save_cb()
            self._hide_if_idle()

    def mousePressEvent(self, e):
        if self.move_mode and e.button() == Qt.LeftButton:
            self.drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.move_mode and self.drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self.drag_offset)

    def mouseReleaseEvent(self, e):
        self.drag_offset = None


# ======================= ОКНО НАСТРОЕК =======================
class SettingsWindow(QWidget):
    BIND_TITLES = {
        "talk": "Говорить (держать)",
        "toggle_overlay": "Показать / скрыть оверлей",
        "move_mode": "Перемещение оверлея",
    }

    def __init__(self, cfg, overlay, save_cb, mics, on_mic_change):
        super().__init__()
        self.cfg = cfg
        self.overlay = overlay
        self.save_cb = save_cb
        self.on_mic_change = on_mic_change
        self.pending = None
        self.bind_labels = {}
        self.bind_buttons = {}
        self.setWindowTitle("Переводчик для игр — настройки")
        self.setMinimumWidth(480)
        root = QVBoxLayout(self)

        # --- Звук ---
        box = QGroupBox("Звук")
        grid = QGridLayout(box)
        self.mic_combo = QComboBox()
        self.mic_combo.addItem("(по умолчанию в Windows)", "")
        for _, name in mics:
            self.mic_combo.addItem(name, name)
        idx = self.mic_combo.findData(cfg["mic"])
        if idx >= 0:
            self.mic_combo.setCurrentIndex(idx)
        self.mic_combo.currentIndexChanged.connect(self._mic_changed)
        grid.addWidget(QLabel("Микрофон"), 0, 0)
        grid.addWidget(self.mic_combo, 0, 1)
        root.addWidget(box)

        # --- Управление ---
        box = QGroupBox("Управление (клавиши и кнопки мыши)")
        grid = QGridLayout(box)
        for row, action in enumerate(self.BIND_TITLES):
            grid.addWidget(QLabel(self.BIND_TITLES[action]), row, 0)
            lab = QLabel(bind_display(cfg["binds"].get(action, "")))
            lab.setStyleSheet("font-weight: bold;")
            btn = QPushButton("Назначить")
            btn.clicked.connect(lambda _=False, a=action: self._start_capture(a))
            grid.addWidget(lab, row, 1)
            grid.addWidget(btn, row, 2)
            self.bind_labels[action] = lab
            self.bind_buttons[action] = btn
        root.addWidget(box)

        # --- Оверлей ---
        box = QGroupBox("Оверлей")
        grid = QGridLayout(box)
        o = cfg["overlay"]
        rows = [
            ("Размер шрифта", self._spin("font_size", 10, 60)),
            ("Ширина окна", self._spin("width", 200, 1400)),
            ("Показывать, секунд", self._spin("show_seconds", 3, 60)),
            ("Максимум строк", self._spin("max_lines", 1, 10)),
            ("Цвет моего текста", self._color_button("color_me")),
            ("Цвет текста союзника", self._color_button("color_ally")),
            ("Цвет фона", self._color_button("bg_color")),
        ]
        for row, (title, widget) in enumerate(rows):
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(widget, row, 1)
        row = len(rows)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setValue(o["bg_opacity"])
        self.opacity_label = QLabel(f"{o['bg_opacity']}%")
        self.opacity.valueChanged.connect(self._opacity_changed)
        grid.addWidget(QLabel("Прозрачность фона (плотность)"), row, 0)
        grid.addWidget(self.opacity, row, 1)
        grid.addWidget(self.opacity_label, row, 2)
        self.original_check = QCheckBox("Показывать под переводом распознанный текст")
        self.original_check.setChecked(o["show_original"])
        self.original_check.toggled.connect(lambda v: self._set("show_original", v))
        grid.addWidget(self.original_check, row + 1, 0, 1, 3)
        move_btn = QPushButton("Переместить / зафиксировать оверлей")
        move_btn.clicked.connect(self.overlay.toggle_move_mode)
        preview_btn = QPushButton("Показать пример")
        preview_btn.clicked.connect(
            lambda: self.overlay.add_message("me", "Example: hot zone ahead, push now", "Пример: хот зона впереди, пушим")
        )
        grid.addWidget(move_btn, row + 2, 0, 1, 2)
        grid.addWidget(preview_btn, row + 2, 2)
        root.addWidget(box)

        note = QLabel("Окно можно закрыть: программа продолжит работать в трее (значок у часов).")
        note.setWordWrap(True)
        root.addWidget(note)

    # ----- оверлей -----
    def _set(self, key, value):
        self.cfg["overlay"][key] = value
        self.save_cb()
        self.overlay.refresh_all()

    def _spin(self, key, lo, hi):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(self.cfg["overlay"][key])
        sp.valueChanged.connect(lambda v, k=key: self._set(k, v))
        return sp

    def _paint(self, btn, color):
        btn.setText(color)
        fg = "#000000" if QColor(color).lightness() > 128 else "#FFFFFF"
        btn.setStyleSheet(f"background-color: {color}; color: {fg};")

    def _color_button(self, key):
        btn = QPushButton()
        self._paint(btn, self.cfg["overlay"][key])
        btn.clicked.connect(lambda _=False, k=key, b=btn: self._pick(k, b))
        return btn

    def _pick(self, key, btn):
        c = QColorDialog.getColor(QColor(self.cfg["overlay"][key]), self, "Выбери цвет")
        if c.isValid():
            self._set(key, c.name())
            self._paint(btn, c.name())

    def _opacity_changed(self, v):
        self.opacity_label.setText(f"{v}%")
        self._set("bg_opacity", v)

    # ----- микрофон -----
    def _mic_changed(self):
        self.cfg["mic"] = self.mic_combo.currentData() or ""
        self.save_cb()
        self.on_mic_change()

    # ----- назначение кнопок -----
    def _start_capture(self, action):
        self._reset_pending_ui()
        self.pending = action
        self.bind_buttons[action].setText("Нажми кнопку... (Esc — отмена)")
        begin_capture()

    def _reset_pending_ui(self):
        if self.pending:
            self.bind_buttons[self.pending].setText("Назначить")
            self.pending = None

    def on_captured(self, iid):
        action = self.pending
        self._reset_pending_ui()
        end_capture()
        if not action or not iid:
            return
        binds = self.cfg["binds"]
        for other, val in binds.items():
            if other != action and val == iid:
                binds[other] = binds.get(action, "")   # меняем местами
        binds[action] = iid
        self.save_cb()
        for a, lab in self.bind_labels.items():
            lab.setText(bind_display(binds.get(a, "")))

    def closeEvent(self, e):
        if self.pending:
            self.on_captured("")
        e.ignore()
        self.hide()   # прячем в трей


# ======================= ЗВУК, РАСПОЗНАВАНИЕ, ПЕРЕВОД =======================
cfg = None
bridge = None
model = None
translator = None
ready = False
chunks = []
recording = False
held = set()
capturing = False
stream = None
kb_listener = None
mouse_listener = None
tray = None


def begin_capture():
    global capturing
    capturing = True


def end_capture():
    global capturing
    capturing = False


def list_mics():
    result = []
    host = sd.default.hostapi
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == host:
            result.append((i, d["name"]))
    return result


def restart_stream():
    global stream
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        stream = None
    device = None
    for i, name in list_mics():
        if name == cfg["mic"]:
            device = i
            break
    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                callback=audio_callback, device=device)
        stream.start()
    except Exception as e:
        print(f"Ошибка микрофона: {e}")
        bridge.message.emit("info", f"Ошибка микрофона: {e}", "")


def audio_callback(indata, frame_count, time_info, status):
    if recording:
        chunks.append(indata.copy())


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
        model = WhisperModel(cfg["model_size"], device="cpu", compute_type="int8", cpu_threads=4)
        ensure_translation()
        translator = get_translator()
        translator.translate("Привет")   # прогрев
        glossary.load_glossary()
        ready = True
        print("Готово.")
        bridge.message.emit("info", "Готово! Зажми кнопку записи и говори", "")
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


def action_for(iid):
    for action, bound in cfg["binds"].items():
        if bound and bound == iid:
            return action
    return None


def start_recording():
    global recording
    chunks.clear()
    recording = True
    bridge.recording.emit(True)


def stop_recording():
    global recording
    recording = False
    bridge.recording.emit(False)
    if not chunks:
        return
    audio = np.concatenate(chunks).flatten().astype(np.float32)
    if len(audio) < SAMPLE_RATE * 0.3:
        print("(слишком коротко)")
        return
    threading.Thread(target=process, args=(audio,), daemon=True).start()


def handle_input(iid, down):
    global capturing
    if iid is None:
        return
    if down:
        if iid in held:          # автоповтор зажатой клавиши
            return
        held.add(iid)
        if capturing:
            capturing = False
            bridge.captured.emit("" if iid == "key:esc" else iid)
            return
        action = action_for(iid)
        if action == "talk" and ready and not recording:
            start_recording()
        elif action == "toggle_overlay":
            bridge.toggle_overlay.emit()
        elif action == "move_mode":
            bridge.toggle_move.emit()
    else:
        held.discard(iid)
        if action_for(iid) == "talk" and recording:
            stop_recording()


def on_key_press(key):
    handle_input(key_id(key), True)


def on_key_release(key):
    handle_input(key_id(key), False)


def on_click(x, y, button, pressed):
    handle_input(mouse_id(button), pressed)


def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#4C8BF5"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    font = p.font()
    font.setPointSize(26)
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "T")
    p.end()
    return QIcon(pm)


def shutdown():
    for obj in (kb_listener, mouse_listener):
        try:
            obj.stop()
        except Exception:
            pass
    try:
        stream.stop()
        stream.close()
    except Exception:
        pass


def main():
    global cfg, bridge, kb_listener, mouse_listener, tray
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # Ctrl+C в консоли закрывает программу
    cfg, first_run = settings.load()
    save_cb = lambda: settings.save(cfg)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    bridge = Bridge()
    overlay = Overlay(cfg, save_cb)
    win = SettingsWindow(cfg, overlay, save_cb, list_mics(), restart_stream)

    bridge.message.connect(overlay.add_message)
    bridge.recording.connect(overlay.set_recording)
    bridge.toggle_move.connect(overlay.toggle_move_mode)
    bridge.toggle_overlay.connect(overlay.toggle_overlay)
    bridge.captured.connect(win.on_captured)

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("Переводчик для игр")
    menu = QMenu()
    menu.addAction("Настройки", lambda: (win.show(), win.raise_(), win.activateWindow()))
    menu.addAction("Показать / скрыть оверлей", overlay.toggle_overlay)
    menu.addSeparator()
    menu.addAction("Выход", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: win.show() if reason == QSystemTrayIcon.DoubleClick else None)
    tray.show()

    restart_stream()
    kb_listener = keyboard.Listener(on_press=on_key_press, on_release=on_key_release)
    mouse_listener = mouse.Listener(on_click=on_click)
    kb_listener.start()
    mouse_listener.start()
    threading.Thread(target=load_everything, daemon=True).start()

    app.aboutToQuit.connect(shutdown)
    win.show()   # пока отлаживаем, окно настроек показываем при каждом запуске
    if first_run:
        save_cb()
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Системный трей недоступен")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()