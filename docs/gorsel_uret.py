"""README'deki ekran görüntülerini ve GIF'i uygulamanın gerçek pencerelerinden üretir.

İki gerçek SolidGit LAN penceresi açılır — Hüseyin (oturumu açan) ve Ahmet (dosyaları
olmayan arkadaş) — ve gerçek bir çalışma günü oynatılır: oturum açma, katılım kodu ve onay,
TLS üzerinden indirme, kilitleme, kaydedip gönderme, güncellemeyi alma, ve ayrı çalışılınca
çıkan birleştirme ekranı. Her görsel o pencerelerin kendisinden yakalanır; hiçbir şey elle
çizilmez. Numaralı genel bakıştaki çerçeveler bile widget'ların gerçek konumlarından
hesaplanır.

İki bilinçli kısayol:
  * Dosya içerikleri rastgele baytlardır (gerçek CAD dosyası değil). Uygulama dosyaların
    içini açmaz, yalnızca hash'ler ve taşır; adlar ve boyutlar gerçekçi tutulmuştur.
  * Ahmet'in penceresi oturumu UDP yayınından değil, doğrudan 127.0.0.1 kaydından görür.
    Böylece görüntüler her çalıştırmada aynı çıkar ve bu bilgisayarın yerel ağ adresleri
    README'ye girmez. Yayınla keşfin kendisi testlerde (tests/test_discovery.py) sınanır.

Pencereler ekranda açılmaz (Qt'nin offscreen platformu, Windows yazı tipleriyle).

Çalıştırma (proje kökünden):
    .venv\\Scripts\\python.exe docs\\gorsel_uret.py
"""

from __future__ import annotations

import io
import os
import random
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"

# `setdefault` is not enough: an empty QT_QPA_PLATFORM counts as set, and Qt then falls back
# to real on-screen windows. Offscreen keeps the run invisible and the output identical.
if not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
if not os.environ.get("QT_QPA_FONTDIR"):
    os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from solidgit_lan.ui import theme  # noqa: E402
from solidgit_lan.ui.main_window import NETWORK_PAGE, MainWindow  # noqa: E402
from solidgit_lan.ui.reconcile_dialog import ReconcileDialog  # noqa: E402

WINDOW = (1100, 690)
GIF_WIDTH = 960
SEED = 7

PROJECT_FILES = {
    "Sasi/sasi.sldasm": 3_200_000,
    "Sasi/govde.sldprt": 6_100_000,
    "Sasi/kapak.sldprt": 1_800_000,
    "Sasi/mil.sldprt": 900_000,
    "Sasi/rulman_yatagi.sldprt": 1_400_000,
    "Sasi/braket.sldprt": 700_000,
    "Sasi/Motor/motor.sldasm": 2_400_000,
    "Sasi/Motor/rotor.sldprt": 3_300_000,
    "Sasi/teknik_resim.slddrw": 2_600_000,
}

HUSEYIN = ("Hüseyin", "HUSEYIN-PC", "#5b8def")
AHMET = ("Ahmet", "AHMET-LAPTOP", "#a78bfa")

# Her karenin altındaki açıklama, iki dilde. GIF'ler aynı karelerden iki kez birleştirilir.
STEPS = {
    "acilis": (
        "Hüseyin'de montaj var; geçmişi ve dosyaları burada.",
        "Hüseyin has the assembly, with its history.",
    ),
    "bos": (
        "Ahmet'te hiçbir şey yok — dosyalar olmadan da katılabilir.",
        "Ahmet has nothing yet — he can join without the files.",
    ),
    "oturum": (
        "Hüseyin oturumu başlatır; kodu arkadaşlarına söyler.",
        "Hüseyin starts a session and reads out the join code.",
    ),
    "liste": (
        "Aynı ağdaki oturum Ahmet'in listesinde kendiliğinden görünür.",
        "The session appears in Ahmet's list on its own.",
    ),
    "istek": (
        "Doğru kod da yetmez: Hüseyin izin vermeden kimse giremez.",
        "A correct code is not enough: Hüseyin has to let him in.",
    ),
    "indirme": (
        "Ahmet projeyi indirir — her dosya hash'iyle doğrulanır.",
        "Ahmet downloads the project — every file verified by hash.",
    ),
    "indi": (
        "Dosyalar geldi. Ahmet gövdeyi kilitler.",
        "The files arrived. Ahmet locks the body part.",
    ),
    "kilit": (
        "Hüseyin'de anında görünür: gövdeyi artık kimse değiştiremez.",
        "It shows up for Hüseyin at once: nobody else can change it.",
    ),
    "geldi": (
        "Ahmet kaydedip gönderir; Hüseyin'in dosyaları altından değişmez.",
        "Ahmet saves and sends; Hüseyin's files never change under him.",
    ),
    "alindi": (
        "Hüseyin hazır olunca alır — iki bilgisayarda aynı dosyalar.",
        "Hüseyin takes it when ready — the same files on both machines.",
    ),
}
WHO = {
    "Hüseyin": ("HÜSEYİN'İN EKRANI · oturumu açan", "HÜSEYİN'S SCREEN · hosting"),
    "Ahmet": ("AHMET'İN EKRANI · katılan", "AHMET'S SCREEN · joining"),
}


