"""Main window: header, sidebar, and the page stack.

A single timer refreshes whichever page is visible. Hosting runs on its own thread inside the
coordinator, so the window never blocks on the network — it just reads state that is already
there.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..appdata import forget_project, recent_projects
from ..core.repo import Repository, RepositoryError
from ..telemetry import SessionRecorder
from . import theme
from .pages import FilesPage, HistoryPage, NetworkPage
from .state import AppState
from .widgets import label

REFRESH_MILLISECONDS = 1000


PAGE_NAMES = {0: "başlangıç", 1: "dosyalar", 2: "geçmiş", 3: "ağ"}
FILES_PAGE = 1
NETWORK_PAGE = 3


class MainWindow(QMainWindow):
    def __init__(
        self, workspace: str | Path | None = None, recorder: SessionRecorder | None = None
    ) -> None:
        super().__init__()
        self.state = AppState()
        # A disabled recorder when none is supplied, so tests and the CLI never have to care
        # that recording exists.
        self.recorder = recorder or SessionRecorder(path=Path(), enabled=False)
        self.setWindowTitle("SolidGit LAN")
        self.resize(1180, 760)

        self.files_page = FilesPage(self.state, self.recorder)
        self.history_page = HistoryPage(self.state, self.recorder)
        self.network_page = NetworkPage(self.state, self.recorder)
        self.files_page.changed.connect(self.refresh)
        self.history_page.changed.connect(self.refresh)
        self.network_page.changed.connect(self.refresh)

        self.pages = QStackedWidget()
        self.welcome = self._build_welcome()
        for page in (self.welcome, self.files_page, self.history_page, self.network_page):
            self.pages.addWidget(page)

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_header())
        right.addWidget(self.pages, 1)
        container = QWidget()
        container.setLayout(right)
        layout.addWidget(container, 1)
        self.setCentralWidget(root)

        self._was_open = False
        self._recent_signature: tuple | None = None

        # Listen from the moment the window exists. Someone joining a teammate's session
        # does not have the project yet — that is the whole reason they are joining.
        self.state.start_listening()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MILLISECONDS)

        if workspace is not None:
            self._open_path(Path(workspace))
        self.refresh()

    # -- chrome ---------------------------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 18, 12, 18)
        layout.setSpacing(6)

        brand = label("SolidGit LAN", "title")
        brand.setContentsMargins(6, 0, 0, 4)
        layout.addWidget(brand)
        tagline = label("yerel ağ · sürüm ve kilit", "subtitle")
        tagline.setContentsMargins(6, 0, 0, 16)
        layout.addWidget(tagline)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for index, text in enumerate(("Dosyalar", "Geçmiş", "Ağ"), start=1):
            button = QPushButton(text)
            button.setCheckable(True)
            # Files and history need a project; the network page is how you get one.
            button.setEnabled(index == NETWORK_PAGE)
            self.nav.addButton(button, index)
            layout.addWidget(button)
        self.nav.idClicked.connect(self._go_to_page)
        self.nav.button(1).setChecked(True)

        layout.addStretch(1)

        open_button = QPushButton("Başka klasör seç…")
        open_button.clicked.connect(self.choose_folder)
        help_button = QPushButton("Nasıl çalışır?")
        help_button.clicked.connect(self.show_help)
        record_button = QPushButton("Kullanım kaydı")
        record_button.clicked.connect(self.show_recording)
        build_button = QPushButton("Sürüm oluştur…")
        build_button.setToolTip(
            "Arkadaşlarına verebileceğin tek bir zip dosyası üretir.\n"
            "Karşı tarafın hiçbir şey kurmasına gerek kalmaz."
        )
        build_button.clicked.connect(self.show_build)
        layout.addWidget(open_button)
        layout.addWidget(help_button)
        layout.addWidget(record_button)
        layout.addWidget(build_button)
        return sidebar

    def show_build(self) -> None:
        from .build_dialog import BuildDialog

        BuildDialog(self, recorder=self.recorder).exec()

    def current_page_name(self) -> str:
        return PAGE_NAMES.get(self.pages.currentIndex(), "?")

    def _go_to_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        # Pages are also changed from code (the welcome screen's "join" button), and then
        # nothing clicks the sidebar — so the highlight has to be moved here explicitly.
        button = self.nav.button(index)
        if button is not None and button.isEnabled():
            button.setChecked(True)
        self.recorder.event("page_change", page=PAGE_NAMES.get(index, "?"))
        self.refresh()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(66)

        self.project_label = label("Proje açık değil", "title")
        self.project_label.setStyleSheet("font-size: 16px;")
        self.head_label = label("", "subtitle")
        self.status_label = label("", "subtitle")

        # Ignored width policy: the label's width comes from the layout, not from its text.
        # Otherwise eliding to fit shrinks the label, which shrinks the text, and so on.
        self.head_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.project_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(1)
        left.addWidget(self.project_label)
        left.addWidget(self.head_label)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.addLayout(left, 1)
        layout.addWidget(self.status_label, 0)
        return header

    def _build_welcome(self) -> QWidget:
        """One obvious action, plus whatever was opened before.

        The old screen offered "open existing" and "start new" side by side. A first-time
        user has no existing project, so the wrong choice was an error message — and after
        the first session, the right choice was a file dialog they had to navigate every
        time. One button that works it out, and a list of what came before, removes both.
        """
        page = QWidget()
        # Stretch above and below, none between: otherwise the slack is shared out between
        # the labels and the screen reads as four unrelated things floating apart.
        frame = QVBoxLayout(page)
        frame.addStretch(1)
        outer = QVBoxLayout()
        outer.setSpacing(6)
        frame.addLayout(outer)
        frame.addStretch(1)

        title = label("Montaj klasörünü seç", "title")
        title.setAlignment(Qt.AlignCenter)
        explanation = QLabel(
            "Parçalar, montajlar ve teknik resimlerin hepsi tek bir klasörün içinde olmalı;\n"
            "klasör dışına referans verilmemeli. Klasör zaten bir projeyse açılır, değilse\n"
            "sana sorup projeye çeviririm — dosyaların değişmez."
        )
        explanation.setAlignment(Qt.AlignCenter)
        explanation.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        choose = QPushButton("Klasör seç…")
        choose.setProperty("kind", "primary")
        choose.setMinimumWidth(200)
        choose.clicked.connect(self.choose_folder)

        # The other way in: a teammate who has nothing yet and is here to receive. Without
        # this the only visible action assumed you already had the files.
        self.join_button = QPushButton("Arkadaşının oturumuna katıl")
        self.join_button.setMinimumWidth(200)
        self.join_button.setToolTip(
            "Dosyalar sende olmasa da olur — oturuma katılıp projeyi indirebilirsin."
        )
        self.join_button.clicked.connect(lambda: self._go_to_page(NETWORK_PAGE))

        buttons = QHBoxLayout()
        buttons.setAlignment(Qt.AlignCenter)
        buttons.setSpacing(10)
        buttons.addWidget(choose)
        buttons.addWidget(self.join_button)

        outer.addWidget(title)
        outer.addSpacing(4)
        outer.addWidget(explanation)
        outer.addSpacing(16)
        outer.addLayout(buttons)

        self.recent_caption = label("SON KULLANDIKLARIN", "section")
        self.recent_caption.setAlignment(Qt.AlignCenter)
        self.recent_list = QListWidget()
        self.recent_list.setFixedWidth(560)
        self.recent_list.itemActivated.connect(self._open_recent)
        self.recent_list.itemClicked.connect(self._open_recent)

        outer.addSpacing(26)
        outer.addWidget(self.recent_caption, 0, Qt.AlignHCenter)
        outer.addWidget(self.recent_list, 0, Qt.AlignHCenter)
        return page

    def _refresh_recent(self) -> None:
        projects = recent_projects()
        # This runs on every tick of the refresh timer. Rebuilding an unchanged list each
        # second resets hover and selection under the user's mouse, so only rebuild when
        # the list actually differs.
        signature = tuple((p.path, p.name) for p in projects)
        if signature == self._recent_signature:
            return
        self._recent_signature = signature
        self.recent_list.clear()
        for project in projects:
            folder = Path(project.path)
            # Full paths are long and mostly noise; the name plus its parent folder is what
            # someone recognises, and the rest is a tooltip away.
            where = folder.parent.name or str(folder.parent)
            item = QListWidgetItem(f"{project.name}   —   …\\{where}\\{folder.name}")
            item.setData(Qt.UserRole, project.path)
            item.setToolTip(project.path)
            self.recent_list.addItem(item)

        self.recent_caption.setVisible(bool(projects))
        self.recent_list.setVisible(bool(projects))
        # Size to content so one old project does not leave a large empty panel behind it.
        self.recent_list.setFixedHeight(min(len(projects), 5) * 34 + 8)

    def _open_recent(self, item: QListWidgetItem) -> None:
        self._open_path(Path(item.data(Qt.UserRole)))

    # -- workspace ------------------------------------------------------------------------

    def choose_folder(self) -> None:
        """Pick a folder and do the right thing with it, whichever it turns out to be."""
        chosen = QFileDialog.getExistingDirectory(self, "Montaj klasörünü seç")
        if not chosen:
            return
        path = Path(chosen)
        if Repository.is_workspace(path):
            self._open_path(path)
            return

        answer = QMessageBox.question(
            self,
            "Bu klasör henüz proje değil",
            f"<b>{path.name}</b> klasörünü projeye çevireyim mi?<br><br>"
            "Dosyalarının hiçbiri değişmez, silinmez veya taşınmaz. Klasöre sadece "
            "sürüm geçmişini tutan gizli bir <code>.solidgit</code> klasörü eklenir.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            self.state.initialise_workspace(path)
        except RepositoryError as e:
            QMessageBox.critical(self, "Olmadı", str(e))
            return
        self._after_open()

    def show_help(self) -> None:
        QMessageBox.information(
            self,
            "Nasıl çalışır?",
            "<b>1. Klasörünü kaydet.</b> Montaj klasörünü seç, aşağıya kısa bir isim yaz "
            "ve Kaydet'e bas. Geçmiş sayfasından herhangi bir dosyanın o anki hâline "
            "geri dönebilirsin.<br><br>"
            "<b>2. Üzerinde çalışacağın dosyayı kilitle.</b> Kilitli olmayan dosyalar "
            "salt-okunur olur; böylece kazara değiştirilemez ve iki kişi aynı parçayı "
            "aynı anda düzenleyemez.<br><br>"
            "<b>3. İşin bitince kaydet.</b> Ne yaptığını bir cümleyle yaz. Kilit "
            "kendiliğinden kalkar.<br><br>"
            "<b>4. Arkadaşlarınla çalış.</b> Ağ sekmesinden oturum başlat, çıkan kodu "
            "onlara söyle. Onlar da aynı ağa bağlanıp senin oturumunu seçsin — "
            "dosyalar onlarda olmasa da olur, katılıp indirebilirler.",
        )

    def _open_path(self, path: Path) -> None:
        try:
            self.state.open_workspace(path)
        except RepositoryError as e:
            forget_project(path)
            QMessageBox.critical(self, "Açılamadı", str(e))
            self._refresh_recent()
            return
        self._after_open()

    def show_recording(self) -> None:
        """Explain what is being recorded, and let the user see it or switch it off."""
        from ..appdata import app_data_dir
        from ..telemetry import recording_enabled, sessions_dir, set_recording_enabled

        enabled = recording_enabled()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Kullanım kaydı")
        box.setText(
            "Kayıt AÇIK — bu uygulamadaki kullanımın kaydediliyor."
            if enabled
            else "Kayıt KAPALI — hiçbir şey kaydedilmiyor."
        )
        box.setInformativeText(
            "<b>Kaydedilenler:</b> hangi butona bastığın, hangi sayfada olduğun, "
            "işlemlerin ne kadar sürdüğü, reddedilen kilitler ve hatalar.<br><br>"
            "<b>Kaydedilmeyenler:</b> yazdığın metinler, dosya içerikleri, "
            "bu uygulamanın dışındaki hiçbir şey.<br><br>"
            "Kayıtlar yalnızca bu bilgisayarda durur; hiçbir yere gönderilmez. "
            "Sorun yaşadığında <b>sessions</b> klasöründeki son dosyayı paylaşman yeterli."
            f"<br><br>Ayarlar ve kayıtlar uygulamanın yanındaki klasörde:"
            f"<br><code>{app_data_dir()}</code>"
        )
        box.addButton("Kapat", QMessageBox.AcceptRole)
        open_button = box.addButton("Klasörü aç", QMessageBox.ActionRole)
        toggle_button = box.addButton(
            "Kaydı durdur" if enabled else "Kaydı başlat", QMessageBox.DestructiveRole
        )
        box.setDefaultButton(open_button)
        box.exec()

        if box.clickedButton() is open_button:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_data_dir())))
        elif box.clickedButton() is toggle_button:
            set_recording_enabled(not enabled)
            self.recorder.set_enabled(not enabled)
            QMessageBox.information(
                self,
                "Kullanım kaydı",
                "Kayıt durduruldu." if enabled else "Kayıt başladı — hemen geçerli.",
            )

    def _after_open(self) -> None:
        """A project just became available — opened, created, or downloaded from a peer."""
        self._was_open = True
        for button in self.nav.buttons():
            button.setEnabled(True)
        self.pages.setCurrentIndex(FILES_PAGE)
        self.nav.button(FILES_PAGE).setChecked(True)

        repo = self.state.require_repo()
        manifest = repo.scan()
        self.recorder.event(
            "workspace_opened",
            project=repo.info.name,
            files=len(manifest),
            bytes=manifest.total_size(),
            commits=len(repo.commits.all_ids()),
        )
        self.refresh()

    # -- refresh --------------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.state.is_open:
            self._was_open = False
            self._refresh_without_project()
            return

        # A download finishes on a worker thread, so nothing calls `_after_open` for it —
        # the window notices the project appearing here instead. Without this the pages
        # stayed locked after a successful download, as if nothing had arrived.
        if not self._was_open:
            self._after_open()
            return

        repo = self.state.require_repo()
        head = repo.commits.head_commit()
        self.project_label.setText(repo.info.name)
        self.project_label.setToolTip(str(repo.root))

        # The commit id means nothing to someone who has never used version control; what
        # they recognise is their own sentence and when they wrote it.
        summary = (
            f"Son kayıt: {head.message}  ·  {head.wall_clock.replace('T', ' ')[:16]}"
            if head
            else "Henüz hiçbir şey kaydedilmedi"
        )
        # The workspace path is often far too long for the header, so it lives in the tooltip
        # and whatever is left gets elided rather than pushing the status text off-screen.
        metrics = QFontMetrics(self.head_label.font())
        self.head_label.setText(metrics.elidedText(summary, Qt.ElideRight, self.head_label.width()))
        self.head_label.setToolTip(summary)

        if self.state.is_hosting:
            connected = len(self.state.connected_users())
            pending = len(self.state.pending_join_requests())
            text = f"● Oturum açık · {connected} kişi bağlı"
            if pending:
                text += f" · {pending} istek bekliyor"
            self.status_label.setStyleSheet(f"color: {theme.OK};")
        elif self.state.session is not None:
            peer, _ = self.state.session
            text = f"● {peer.announcement.user_name} adlı kişinin oturumundasın"
            self.status_label.setStyleSheet(f"color: {theme.OK};")
        else:
            text = f"○ Oturum kapalı · {self.state.identity.user_name}"
            self.status_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.status_label.setText(text)

        current = self.pages.currentWidget()
        if hasattr(current, "refresh"):
            current.refresh()

    def _refresh_without_project(self) -> None:
        on_network = self.pages.currentWidget() is self.network_page
        if not on_network:
            self.pages.setCurrentWidget(self.welcome)
            self._refresh_recent()

        # Files and history have nothing to show yet; the network page stays reachable,
        # because joining a session is how someone without the files gets them.
        self.nav.setExclusive(False)
        for index, button in ((i, self.nav.button(i)) for i in (1, 2, 3)):
            button.setEnabled(index == NETWORK_PAGE)
            button.setChecked(index == NETWORK_PAGE and on_network)
        self.nav.setExclusive(True)

        self.project_label.setText("Proje açık değil")
        if self.state.session is not None:
            peer, _ = self.state.session
            self.head_label.setText(
                f"{peer.announcement.user_name} adlı kişinin oturumuna katıldın — "
                "dosyaları Ağ sayfasından indirebilirsin."
            )
            self.status_label.setStyleSheet(f"color: {theme.OK};")
            self.status_label.setText("● Oturumdasın")
        else:
            self.head_label.setText("")
            self.status_label.setText("")

        if on_network:
            self.network_page.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.recorder.event("window_closed")
        self.state.shutdown()
        super().closeEvent(event)
