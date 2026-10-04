"""README'deki grafiği üretir — verisi SolidGit LAN'ın kendisinden gelir.

Ne ölçülür:
  1. Disk: her kayıttan sonra deponun diskte kapladığı yer, ve aynı geçmişi "klasörü
     her seferinde kopyala" (Montaj_v1, Montaj_v2, …) yöntemiyle tutmanın maliyeti.
  2. Ağ: gerçek bir oturum açılır, bir istemci gerçek TLS bağlantısıyla katılıp her
     kayıttan sonra günceller; her güncellemede ağdan geçen bayt ölçülür.

Hiçbir sayı elle yazılmaz. Dosya içerikleri sabit tohumlu rastgele baytlardır — yani
sıkıştırılamaz. Bu bilinçli bir seçim: grafik yalnızca "değişmeyen parça bir kez saklanır,
bir kez gönderilir" etkisini gösterir. Gerçek CAD dosyaları kısmen sıkıştırılabildiği için
gerçek kazanç bundan daha büyüktür, küçük değil.

Çalıştırma (proje kökünden):
    .venv\\Scripts\\python.exe docs\\grafik_uret.py
"""

from __future__ import annotations

import csv
import random
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from solidgit_lan.core.commits import Author  # noqa: E402
from solidgit_lan.core.repo import Repository  # noqa: E402
from solidgit_lan.net import sync  # noqa: E402
from solidgit_lan.net.client import PeerClient  # noqa: E402
from solidgit_lan.net.protocol import JoinStatus  # noqa: E402
from solidgit_lan.net.server import CoordinatorServer  # noqa: E402

OUT = ROOT / "docs" / "images"
SEED = 2026
COMMITS = 20

#: Küçük bir öğrenci montajına benzeyen boyutlar (bayt). Toplam ≈ 30 MB.
PARTS = {
    "Sasi/sasi.sldasm": 3_200_000,
    "Sasi/govde.sldprt": 6_100_000,
    "Sasi/kapak.sldprt": 1_800_000,
    "Sasi/mil.sldprt": 900_000,
    "Sasi/rulman_yatagi.sldprt": 1_400_000,
    "Sasi/braket.sldprt": 700_000,
    "Sasi/flans.sldprt": 1_100_000,
    "Sasi/Motor/motor.sldasm": 2_400_000,
    "Sasi/Motor/rotor.sldprt": 3_300_000,
    "Sasi/Motor/stator.sldprt": 4_200_000,
    "Sasi/teknik_resim.slddrw": 2_600_000,
    "Sasi/montaj_resmi.slddrw": 2_900_000,
}

ACCENT = "#3d6fd9"
GREY = "#9aa0ab"
INK = "#2b2f38"

#: Aynı ölçüm, iki dilde çizilir — README.md ve README.en.md için.
LABELS = {
    "tr": {
        "copies": "Klasörü her kayıtta kopyalamak",
        "store": "SolidGit deposu",
        "disk_title": "Diskte kaplanan yer",
        "commits": "Kayıt sayısı",
        "net_title": "Her güncellemede ağdan geçen veri",
        "net_x": "Kayıt (1 = ilk indirme)",
        "whole": "Her seferinde bütün klasörü göndermek: {mb:.0f} MB",
    },
    "en": {
        "copies": "Copying the folder at every save",
        "store": "SolidGit store",
        "disk_title": "Disk space used",
        "commits": "Number of commits",
        "net_title": "Data sent over the network per update",
        "net_x": "Commit (1 = first download)",
        "whole": "Sending the whole folder every time: {mb:.0f} MB",
    },
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def megabytes(n: int) -> float:
    return n / 1024 / 1024


def measure(lab: Path) -> list[dict]:
    rng = random.Random(SEED)
    author = Author(name="Demo", device="DEMO-PC")

    host_root = lab / "host" / "Sasi"
    for path, size in PARTS.items():
        target = host_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rng.randbytes(size))

    host = Repository.initialise(host_root, name="Sasi")
    host.commit("ilk montaj", author)

    server = CoordinatorServer(
        repo=host,
        user_name="Demo",
        device_name="DEMO-PC",
        device_id="host",
        port=free_port(),
        auto_approve=True,
        devices_path=lab / "devices.json",
    )
    server.start(host="127.0.0.1", discovery=False)

    client = PeerClient(device_id="ekip", device_name="EKIP-PC", user_name="Ekip")
    rows: list[dict] = []
    try:
        with client.open("127.0.0.1", server.port) as connection:
            joined = client.join(connection, server.join_code, host.info.repo_id)
            assert joined.status is JoinStatus.APPROVED
            token = joined.device_token

            peer, result = sync.clone(
                lab / "ekip" / "Sasi", host.info.repo_id, "Sasi", client, connection, token
            )
            assert result.ok, result.message

            folder_copies = 0
            for number in range(1, COMMITS + 1):
                if number > 1:
                    # Bir (bazen iki) parça değişir — gerçek bir çalışma gününe benzer biçimde.
                    changed = rng.sample(sorted(PARTS), 2 if number % 5 == 0 else 1)
                    for path in changed:
                        size = int(PARTS[path] * rng.uniform(0.95, 1.08))
                        (host_root / path).write_bytes(rng.randbytes(size))
                    host.commit(f"kayıt {number}", author)

                    plan = sync.plan_pull(peer, client, connection, token)
                    result = sync.pull(peer, plan, client, connection, token)
                    assert result.ok, result.message

                project = host.head_manifest().total_size()
                folder_copies += project
                rows.append(
                    {
                        "kayit": number,
                        "proje_mb": round(megabytes(project), 2),
                        "klasor_kopyalari_mb": round(megabytes(folder_copies), 2),
                        "solidgit_depo_mb": round(megabytes(host.objects.disk_usage()), 2),
                        "agdan_gecen_mb": round(megabytes(result.bytes_transferred), 2),
                    }
                )

            # Gerçekten aynı mı? Grafik ancak aktarım doğruysa bir şey ifade eder.
            for path in host.head_manifest().entries:
                assert (peer.root / path).read_bytes() == (host.root / path).read_bytes()
    finally:
        server.stop()
    return rows


