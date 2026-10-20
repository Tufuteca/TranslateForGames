"""Окно настроек и значок в трее.

Окно получает всё нужное через параметры конструктора."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
    QCheckBox, QGroupBox, QColorDialog, QGridLayout,
)

from inputs import bind_display


class SettingsWindow(QWidget):
    BIND_TITLES = {
        "talk": "Говорить (держать)",
        "toggle_overlay": "Показать / скрыть оверлей",
        "move_mode": "Перемещение оверлея",
    }

    def __init__(self, cfg, overlay, save_cb, mics, on_mic_change, begin_capture, end_capture):
        super().__init__()
        self.cfg = cfg
        self.overlay = overlay
        self.save_cb = save_cb
        self.on_mic_change = on_mic_change
        self.begin_capture = begin_capture   # начать ожидание нажатия (из inputs.py)
        self.end_capture = end_capture
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
        self.begin_capture()

    def _reset_pending_ui(self):
        if self.pending:
            self.bind_buttons[self.pending].setText("Назначить")
            self.pending = None

    def on_captured(self, iid):
        action = self.pending
        self._reset_pending_ui()
        self.end_capture()
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
