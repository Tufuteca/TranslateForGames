import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget



from version import REPO, __version__

USER_AGENT = f"GameTranslator/{__version__}"
TIMEOUT = 10
ALLOWED_HOSTS = ("github.com", ".githubusercontent.com")


def parse_version(tag: str) -> tuple[int, ...]:
    """Преобразует строку тега (например, 'v0.1.2-beta') в кортеж чисел (0, 1, 2)."""
    clean = tag.lstrip("vV").strip()
    parts = []
    for part in clean.split("."):
        m = re.match(r"^(\d+)", part)
        if m:
            parts.append(int(m.group(1)))
        else:
            break
    return tuple(parts) if parts else (0,)


def is_newer_version(remote_tag: str, current_ver: str) -> bool:
    """Сравнивает версии численно с учётом разной длины кортежей (0.1 == 0.1.0)."""
    r_parts = list(parse_version(remote_tag))
    c_parts = list(parse_version(current_ver))
    max_len = max(len(r_parts), len(c_parts))
    r_parts.extend([0] * (max_len - len(r_parts)))
    c_parts.extend([0] * (max_len - len(c_parts)))
    return tuple(r_parts) > tuple(c_parts)


def is_allowed_url(url: str) -> bool:
    """Проверяет, что ссылка использует HTTPS и ведёт на разрешённый хост GitHub."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        return host == "github.com" or host.endswith(".githubusercontent.com")
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Разрешает редиректы только на доверенные HTTPS-хосты GitHub."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_url(newurl):
            raise urllib.error.URLError(f"Запрещённый адрес перенаправления: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_safe_url(url: str, validate_host: bool = True):
    """Открывает URL с проверкой HTTPS-хоста и безопасными перенаправлениями."""
    if validate_host and not is_allowed_url(url):
        raise ValueError(f"Недопустимый URL для скачивания: {url}")

    # Сохраняем системные настройки прокси, если они нужны для доступа к GitHub.
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return opener.open(req, timeout=TIMEOUT)


def fetch_latest_release() -> dict | None:
    """Получает самый новый не-draft релиз из списка релизов репозитория."""
    # Небольшой ответ быстрее загружается и достаточен для обычной истории релизов.
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                releases = json.loads(resp.read().decode("utf-8"))
            break
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 1:
                raise
            print(f"[Updater] Первая попытка проверки не удалась, повторяю: {exc}")
    else:
        raise last_error

    if not isinstance(releases, list):
        return None

    # Фильтруем черновики (draft), пре-релизы разрешены
    valid_releases = [r for r in releases if not r.get("draft", False)]
    if not valid_releases:
        return None

    # Выбираем релиз с максимальной версией по тегу
    valid_releases.sort(
        key=lambda r: parse_version(r.get("tag_name", "")),
        reverse=True,
    )
    return valid_releases[0]


def find_release_assets(release: dict) -> tuple[str | None, str | None]:
    """Ищет ссылки на архив GameTranslator-vX.Y.Z.zip и его .sha256 файл."""
    tag = release.get("tag_name", "").lstrip("vV")
    expected_zip = f"GameTranslator-v{tag}.zip"
    expected_sha = f"{expected_zip}.sha256"

    zip_url = None
    sha_url = None

    for asset in release.get("assets", []):
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if name == expected_zip:
            zip_url = url
        elif name == expected_sha:
            sha_url = url

    # Резервный поиск по шаблону, если имя тега и файла слегка отличаются
    if not zip_url or not sha_url:
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            url = asset.get("browser_download_url", "")
            if not zip_url and re.match(r"^GameTranslator-v[\d.]+\.zip$", name, re.I):
                zip_url = url
            elif not sha_url and re.match(r"^GameTranslator-v[\d.]+\.zip\.sha256$", name, re.I):
                sha_url = url

    return zip_url, sha_url


class _CheckWorker(QThread):
    """Фоновый поток для проверки наличия новой версии."""
    finished = Signal(object, str)  # (release_info_dict | None, error_message)

    def run(self):
        try:
            release = fetch_latest_release()
            if not release:
                self.finished.emit(None, "")
                return

            tag = release.get("tag_name", "")
            if not is_newer_version(tag, __version__):
                self.finished.emit(None, "")
                return

            zip_url, sha_url = find_release_assets(release)
            if not zip_url or not sha_url:
                self.finished.emit(
                    None,
                    f"В релизе {tag} отсутствуют файлы обновления (.zip или .sha256).",
                )
                return

            info = {
                "version": tag.lstrip("vV"),
                "tag": tag,
                "body": (release.get("body") or "").strip(),
                "zip_url": zip_url,
                "sha_url": sha_url,
            }
            self.finished.emit(info, "")
        except Exception as e:
            self.finished.emit(None, str(e))


class _DownloadWorker(QThread):
    """Фоновый поток для скачивания архива, проверки хеша и распаковки."""
    progress = Signal(int)
    finished = Signal(str, str)  # (путь_к_папке_с_файлами, текст_ошибки)

    def __init__(self, zip_url: str, sha_url: str, version: str):
        super().__init__()
        self.zip_url = zip_url
        self.sha_url = sha_url
        self.version = version

    def run(self):
        temp_dir = None
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix=f"gt_update_{self.version}_"))
            zip_path = temp_dir / f"GameTranslator-v{self.version}.zip"

            # 1. Скачиваем контрольную сумму SHA256
            with _open_safe_url(self.sha_url) as resp:
                sha_text = resp.read().decode("utf-8", errors="replace").strip()
            expected_hash = sha_text.split()[0].lower()
            if not re.match(r"^[0-9a-f]{64}$", expected_hash):
                raise ValueError(f"Некорректный формат SHA256 в файле: {expected_hash}")

            # 2. Скачиваем архив с контролем размера и расчётом SHA256
            hasher = hashlib.sha256()
            with _open_safe_url(self.zip_url) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            self.progress.emit(int(downloaded * 100 / total))

            # 3. Сверяем хеш
            actual_hash = hasher.hexdigest().lower()
            if actual_hash != expected_hash:
                msg = f"Ошибка проверки SHA256! Ожидался: {expected_hash}, получен: {actual_hash}"
                print(f"[Updater] {msg}")
                try:
                    zip_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self.finished.emit("", "Контрольная сумма SHA256 не совпала. Установка прервана.")
                return

            # 4. Безопасная распаковка архива
            extract_dir = temp_dir / "extracted"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    target_path = (extract_dir / member.filename).resolve()
                    if not str(target_path).startswith(str(extract_dir.resolve())):
                        raise ValueError("Обнаружен небезопасный путь внутри архива")
                zf.extractall(extract_dir)

            # 5. Определяем корень обновления (файлы в корне или в одной вложенной папке)
            entries = [p for p in extract_dir.iterdir() if p.name != "__MACOSX"]
            if len(entries) == 1 and entries[0].is_dir():
                source_dir = entries[0]
            else:
                source_dir = extract_dir

            self.finished.emit(str(source_dir), "")
        except Exception as e:
            print(f"[Updater] Ошибка при подготовке обновления: {e}")
            self.finished.emit("", str(e))


def _launch_update_script(source_dir: str) -> None:
    """Создаёт временный .bat скрипт для замены файлов после закрытия программы и запускает его."""
    app_dir = Path(sys.executable).resolve().parent
    exe_name = Path(sys.executable).name
    pid = os.getpid()

    fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="gt_updater_")
    os.close(fd)

    # Скрипт ждёт завершения процесса по PID, копирует файлы через robocopy с исключением
    # пользовательских настроек, глоссария, логов и папки моделей, затем запускает программу
    bat_content = f"""@echo off
