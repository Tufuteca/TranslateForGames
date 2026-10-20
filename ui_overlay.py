"""Оверлей с переводом поверх игры."""
import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QLayout


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
