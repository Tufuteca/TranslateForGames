import json
import os
import sys

# Папка программы: рядом с exe после сборки, рядом с кодом при обычном запуске
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "mic": "",
    "model_size": "small",
    "binds": {"talk": "key:f9", "toggle_overlay": "key:f7", "move_mode": "key:f8"},
    "overlay": {
        "x": 100, "y": 100, "width": 520, "font_size": 20,
        "color_me": "#7CFC9A", "color_ally": "#8AB4FF", "color_info": "#E0E0E0",
        "bg_color": "#000000", "bg_opacity": 67,
        "show_seconds": 10, "max_lines": 5, "show_original": True,
    },
}


def _merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load():
    """Возвращает (настройки, первый_запуск)."""
    if not os.path.exists(SETTINGS_FILE):
        return _merge(DEFAULTS, {}), True
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f)), False
    except Exception as e:
        print(f"[ошибка в settings.json, беру настройки по умолчанию: {e}]")
        return _merge(DEFAULTS, {}), False


def save(cfg):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[не удалось сохранить настройки: {e}]")