# -- yardımcılar -----------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def pump(app: QApplication, windows, seconds: float = 0.25) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for window in windows:
            window.refresh()
        app.processEvents()
        time.sleep(0.03)


def wait(app: QApplication, windows, condition, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pump(app, windows, 0.05)
        if condition():
            return
    raise RuntimeError("Senaryo adımı zamanında tamamlanmadı.")


def to_image(widget) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    widget.grab().save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGB")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_window(app, name: str, device: str, appdata: Path) -> MainWindow:
    # Her pencere kendi cihaz kimliğini kendi ayar klasöründen okur.
    os.environ["SOLIDGIT_APPDATA"] = str(appdata)
    os.environ["SOLIDGIT_USER"] = name
    os.environ["SOLIDGIT_DEVICE"] = device
    window = MainWindow()
    window.setFixedSize(*WINDOW)
    window.show()
    pump(app, [window], 0.2)
    return window


def write_project(root: Path, rng: random.Random) -> None:
    for path, size in PROJECT_FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rng.randbytes(size))


def edit(path: Path, rng: random.Random) -> None:
    from solidgit_lan.core.objects import clear_readonly

    clear_readonly(path)
    path.write_bytes(rng.randbytes(int(path.stat().st_size * rng.uniform(0.97, 1.05))))


def widget_rect(widget, window) -> QRect:
    origin = widget.mapTo(window, QPoint(0, 0))
    return QRect(origin, widget.size())


def union(*rects: QRect) -> QRect:
    result = rects[0]
    for rect in rects[1:]:
        result = result.united(rect)
    return result


# -- senaryo ---------------------------------------------------------------------------------


