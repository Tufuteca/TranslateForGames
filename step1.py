import time
import threading
import numpy as np
import sounddevice as sd
from pynput import keyboard
from faster_whisper import WhisperModel
import argostranslate.package
import argostranslate.translate
import json
import os
import re


SAMPLE_RATE = 16000
HOTKEY = keyboard.Key.f9  # зажми F9 и говори
MODEL_SIZE = "small"  # потом поменяем на "medium"
GLOSSARY_FILE = "glossary.json"
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


print("Загружаю Whisper (при первом запуске скачается модель)...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=4)
ensure_translation()


def get_translator(from_code="ru", to_code="en"):
    langs = {l.code: l for l in argostranslate.translate.get_installed_languages()}
    return langs[from_code].get_translation(langs[to_code])


print("Готовлю переводчик...")
t_start = time.time()
translator = get_translator()
translator.translate("Привет")  # прогрев, чтобы первая настоящая фраза не тормозила
print(f"Переводчик готов за {time.time() - t_start:.1f} с")


chunks = []
recording = False


def audio_callback(indata, frame_count, time_info, status):
    if recording:
        chunks.append(indata.copy())


def process(audio):
    load_glossary()
    t0 = time.time()
    segments, _ = model.transcribe(audio, language="ru", vad_filter=True, beam_size=1, initial_prompt=build_prompt())
    text = " ".join(s.text.strip() for s in segments).strip()
    if not text:
        print("(ничего не распознано)")
        return
    t1 = time.time()
    fixed = apply_glossary(text)
    translated = translator.translate(fixed)
    t2 = time.time()
    print(f"RU: {text}")
    if fixed != text:
        print(f"RU (после словаря): {fixed}")
    print(f"EN: {translated}")
    print(f"[распознавание {t1 - t0:.1f} с, перевод {t2 - t1:.1f} с]\n")


def on_press(key):
    global recording
    if key == HOTKEY and not recording:
        chunks.clear()
        recording = True
        print("Запись...")


def on_release(key):
    global recording
    if key == HOTKEY and recording:
        recording = False
        if not chunks:
            return
        audio = np.concatenate(chunks).flatten().astype(np.float32)
        if len(audio) < SAMPLE_RATE * 0.3:
            print("(слишком коротко)")
            return
        threading.Thread(target=process, args=(audio,), daemon=True).start()


print("Готово. Зажми F9, скажи фразу по-русски, отпусти. Выход: Ctrl+C\n")
with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=audio_callback):
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass