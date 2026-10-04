"""Building a version teammates can run, from inside the app itself.

The people this is for do not have Python, do not have git, and will not run a build command.
So the person who *does* have the source builds a package here and hands over one file.

Everything runs as a subprocess with its output streamed line by line, because a build takes
minutes and a progress-free wait is indistinguishable from a hang.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from . import __version__
from .appdata import app_root

APP_NAME = "SolidGitLAN"
OUTPUT_DIR_NAME = "dagitim"

#: Modules PyInstaller cannot see by following imports. uvicorn and zeroconf both resolve
#: large parts of themselves at run time, so they have to be collected wholesale.
COLLECT_ALL = ("zeroconf",)
COLLECT_SUBMODULES = ("uvicorn", "fastapi", "solidgit_lan")

#: Qt ships far more than this app uses; excluding the big unused pieces roughly halves the
#: download a teammate has to accept.
EXCLUDED_MODULES = (
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtMultimedia",
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "tkinter",
    "matplotlib",
    "pandas",
)


class BuildError(Exception):
    """Raised when a build cannot even be started."""


@dataclass
class BuildResult:
    ok: bool
    package: Path | None = None
    size_bytes: int = 0
    seconds: float = 0.0
    message: str = ""
    log_tail: list[str] = field(default_factory=list)


def output_dir() -> Path:
    return app_root() / OUTPUT_DIR_NAME


def can_build() -> tuple[bool, str]:
    """Whether a build is possible from here, and why not if it is not."""
    if getattr(sys, "frozen", False):
        return False, (
            "Bu zaten derlenmiş bir sürüm. Yeni sürüm oluşturmak için uygulamayı kaynak "
            "koddan (SolidGitLAN.bat ile) çalıştır."
        )
    if not (app_root() / "launch.py").exists():
        return False, f"Kaynak kod bulunamadı ({app_root()})."
    return True, ""


def pyinstaller_installed() -> bool:
    try:
        import PyInstaller  # noqa: F401

        return True
    except ImportError:
        return False


def _stream(command: list[str], cwd: Path, on_line: Callable[[str], None],
            cancel: threading.Event) -> int:
    """Run a command, forwarding each output line as it appears."""
    on_line("> " + " ".join(command[1:3]) + " …")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    for line in process.stdout:
        if cancel.is_set():
            process.terminate()
            on_line("İptal edildi.")
            return -1
        stripped = line.rstrip()
        if stripped:
            on_line(stripped)
    return process.wait()


def build(
    on_line: Callable[[str], None],
    run_tests: bool = True,
    one_file: bool = False,
    cancel: threading.Event | None = None,
) -> BuildResult:
    """Produce a package in `dagitim/`. Returns where it landed."""
    started = time.perf_counter()
    cancel = cancel or threading.Event()
    log: list[str] = []

    def emit(line: str) -> None:
        log.append(line)
        del log[:-400]
        try:
            on_line(line)
        except Exception:  # noqa: BLE001 - reporting progress must never fail the build
            pass

    possible, reason = can_build()
    if not possible:
        return BuildResult(ok=False, message=reason)

    root = app_root()
    target = output_dir()
    work = target / ".work"
    staging = target / "build"

    if run_tests:
        emit("Testler çalıştırılıyor…")
        env_platform = os.environ.get("QT_QPA_PLATFORM")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        try:
            code = _stream([sys.executable, "-m", "pytest", "-q"], root, emit, cancel)
        finally:
            if env_platform is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = env_platform
        if cancel.is_set():
            return BuildResult(ok=False, message="İptal edildi.", log_tail=log[-40:])
        if code != 0:
            # Shipping a build whose tests fail is how a teammate ends up debugging your bug.
            return BuildResult(
                ok=False,
                message="Testler geçmedi — sürüm oluşturulmadı. Önce hatayı düzelt.",
                log_tail=log[-40:],
            )
        emit("Testler geçti.")

    if not pyinstaller_installed():
        emit("PyInstaller kurulu değil, kuruluyor…")
        code = _stream(
            [sys.executable, "-m", "pip", "install", "pyinstaller"], root, emit, cancel
        )
        if code != 0:
            return BuildResult(
                ok=False, message="PyInstaller kurulamadı.", log_tail=log[-40:]
            )

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(staging),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
        "--paths",
        str(root),
    ]
    command += ["--onefile"] if one_file else ["--onedir"]
    for module in COLLECT_ALL:
        command += ["--collect-all", module]
    for module in COLLECT_SUBMODULES:
        command += ["--collect-submodules", module]
    for module in EXCLUDED_MODULES:
        command += ["--exclude-module", module]
    command.append(str(root / "launch.py"))

    emit(f"Sürüm oluşturuluyor ({'tek dosya' if one_file else 'klasör'})…")
    emit("Bu birkaç dakika sürebilir.")
    code = _stream(command, root, emit, cancel)
    if cancel.is_set():
        return BuildResult(ok=False, message="İptal edildi.", log_tail=log[-40:])
    if code != 0:
        return BuildResult(
            ok=False,
            message="Derleme başarısız oldu. Günlüğün sonuna bak.",
            log_tail=log[-40:],
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    package = target / f"{APP_NAME}-{__version__}-{stamp}.zip"
    payload = staging / (f"{APP_NAME}.exe" if one_file else APP_NAME)
    if not payload.exists():
        return BuildResult(
            ok=False, message=f"Beklenen çıktı bulunamadı: {payload}", log_tail=log[-40:]
        )

    emit("Paket sıkıştırılıyor…")
    readme = _write_readme(target, one_file)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(readme, "OKU-BENI.txt")
        if one_file:
            archive.write(payload, f"{APP_NAME}.exe")
        else:
            for item in sorted(payload.rglob("*")):
                if item.is_file():
                    archive.write(item, str(Path(APP_NAME) / item.relative_to(payload)))
    readme.unlink(missing_ok=True)

    size = package.stat().st_size
    emit(f"Bitti: {package.name} ({size / 1024 / 1024:.0f} MB)")
    return BuildResult(
        ok=True,
        package=package,
        size_bytes=size,
        seconds=time.perf_counter() - started,
        message="Sürüm hazır.",
        log_tail=log[-40:],
    )


def _write_readme(directory: Path, one_file: bool) -> Path:
    """A note for the person receiving the package, not for us.

    They will meet a SmartScreen warning on an unsigned executable, and without a sentence
    explaining it, half of them will decide the file is a virus and delete it.
    """
    how_to_run = (
        f"1) Zip'i bir klasöre çıkar.\n"
        f"2) {APP_NAME}.exe dosyasına çift tıkla.\n"
    ) if one_file else (
        f"1) Zip'i bir klasöre çıkar.\n"
        f"2) {APP_NAME} klasörünün içindeki {APP_NAME}.exe dosyasına çift tıkla.\n"
        f"   (Klasörün tamamı gerekli — sadece exe'yi taşıma.)\n"
    )

    text = f"""SolidGit LAN {__version__}
Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M')}