def run(lab: Path) -> tuple[list[tuple[str, str, Image.Image]], dict[str, Image.Image], dict]:
    """Senaryoyu oynatır. GIF kareleri, tek tek ekran görüntüleri ve konum bilgisi döner."""
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)
    rng = random.Random(SEED)
    frames: list[tuple[str, str, Image.Image]] = []
    shots: dict[str, Image.Image] = {}
    marks: dict = {}

    project = lab / "huseyin" / "Sasi Montaji"
    write_project(project, rng)

    host = make_window(app, *HUSEYIN[:2], lab / "veri_huseyin")
    host.state.initialise_workspace(project, name="Şasi Montajı")
    host._after_open()
    host.state.commit("ilk montaj")
    edit(project / "Sasi" / "govde.sldprt", rng)
    host.state.commit("gövde et kalınlığı 3 mm'ye çıkarıldı")
    edit(project / "Sasi" / "kapak.sldprt", rng)
    host.state.commit("kapak cıvata delikleri M6")
    pump(app, [host])
    frames.append(("Hüseyin", "acilis", to_image(host)))

    ahmet = make_window(app, *AHMET[:2], lab / "veri_ahmet")
    ahmet.state.stop_listening()  # keşif aşağıda elle — bkz. modül açıklaması
    pump(app, [ahmet])
    frames.append(("Ahmet", "bos", to_image(ahmet)))
    shots["adim1-karsilama"] = frames[-1][2]

    # Hüseyin oturumu başlatır.
    host._go_to_page(NETWORK_PAGE)
    host.state.start_hosting(free_port())
    pump(app, [host])
    frames.append(("Hüseyin", "oturum", to_image(host)))
    shots["adim2-oturum"] = frames[-1][2]

    def show_session() -> None:
        ahmet.state.registry.observe(host.state.server.announcement(), "127.0.0.1", time.time())

    # Ahmet "Arkadaşının oturumuna katıl" der; oturum listesinde görünür.
    ahmet.join_button.click()
    show_session()
    pump(app, [ahmet])
    ahmet.network_page.sessions.topLevelItem(0).setSelected(True)
    pump(app, [ahmet])
    frames.append(("Ahmet", "liste", to_image(ahmet)))
    shots["adim3-katil"] = frames[-1][2]

    # Ahmet kodu girer; istek Hüseyin'de belirir.
    peer = ahmet.state.nearby_sessions()[0]
    ahmet.state.join(peer, host.state.server.join_code)
    wait(app, [host, ahmet], lambda: bool(host.state.pending_join_requests()))
    pump(app, [host])
    frames.append(("Hüseyin", "istek", to_image(host)))
    shots["ag"] = to_image(host)

    host.state.approve(host.state.pending_join_requests()[0].device_id)
    wait(app, [host, ahmet], lambda: ahmet.state.session is not None)

    # İndirme — ilerleme çubuğu ortadayken bir kare yakalanabilirse alınır.
    ahmet.state.start_download(lab / "ahmet" / "Sasi Montaji")
    caught = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        show_session()
        pump(app, [ahmet], 0.02)
        transfer = ahmet.state.transfer
        if transfer is not None and transfer.running and 15 <= transfer.percent <= 85:
            if not caught:
                frames.append(("Ahmet", "indirme", to_image(ahmet)))
                shots["adim4-indirme"] = frames[-1][2]
                caught = True
        if transfer is not None and not transfer.running:
            break
    assert ahmet.state.transfer and ahmet.state.transfer.ok, ahmet.state.transfer
    if not caught:
        print("  (indirme o kadar hızlıydı ki ortası yakalanamadı — kare atlandı)")

    # Ahmet'in penceresi dosyaları açar; gövdeyi kilitler.
    pump(app, [ahmet])
    assert ahmet.state.acquire(["Sasi/govde.sldprt"]).ok
    pump(app, [ahmet], 0.4)
    frames.append(("Ahmet", "indi", to_image(ahmet)))

    host._go_to_page(1)
    pump(app, [host])
    frames.append(("Hüseyin", "kilit", to_image(host)))
    shots["adim5-kilit"] = frames[-1][2]

    # Ahmet gövdeyi düzenler, kaydeder ve gönderir.
    edit(ahmet.state.require_repo().root / "Sasi" / "govde.sldprt", rng)
    ahmet.files_page.message.setPlainText("gövdeye montaj deliği eklendi")
    ahmet.files_page._commit()
    ahmet.state.start_upload()
    wait(app, [ahmet], lambda: ahmet.state.transfer and not ahmet.state.transfer.running)
    assert ahmet.state.transfer.ok, ahmet.state.transfer.message
    pump(app, [host], 0.4)
    frames.append(("Hüseyin", "geldi", to_image(host)))
    shots["adim6-al"] = frames[-1][2]

    host.files_page._take_updates()
    pump(app, [host], 0.4)
    frames.append(("Hüseyin", "alindi", to_image(host)))

    # --- Tek tek ekran görüntüleri -----------------------------------------------------
    # Dosyalar: Hüseyin bir parçayı kilitleyip değiştirmiş, Ahmet başka birini tutuyor.
    assert host.state.acquire(["Sasi/mil.sldprt"]).ok
    edit(project / "Sasi" / "mil.sldprt", rng)
    assert ahmet.state.acquire(["Sasi/kapak.sldprt"]).ok
    host.files_page.message.setPlainText("mil çapı 20 mm'ye düşürüldü")
    host.files_page.banner.clear()  # önceki adımın "1 dosya güncellendi" bildirimi
    host._go_to_page(1)
    pump(app, [host], 0.5)
    shots["dosyalar"] = to_image(host)
    page = host.files_page
    marks["genel"] = {
        1: union(*(widget_rect(host.nav.button(i), host) for i in (1, 2, 3))),
        2: widget_rect(page.hint, host),
        3: union(widget_rect(page.lock_button, host), widget_rect(page.folder_button, host)),
        4: widget_rect(page.tree, host),
        5: union(widget_rect(page.message, host), widget_rect(page.commit_button, host)),
        6: widget_rect(host.status_label, host),
    }

    # Geçmiş: Ahmet'in kaydı seçili, bir dosya seçili — "geri yükle" etkin.
    host._go_to_page(2)
    pump(app, [host], 0.3)
    history = host.history_page
    history.tree.setCurrentItem(history.tree.topLevelItem(0))
    pump(app, [host], 0.2)
    for index in range(history.files.topLevelItemCount()):
        item = history.files.topLevelItem(index)
        if item.text(0).endswith("govde.sldprt"):
            item.setSelected(True)
    pump(app, [host], 0.2)
    shots["gecmis"] = to_image(host)

    # Birleştirme: oturum dışında ayrı ayrı çalışmışlar, ikisi de gövdeye dokunmuş.
    host.state.release(["Sasi/mil.sldprt"])
    ahmet.state.release(["Sasi/kapak.sldprt"])
    host.state.require_repo().make_all_writable()
    shutil.copyfile(
        project / "Sasi" / "mil.sldprt", lab / "mil_yedek"
    )  # Hüseyin'in yarım işi kenara
    host.state.require_repo().checkout(host.state.require_repo().commits.head())
    edit(project / "Sasi" / "govde.sldprt", rng)
    edit(project / "Sasi" / "kapak.sldprt", rng)
    host.state.require_repo().commit(
        "gövde ve kapak revizyonu (evde)", _author(*HUSEYIN[:2])
    )
    ahmet_repo = ahmet.state.require_repo()
    ahmet_repo.make_all_writable()
    edit(ahmet_repo.root / "Sasi" / "govde.sldprt", rng)
    (ahmet_repo.root / "Sasi" / "destek_plakasi.sldprt").write_bytes(rng.randbytes(450_000))
    ahmet_repo.commit("gövde güçlendirme + destek plakası (evde)", _author(*AHMET[:2]))
    ahmet.state.start_upload()
    wait(app, [ahmet], lambda: ahmet.state.transfer and not ahmet.state.transfer.running)
    assert ahmet.state.divergence is not None, "ayrılık tespit edilmedi"

    dialog = ReconcileDialog(ahmet.state.divergence, their_name="Hüseyin")
    dialog.setStyleSheet(theme.STYLESHEET)
    dialog.setFixedSize(900, 540)
    dialog.show()
    pump(app, [], 0.3)
    shots["birlestirme"] = to_image(dialog)
    dialog.close()

    for window in (ahmet, host):
        window.state.shutdown()
        window.close()
    return frames, shots, marks


