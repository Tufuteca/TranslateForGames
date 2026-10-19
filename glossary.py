import json
import os
import re

GLOSSARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.json")
DEFAULT_GLOSSARY = {
    "hints": ["хот зона", "пушить", "прикрой", "респаун", "эвак", "хелс"],
    "replace": {
        "ход зон": "hot zone",
        "хот зон": "hot zone",
    },
}

_glossary = {"hints": [], "replace": {}}
_glossary_mtime = 0


def load_glossary():
    """Читает glossary.json; перечитывает сам, если файл изменили."""
    global _glossary, _glossary_mtime
    if not os.path.exists(GLOSSARY_FILE):
        with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_GLOSSARY, f, ensure_ascii=False, indent=2)
    mtime = os.path.getmtime(GLOSSARY_FILE)
    if mtime == _glossary_mtime:
        return
    _glossary_mtime = mtime
    try:
        with open(GLOSSARY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _glossary = {"hints": data.get("hints", []), "replace": data.get("replace", {})}
        print(f"[словарь загружен: {len(_glossary['hints'])} подсказок, {len(_glossary['replace'])} замен]")
    except Exception as e:
        print(f"[ошибка в glossary.json, оставляю прежний словарь: {e}]")


def build_prompt():
    words = ", ".join(_glossary["hints"])
    return ("Тактический шутер. " + words) if words else None


def apply_glossary(text):
    for heard in sorted(_glossary["replace"], key=len, reverse=True):
        correct = _glossary["replace"][heard]
        pattern = r"(?<!\w)" + re.escape(heard) + r"\w*"
        text = re.sub(pattern, lambda m, c=correct: c, text, flags=re.IGNORECASE)
    return text