NASIL ÇALIŞTIRILIR
{how_to_run}
"WINDOWS BU UYGULAMAYI ENGELLEDİ" UYARISI
Program imzalı olmadığı için Windows SmartScreen uyarı verir. Bu bir virüs
uyarısı değil, "bu dosyayı daha önce görmedim" uyarısıdır.
  Ek bilgi  ->  Yine de çalıştır

İLK KULLANIM
1) "Klasör seç" ile montaj klasörünü aç.
2) Alta kısa bir isim yaz, "Değişiklikleri kaydet"e bas.
3) Üzerinde çalışacağın dosyayı seçip "Kilitle"ye bas.
4) Birlikte çalışmak için: Ağ sekmesi -> Oturumu başlat, çıkan kodu paylaş.
   Karşı taraf aynı ağa bağlanıp listeden oturumu seçer ve kodu girer.

ÖNEMLİ
- Herkeste AYNI sürüm olmalı. Farklı sürümler birbirine bağlanmayı reddeder.
- Ayarların ve kayıtların, exe'nin yanındaki "veri" klasöründe tutulur.
- Sorun olursa veri/sessions klasöründeki son dosyayı paylaş.
"""
    # utf-8-sig, not plain utf-8: this file is opened in Notepad by someone who did not build
    # it, and without the byte-order mark Windows guesses the legacy code page and renders
    # every Turkish character as mojibake. A garbled instruction sheet is worse than none.
    path = directory / "OKU-BENI.txt"
    path.write_text(text, encoding="utf-8-sig")
    return path
