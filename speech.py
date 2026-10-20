"""Микрофон, распознавание речи (Whisper) и перевод (Argos).

Модуль не знает про окна: результат отдаёт через функции emit_message и emit_recording.
"""
import os
import threading
import time

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import argostranslate.package
import argostranslate.translate

import glossary
import settings

SAMPLE_RATE = 16000


def list_mics():
    """Список входных устройств (индекс, имя) только для основного звукового API Windows."""
    result = []
    host = sd.default.hostapi
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == host:
            result.append((i, d["name"]))
    return result


def local_model_path(size):
    """Модель Whisper из папки models рядом с программой; если её нет, будет скачана из интернета."""
    path = os.path.join(settings.APP_DIR, "models", f"faster-whisper-{size}")
    return path if os.path.isdir(path) else size


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


class SpeechEngine:
    """Запись с микрофона -> Whisper -> словарь -> перевод.

    emit_message(kind, main, sub) - показать сообщение в оверлее
    emit_recording(bool)          - запись началась или закончилась
    """

    def __init__(self, cfg, emit_message, emit_recording):
        self.cfg = cfg
        self.emit_message = emit_message
        self.emit_recording = emit_recording
        self.model = None
        self.translator = None
        self.ready = False
        self.recording = False
        self.chunks = []
        self.stream = None

    # ----- микрофон -----
    def restart_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        device = None
        for i, name in list_mics():
            if name == self.cfg["mic"]:
                device = i
                break
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                callback=self._audio_callback, device=device,
            )
            self.stream.start()
        except Exception as e:
            print(f"Ошибка микрофона: {e}")
            self.emit_message("info", f"Ошибка микрофона: {e}", "")

    def _audio_callback(self, indata, frame_count, time_info, status):
        if self.recording:
            self.chunks.append(indata.copy())

    # ----- загрузка моделей (вызывать в отдельном потоке) -----
    def load_models(self):
        try:
            self.emit_message("info", "Загружаю модели, подожди...", "")
            print("Загружаю Whisper...")
            self.model = WhisperModel(
                local_model_path(self.cfg["model_size"]),
                device="cpu", compute_type="int8", cpu_threads=4,
            )
            ensure_translation()
            self.translator = get_translator()
            self.translator.translate("Привет")   # прогрев
            glossary.load_glossary()
            self.ready = True
            print("Готово.")
            self.emit_message("info", "Готово! Зажми кнопку записи и говори", "")
        except Exception as e:
            print(f"Ошибка загрузки: {e}")
            self.emit_message("info", f"Ошибка загрузки: {e}", "")

    # ----- запись по кнопке -----
    def start_recording(self):
        if not self.ready or self.recording:
            return
        self.chunks.clear()
        self.recording = True
        self.emit_recording(True)

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self.emit_recording(False)
        if not self.chunks:
            return
        audio = np.concatenate(self.chunks).flatten().astype(np.float32)
        if len(audio) < SAMPLE_RATE * 0.3:
            print("(слишком коротко)")
            return
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio):
        glossary.load_glossary()
        t0 = time.time()
        segments, _ = self.model.transcribe(
            audio, language="ru", vad_filter=True, beam_size=1,
            initial_prompt=glossary.build_prompt(),
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            print("(ничего не распознано)")
            return
        t1 = time.time()
        fixed = glossary.apply_glossary(text)
        translated = self.translator.translate(fixed)
        t2 = time.time()
        print(f"RU: {text}")
        if fixed != text:
            print(f"RU (после словаря): {fixed}")
        print(f"EN: {translated}")
        print(f"[распознавание {t1 - t0:.1f} с, перевод {t2 - t1:.1f} с]\n")
        self.emit_message("me", translated, fixed)

    def shutdown(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
