"""Клавиатура и мышь: глобальные нажатия, назначение кнопок.

Модуль ничего не знает про окна и распознавание: он только сообщает «нажато действие X».
"""
from pynput import keyboard, mouse

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
    """Человекочитаемое название кнопки для окна настроек."""
    if not iid:
        return "— не назначено —"
    kind, _, name = iid.partition(":")
    if kind == "mouse":
        return MOUSE_NAMES.get(name, f"мышь: {name}")
    if kind == "vk":
        vk = int(name)
        return chr(vk) if 0x30 <= vk <= 0x5A else f"клавиша (код {vk})"
    return name.upper()


class InputManager:
    """Слушает клавиатуру и мышь и превращает нажатия в действия из cfg["binds"].

    on_press(action)    - действие нажато
    on_release(action)  - действие отпущено
    on_captured(iid)    - в режиме назначения пришла кнопка ("" = отмена по Esc)
    Все три вызываются из потока слушателя, а не из потока интерфейса.
    """

    def __init__(self, cfg, on_press, on_release, on_captured):
        self.cfg = cfg
        self.on_press = on_press
        self.on_release = on_release
        self.on_captured = on_captured
        self.held = set()
        self.capturing = False
        self._kb = None
        self._mouse = None

    def start(self):
        self._kb = keyboard.Listener(
            on_press=lambda key: self._handle(key_id(key), True),
            on_release=lambda key: self._handle(key_id(key), False),
        )
        self._mouse = mouse.Listener(
            on_click=lambda x, y, button, pressed: self._handle(mouse_id(button), pressed),
        )
        self._kb.start()
        self._mouse.start()

    def stop(self):
        for listener in (self._kb, self._mouse):
            try:
                listener.stop()
            except Exception:
                pass

    def begin_capture(self):
        self.capturing = True

    def end_capture(self):
        self.capturing = False

    def action_for(self, iid):
        for action, bound in self.cfg["binds"].items():
            if bound and bound == iid:
                return action
        return None

    def _handle(self, iid, down):
        if iid is None:
            return
        if down:
            if iid in self.held:          # автоповтор зажатой клавиши
                return
            self.held.add(iid)
            if self.capturing:
                self.capturing = False
                self.on_captured("" if iid == "key:esc" else iid)
                return
            action = self.action_for(iid)
            if action:
                self.on_press(action)
        else:
            self.held.discard(iid)
            action = self.action_for(iid)
            if action:
                self.on_release(action)