def draw(rows: list[dict], target: Path, lang: str = "tr") -> None:
    text = LABELS[lang]
    commits = [r["kayit"] for r in rows]
    project = rows[-1]["proje_mb"]

    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.edgecolor": "#d5d8de",
            "axes.labelcolor": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig, (disk, net) = plt.subplots(1, 2, figsize=(11, 4.2), dpi=150)

    # Farklı soruları aynı eksende üst üste çizmemek için iki ayrı grafik.
    disk.plot(
        commits, [r["klasor_kopyalari_mb"] for r in rows],
        color=GREY, linewidth=2.2, label=text["copies"],
    )
    disk.plot(
        commits, [r["solidgit_depo_mb"] for r in rows],
        color=ACCENT, linewidth=2.6, label=text["store"],
    )
    disk.fill_between(
        commits,
        [r["solidgit_depo_mb"] for r in rows],
        [r["klasor_kopyalari_mb"] for r in rows],
        color=GREY, alpha=0.12,
    )
    disk.set_title(text["disk_title"], loc="left", fontweight="bold", color=INK)
    disk.set_xlabel(text["commits"])
    disk.set_ylabel("MB")
    disk.set_xlim(1, commits[-1])
    disk.set_ylim(0)
    disk.legend(frameon=False, loc="upper left")
    disk.annotate(
        f"{rows[-1]['klasor_kopyalari_mb']:.0f} MB",
        (commits[-1], rows[-1]["klasor_kopyalari_mb"]),
        textcoords="offset points", xytext=(-4, 6), ha="right", color=GREY,
    )
    disk.annotate(
        f"{rows[-1]['solidgit_depo_mb']:.0f} MB",
        (commits[-1], rows[-1]["solidgit_depo_mb"]),
        textcoords="offset points", xytext=(-4, 8), ha="right", color=ACCENT,
        fontweight="bold",
    )

    sent = [r["agdan_gecen_mb"] for r in rows]
    colours = [GREY if i == 0 else ACCENT for i in range(len(rows))]
    net.bar(commits, sent, color=colours, width=0.7)
    net.axhline(project, color=INK, linestyle="--", linewidth=1.1)
    net.text(
        (commits[-1] + 2) / 2, project * 1.035,
        text["whole"].format(mb=project),
        va="bottom", ha="center", fontsize=8.5, color=INK,
    )
    net.set_title(text["net_title"], loc="left", fontweight="bold", color=INK)
    net.set_xlabel(text["net_x"])
    net.set_ylabel("MB")
    net.set_xlim(0.3, commits[-1] + 0.7)
    net.set_ylim(0, project * 1.18)

    # Kayıt sayısı tam sayıdır; "2.5. kayıt" diye bir şey yok.
    ticks = [1, 5, 10, 15, 20]
    disk.set_xticks(ticks)
    net.set_xticks(ticks)

    fig.tight_layout(w_pad=3)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, facecolor="white")
    plt.close(fig)


def main() -> None:
    lab = Path(tempfile.mkdtemp(prefix="solidgit-grafik-"))
    try:
        rows = measure(lab)
    finally:
        shutil.rmtree(lab, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "grafik_veri.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    draw(rows, OUT / "grafik.png", "tr")
    draw(rows, OUT / "grafik.en.png", "en")

    last = rows[-1]
    updates = [r["agdan_gecen_mb"] for r in rows[1:]]
    print(f"proje boyutu          : {last['proje_mb']:.1f} MB")
    print(f"{len(rows)} kayıt, klasör kopyaları: {last['klasor_kopyalari_mb']:.1f} MB")
    print(f"{len(rows)} kayıt, SolidGit deposu : {last['solidgit_depo_mb']:.1f} MB")
    print(f"ilk indirme           : {rows[0]['agdan_gecen_mb']:.1f} MB")
    print(f"güncelleme ortalaması : {sum(updates) / len(updates):.1f} MB")
    print(f"yazıldı               : {OUT / 'grafik.png'}")


if __name__ == "__main__":
    main()