def _author(name: str, device: str):
    from solidgit_lan.core.commits import Author

    return Author(name=name, device=device)


# -- çıktı -------------------------------------------------------------------------------------


def overview(image: Image.Image, rects: dict[int, QRect]) -> Image.Image:
    """Gerçek ekran görüntüsünün üstüne, widget'ların gerçek konumlarında numaralar."""
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    orange = (255, 138, 61)
    badge_font = font(17, bold=True)
    for number, rect in rects.items():
        pad = 4
        box = (rect.left() - pad, rect.top() - pad, rect.right() + pad, rect.bottom() + pad)
        draw.rounded_rectangle(box, radius=8, outline=orange, width=3)
        cx, cy = box[0] + 2, box[1] + 2
        draw.ellipse((cx - 15, cy - 15, cx + 15, cy + 15), fill=orange)
        draw.text((cx, cy), str(number), font=badge_font, fill="white", anchor="mm")
    return canvas


def compose_gif(frames, target: Path, lang: int) -> None:
    """Pencereleri sırayla, kimin ekranı olduğu ve adımın açıklamasıyla birleştirir."""
    scale = GIF_WIDTH / WINDOW[0]
    body_h = int(WINDOW[1] * scale)
    top, bottom = 38, 48
    title_font, caption_font = font(15, bold=True), font(17)
    colours = {HUSEYIN[0]: HUSEYIN[2], AHMET[0]: AHMET[2]}

    images, durations = [], []
    for index, (who, step, shot) in enumerate(frames, start=1):
        canvas = Image.new("RGB", (GIF_WIDTH, top + body_h + bottom), theme.BACKGROUND)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, GIF_WIDTH, top), fill=colours[who])
        draw.text((14, top // 2), WHO[who][lang], font=title_font, fill="white", anchor="lm")
        draw.text(
            (GIF_WIDTH - 14, top // 2), f"{index}/{len(frames)}",
            font=title_font, fill="white", anchor="rm",
        )
        canvas.paste(shot.resize((GIF_WIDTH, body_h), Image.LANCZOS), (0, top))
        draw.text(
            (GIF_WIDTH // 2, top + body_h + bottom // 2), STEPS[step][lang],
            font=caption_font, fill=theme.TEXT, anchor="mm",
        )
        images.append(canvas.convert("P", palette=Image.ADAPTIVE, colors=128))
        durations.append(3600 if index == len(frames) else 2400)

    images[0].save(
        target, save_all=True, append_images=images[1:], duration=durations, loop=0,
        optimize=True,
    )


def main() -> None:
    lab = Path(tempfile.mkdtemp(prefix="solidgit-gorsel-"))
    try:
        frames, shots, marks = run(lab)
    finally:
        shutil.rmtree(lab, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)
    for name, image in shots.items():
        image.save(OUT / f"{name}.png", optimize=True)
    overview(shots["dosyalar"], marks["genel"]).save(OUT / "genel-bakis.png", optimize=True)
    compose_gif(frames, OUT / "demo.gif", lang=0)
    compose_gif(frames, OUT / "demo.en.gif", lang=1)

    for path in sorted(OUT.iterdir()):
        print(f"  {path.name:<22} {path.stat().st_size / 1024:7.0f} KB")


if __name__ == "__main__":
    main()