chcp 65001 >nul
setlocal

:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)

robocopy "{source_dir}" "{app_dir}" /E /XF "settings.json" "glossary.json" "translator.log" "translator.log.old" /XD "models" /R:5 /W:1 /NFL /NDL /NJH /NJS >NUL

start "" /D "{app_dir}" "{app_dir / exe_name}"

rmdir /S /Q "{Path(source_dir).parent}" 2>NUL
del "%~f0" & exit
"""
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=creation_flags,
        close_fds=True,
        cwd=tempfile.gettempdir(),
    )


class UpdateManager(QObject):
    """Управляет проверкой и установкой обновлений с поддержкой тихого и ручного режимов."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._parent_widget = parent
        self._check_worker: _CheckWorker | None = None
        self._download_worker: _DownloadWorker | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._silent = True

    def check_for_updates(self, silent: bool = True) -> None:
        """Запускает проверку обновлений в фоновом потоке."""
        if self._check_worker is not None and self._check_worker.isRunning():
            return
        if self._download_worker is not None and self._download_worker.isRunning():
            return

        self._silent = silent
        self._check_worker = _CheckWorker()
        self._check_worker.finished.connect(self._on_check_finished)
        self._check_worker.start()

    def _on_check_finished(self, info: dict | None, error: str) -> None:
        if error:
            print(f"[Updater] Ошибка при проверке обновлений: {error}")
            if not self._silent:
                QMessageBox.warning(
                    self._parent_widget,
                    "Ошибка обновления",
                    f"Не удалось проверить обновления:\n{error}",
                )
            return

        if not info:
            if not self._silent:
                QMessageBox.information(
                    self._parent_widget,
                    "Обновление",
                    f"У вас последняя версия ({__version__}).",
                )
            return

        version = info["version"]
        body = info["body"] or "Описание изменений отсутствует."

        # В режиме разработки (запуск из исходников) ничего не скачиваем
        if not getattr(sys, "frozen", False):
            print(f"[Updater] Доступна новая версия {version} (режим разработки).")
            QMessageBox.information(
                self._parent_widget,
                "Доступно обновление",
                f"Доступна новая версия {version}.\n\n"
                f"Программа запущена в режиме разработки (не из .exe), "
                f"автоматическое обновление отключено.\n\n"
                f"Описание релиза:\n{body}",
            )
            return

        msg_box = QMessageBox(self._parent_widget)
        msg_box.setWindowTitle("Доступно обновление")
        msg_box.setIcon(QMessageBox.Icon.Information)
        msg_box.setText(f"Доступна версия {version}. Обновить?")
        msg_box.setInformativeText(body[:1000] + ("..." if len(body) > 1000 else ""))
        btn_yes = msg_box.addButton("Да", QMessageBox.ButtonRole.YesRole)
        msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.exec()

        if msg_box.clickedButton() == btn_yes:
            self._start_download(info["zip_url"], info["sha_url"], version)

    def _start_download(self, zip_url: str, sha_url: str, version: str) -> None:
        self._progress_dialog = QProgressDialog(
            f"Скачивание версии {version}...", None, 0, 100, self._parent_widget
        )
        self._progress_dialog.setWindowTitle("Обновление GameTranslator")
        self._progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setValue(0)
        self._progress_dialog.show()

        self._download_worker = _DownloadWorker(zip_url, sha_url, version)
        self._download_worker.progress.connect(self._progress_dialog.setValue)
        self._download_worker.finished.connect(self._on_download_finished)
        self._download_worker.start()

    def _on_download_finished(self, source_dir: str, error: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None

        if error or not source_dir:
            print(f"[Updater] Установка обновления прервана: {error}")
            QMessageBox.critical(
                self._parent_widget,
                "Ошибка обновления",
                f"Не удалось установить обновление:\n{error}",
            )
            return

        try:
            print("[Updater] Запуск скрипта обновления и закрытие программы...")
            _launch_update_script(source_dir)
            app = QApplication.instance()
            if app:
                app.quit()
            else:
                sys.exit(0)
        except Exception as e:
            print(f"[Updater] Не удалось запустить скрипт обновления: {e}")
            QMessageBox.critical(
                self._parent_widget,
                "Ошибка обновления",
                f"Не удалось запустить установщик обновления:\n{e}",
            )


_manager: UpdateManager | None = None


def check_updates(silent: bool = True, parent=None) -> None:
    """Точка входа для вызова из app.py."""
    global _manager
    if _manager is None:
        _manager = UpdateManager(parent)
    _manager.check_for_updates(silent=silent)