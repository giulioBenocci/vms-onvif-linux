"""
viewer - muro video Qt con fino a 16 riquadri, ciascuno pilotato da libmpv.

Scelte architetturali che contano:

  * un'istanza mpv per riquadro, agganciata a una finestra nativa figlia
    tramite --wid: la decodifica e il rendering avvengono interamente in
    codice nativo, quindi il GIL non entra mai nel percorso dei frame;
  * in griglia si usa il *substream* della telecamera (tipicamente
    640x360), il mainstream solo quando un riquadro viene ingrandito.
    Questa singola scelta e' cio' che rende sostenibili 16 flussi;
  * hwdec=auto-copy-safe per delegare la decodifica alla GPU quando
    possibile, ma ricopiando il fotogramma in una texture normale invece
    di un piano overlay a copia zero: quel piano ignorerebbe il clipping
    della finestra --wid e il video sconfinerebbe fuori dal riquadro.
"""

from __future__ import annotations

import locale
import logging
from urllib.parse import urlsplit

from . import onvif_lite as onvif
from .qtcompat import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QIcon,
    QKeySequence,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QShortcut,
    QSpinBox,
    QStatusBar,
    Qt,
    QTimer,
    QVBoxLayout,
    QWidget,
    Signal,
)

log = logging.getLogger(__name__)

try:
    import mpv
except (ImportError, OSError) as exc:      # OSError = libmpv non trovata
    raise SystemExit(
        "Serve libmpv piu' il binding python-mpv.\n"
        "  Debian/Ubuntu/Mint : sudo apt install python3-mpv\n"
        "  macOS              : brew install mpv && pip install python-mpv\n"
        "  Windows            : libmpv-2.dll accanto al programma\n"
        f"Dettaglio: {exc}"
    ) from exc


# Layout supportati: numero di riquadri -> (righe, colonne)
LAYOUTS = {1: (1, 1), 4: (2, 2), 9: (3, 3), 16: (4, 4)}

MPV_OPTIONS = dict(
    vo="gpu",
    # --wid e' un meccanismo X11: forziamo esplicitamente il contesto GPU
    # su X11/EGL, altrimenti su una sessione Wayland mpv puo' autorilevare
    # WAYLAND_DISPLAY e aprire un suo contesto Wayland nativo, ignorando
    # wid e creando una finestra separata invece di incapsularsi.
    gpu_context="x11egl",
    # "-copy" forza la ricopia del fotogramma decodificato in una texture
    # normale, composta dentro la finestra dal renderer di mpv. Senza,
    # "auto-safe" puo' scegliere un percorso a copia zero che scansiona il
    # video su un piano overlay DRM/VAAPI separato: quel piano ignora il
    # clipping della finestra X11 in cui e' incapsulato (--wid), quindi il
    # video "sborda" dal riquadro. Costa un po' di CPU in piu' per la copia,
    # ma la decodifica resta comunque su GPU.
    hwdec="auto-copy-safe",
    profile="low-latency",
    aid="no",                 # niente audio: 16 tracce audio non servono
    osc=False,
    border=False,
    osd_level=0,
    input_default_bindings=False,
    input_vo_keyboard=False,
    keep_open="no",
    rtsp_transport="tcp",     # UDP perde pacchetti appena la rete si carica
    network_timeout=8,
    cache="no",               # buffering = latenza, qui vogliamo il live
    demuxer_lavf_o="reorder_queue_size=0",
)

STYLE = """
QMainWindow, QWidget#wall { background: #111214; }
QFrame#tile { background: #000; border: 1px solid #24262b; }
QFrame#tile[selected="true"] { border: 1px solid #4c8dff; }
QLabel#caption {
    color: #c8ccd4; background: #16181c; padding: 3px 7px;
    font-size: 11px; font-family: "DejaVu Sans Mono", monospace;
}
QFrame#tile[selected="true"] QLabel#caption { color: #fff; background: #1d3557; }
QStatusBar { color: #9aa0a6; background: #16181c; }
"""

_ATTR = Qt.WidgetAttribute


class _Surface(QWidget):
    """Finestra nativa su cui mpv disegna. Non deve avere figli Qt."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(_ATTR.WA_DontCreateNativeAncestors)
        self.setAttribute(_ATTR.WA_NativeWindow)
        self.setAttribute(_ATTR.WA_OpaquePaintEvent)
        self.setMinimumSize(160, 90)
        # Forza subito la creazione della finestra nativa X11, invece di
        # lasciarla pigra al primo mpv.MPV(wid=...). Un widget che diventa
        # nativo a meta' vita (dopo essere gia' passato per layout come
        # widget "alieno") puo' perdere la sincronizzazione posizione/
        # dimensione con Qt sui passaggi di layout successivi: creandola
        # da subito, tutta la vita del widget passa dal percorso nativo,
        # quello testato e affidabile.
        self.winId()


class Tile(QFrame):
    """Un riquadro del muro video."""

    zoom_requested = Signal(object)
    selected_changed = Signal(object)
    configure_requested = Signal(object)
    _status = Signal(str)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.camera = None
        self.quality = "sub"
        self._mpv = None
        self._observer = None

        self.setObjectName("tile")
        self.setProperty("selected", False)

        self.caption = QLabel(f"— {index + 1} —")
        self.caption.setObjectName("caption")
        self.surface = _Surface(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.caption)
        layout.addWidget(self.surface, 1)

        self._status.connect(self._on_status)

    # -- ciclo di vita di mpv --------------------------------------------- #

    def _ensure_player(self):
        if self._mpv is None:
            # Qt (o un widget locale-aware creato nel frattempo, es. uno
            # QDoubleSpinBox) puo' aver rimesso LC_NUMERIC sulla locale di
            # sistema: libmpv segfaulta sul parsing dei numeri se non e' "C".
            locale.setlocale(locale.LC_NUMERIC, "C")
            self._mpv = mpv.MPV(
                wid=str(int(self.surface.winId())),
                **MPV_OPTIONS,
            )

            # L'observer gira nel thread di mpv: passiamo per un Signal Qt,
            # che accoda in modo sicuro verso il thread della UI.
            @self._mpv.property_observer("width")
            def _on_width(_name, value):
                self._status.emit("live" if value else "connessione…")

            self._observer = _on_width
        return self._mpv

    def play(self, camera, quality: str = "sub") -> None:
        stream = camera.stream(quality) if camera else None
        if stream is None:
            self.clear()
            return
        self.camera = camera
        self.quality = quality
        self._label(camera.name, "connessione…")
        try:
            self._ensure_player().play(stream.uri)
        except Exception as exc:                     # noqa: BLE001
            log.warning("riquadro %d: %s", self.index, exc)
            self._label(camera.name, "errore")

    def set_quality(self, quality: str) -> None:
        if self.camera and quality != self.quality:
            self.play(self.camera, quality)

    def stop(self) -> None:
        """Ferma il flusso ma tiene vivo il player (riparte piu' in fretta)."""
        if self._mpv is not None:
            try:
                self._mpv.command("stop")
            except Exception:                        # noqa: BLE001
                pass

    def clear(self) -> None:
        self.camera = None
        self.stop()
        self.caption.setText(f"— {self.index + 1} —")

    def shutdown(self) -> None:
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:                        # noqa: BLE001
                pass
            self._mpv = None

    # -- presentazione ----------------------------------------------------- #

    def _label(self, name: str, state: str) -> None:
        tag = "HD" if self.quality == "main" else "sub"
        self.caption.setText(f"{name}  ·  {tag}  ·  {state}")

    def _on_status(self, state: str) -> None:
        if self.camera:
            self._label(self.camera.name, state)

    def set_selected(self, value: bool) -> None:
        self.setProperty("selected", value)
        self.style().unpolish(self)
        self.style().polish(self)

    # -- eventi ------------------------------------------------------------ #

    def mousePressEvent(self, event):
        self.selected_changed.emit(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.zoom_requested.emit(self)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        label = "Configura telecamera…" if self.camera is None \
            else "Credenziali / RTSP di questa telecamera…"
        menu.addAction(label, lambda: self.configure_requested.emit(self))
        menu.exec(event.globalPos())


class VideoWall(QWidget):
    """Griglia di riquadri con paginazione e zoom."""

    configure_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("wall")

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(2)

        self.tiles: list[Tile] = []
        for i in range(16):
            tile = Tile(i, self)
            tile.zoom_requested.connect(self.toggle_zoom)
            tile.selected_changed.connect(self.select)
            tile.configure_requested.connect(self.configure_requested.emit)
            self.tiles.append(tile)

        self.cameras: list = []
        self.page = 0
        self.capacity = 16
        self.zoomed: Tile | None = None
        self.selected: Tile | None = None

        self.set_layout(16)

    # -- layout ------------------------------------------------------------ #

    def set_layout(self, capacity: int) -> None:
        if capacity not in LAYOUTS:
            capacity = min(k for k in LAYOUTS if k >= capacity)
        self.capacity = capacity
        self.zoomed = None
        rows, cols = LAYOUTS[capacity]

        while self._grid.count():
            self._grid.takeAt(0)

        for i, tile in enumerate(self.tiles):
            if i < capacity:
                self._grid.addWidget(tile, i // cols, i % cols)
                tile.show()
            else:
                tile.hide()
                tile.stop()

        for r in range(rows):
            self._grid.setRowStretch(r, 1)
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

        self.page = 0
        self.refresh()

    def toggle_zoom(self, tile: Tile) -> None:
        if self.zoomed is tile:
            self.restore()
        else:
            self._zoom(tile)

    def _zoom(self, tile: Tile) -> None:
        if tile.camera is None:
            return
        for t in self.tiles:
            if t is tile:
                t.show()
            else:
                t.hide()
                t.stop()
        # A schermo intero ha senso il flusso in alta risoluzione.
        tile.set_quality("main")
        self.zoomed = tile
        self.select(tile)

    def restore(self) -> None:
        self.zoomed = None
        self.set_layout(self.capacity)

    def select(self, tile: Tile) -> None:
        if self.selected is tile:
            return
        if self.selected is not None:
            self.selected.set_selected(False)
        self.selected = tile
        tile.set_selected(True)

    # -- sorgenti ---------------------------------------------------------- #

    def set_cameras(self, cameras: list) -> None:
        self.cameras = cameras
        self.page = min(self.page, max(0, self.pages - 1))
        self.refresh()

    def replace_camera(self, old_camera, new_camera) -> None:
        """Sostituisce una telecamera esistente, o la aggiunge se old_camera
        e' None (riquadro vuoto configurato per la prima volta)."""
        cams = list(self.cameras)
        if old_camera is not None and old_camera in cams:
            cams[cams.index(old_camera)] = new_camera
        else:
            cams.append(new_camera)
        self.set_cameras(cams)

    @property
    def pages(self) -> int:
        if not self.cameras:
            return 1
        return (len(self.cameras) + self.capacity - 1) // self.capacity

    def turn_page(self, delta: int) -> None:
        if self.zoomed:
            self.restore()
        self.page = (self.page + delta) % self.pages
        self.refresh()

    def refresh(self) -> None:
        if self.zoomed:
            return
        start = self.page * self.capacity
        window = self.cameras[start:start + self.capacity]
        for i in range(self.capacity):
            tile = self.tiles[i]
            if i < len(window):
                cam = window[i]
                if tile.camera is not cam or tile.quality != "sub":
                    tile.play(cam, "sub")
            else:
                tile.clear()

    def shutdown(self) -> None:
        for tile in self.tiles:
            tile.shutdown()


class CameraDialog(QDialog):
    """Configura una singola telecamera: ONVIF con credenziali proprie
    (utile quando un device vuole un utente diverso da quello globale), o
    URL RTSP inseriti a mano quando la discovery non trova l'endpoint ONVIF."""

    def __init__(self, camera, defaults: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configura telecamera")
        self.setMinimumWidth(420)
        self.camera = None
        self.entry: dict | None = None

        self.name = QLineEdit(camera.name if camera else "")
        self.name.setPlaceholderText("nome visualizzato")

        self.mode = QComboBox()
        self.mode.addItems(["ONVIF (rilevamento automatico)", "RTSP manuale"])

        # -- ONVIF -- #
        self.host = QLineEdit(camera.host if camera else "")
        self.host.setPlaceholderText("192.168.1.64")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(camera.port if camera and camera.port else 80)
        self.onvif_user = QLineEdit()
        self.onvif_user.setPlaceholderText(
            f"vuoto = usa quello globale ({defaults.get('user') or '—'})")
        self.onvif_password = QLineEdit()
        self.onvif_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.onvif_password.setPlaceholderText("vuoto = usa la password globale")

        onvif_form = QFormLayout()
        onvif_form.addRow("Host", self.host)
        onvif_form.addRow("Porta", self.port)
        onvif_form.addRow("Utente", self.onvif_user)
        onvif_form.addRow("Password", self.onvif_password)
        self.onvif_group = QWidget()
        self.onvif_group.setLayout(onvif_form)

        # -- RTSP manuale -- #
        main_stream = camera.main if camera else None
        sub_stream = camera.sub if camera else None
        main_uri = main_stream.uri if main_stream else ""
        sub_uri = sub_stream.uri if sub_stream and sub_stream is not main_stream else ""

        self.rtsp_main = QLineEdit(main_uri)
        self.rtsp_main.setPlaceholderText("rtsp://host:554/Streaming/Channels/101")
        self.rtsp_sub = QLineEdit(sub_uri)
        self.rtsp_sub.setPlaceholderText("rtsp://host:554/Streaming/Channels/102 (opzionale)")
        self.rtsp_user = QLineEdit()
        self.rtsp_user.setPlaceholderText("utente (opzionale, se non gia' nell'URL)")
        self.rtsp_password = QLineEdit()
        self.rtsp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.rtsp_password.setPlaceholderText("password (opzionale)")

        rtsp_form = QFormLayout()
        rtsp_form.addRow("URL principale (HD)", self.rtsp_main)
        rtsp_form.addRow("URL secondario (sub)", self.rtsp_sub)
        rtsp_form.addRow("Utente", self.rtsp_user)
        rtsp_form.addRow("Password", self.rtsp_password)
        self.rtsp_group = QWidget()
        self.rtsp_group.setLayout(rtsp_form)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e05d5d;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        top_form = QFormLayout()
        top_form.addRow("Nome", self.name)
        top_form.addRow("Modalità", self.mode)

        layout = QVBoxLayout(self)
        layout.addLayout(top_form)
        layout.addWidget(self.onvif_group)
        layout.addWidget(self.rtsp_group)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

        self.mode.currentIndexChanged.connect(self._update_mode)
        # Se la telecamera esistente non ha un xaddr ONVIF ma ha degli
        # stream, e' stata configurata a mano: precompila quella modalita'.
        start_rtsp = camera is not None and not camera.xaddr and bool(camera.streams)
        self.mode.setCurrentIndex(1 if start_rtsp else 0)
        self._update_mode()

    def _update_mode(self) -> None:
        rtsp = self.mode.currentIndex() == 1
        self.rtsp_group.setVisible(rtsp)
        self.onvif_group.setVisible(not rtsp)

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()
        name = self.name.text().strip()

        if self.mode.currentIndex() == 1:
            self._accept_rtsp(name)
        else:
            self._accept_onvif(name)

    def _accept_rtsp(self, name: str) -> None:
        main = self.rtsp_main.text().strip()
        sub = self.rtsp_sub.text().strip()
        if not main and not sub:
            self._fail("Inserisci almeno un URL RTSP.")
            return

        user = self.rtsp_user.text().strip()
        password = self.rtsp_password.text()
        streams = []
        if main:
            uri = onvif.with_credentials(main, user, password) if user else main
            streams.append(onvif.Stream(token="main", name="main", uri=uri,
                                        width=1920, height=1080))
        if sub:
            uri = onvif.with_credentials(sub, user, password) if user else sub
            streams.append(onvif.Stream(token="sub", name="sub", uri=uri,
                                        width=640, height=360))

        host = urlsplit(main or sub).hostname or ""
        camera_name = name or host or "camera"
        self.camera = onvif.Camera(host=host, name=camera_name, streams=streams)
        self.entry = {"name": camera_name, "main": main, "sub": sub}
        if user:
            self.entry["user"] = user
            self.entry["password"] = password
        self.accept()

    def _accept_onvif(self, name: str) -> None:
        host = self.host.text().strip()
        if not host:
            self._fail("Inserisci l'indirizzo host.")
            return

        port = self.port.value()
        user = self.onvif_user.text().strip()
        password = self.onvif_password.text()
        xaddr = f"http://{host}:{port}/onvif/device_service"

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            camera = onvif.resolve_camera(xaddr, user, password)
        except Exception as exc:                          # noqa: BLE001
            camera = None
            log.warning("configurazione manuale %s: %s", host, exc)
        finally:
            QApplication.restoreOverrideCursor()

        if camera is None:
            self._fail(f"{host}:{port} non raggiungibile. Verifica host/porta, "
                       "oppure passa a RTSP manuale.")
            return

        camera.name = name or camera.name
        self.camera = camera
        self.entry = {"name": camera.name, "host": host, "port": port}
        if user:
            self.entry["user"] = user
            self.entry["password"] = password
        self.accept()

    @staticmethod
    def edit(parent, camera, defaults: dict):
        dialog = CameraDialog(camera, defaults, parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.camera, dialog.entry
        return None, None


class SettingsDialog(QDialog):
    """Credenziali ONVIF e parametri di discovery, modificabili a runtime."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Impostazioni ONVIF")
        self.setMinimumWidth(360)

        self.user = QLineEdit(settings.get("user") or "")
        self.user.setPlaceholderText("utente ONVIF")

        self.password = QLineEdit(settings.get("password") or "")
        self.password.setPlaceholderText("password ONVIF")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        self.show_password = QCheckBox("mostra")
        self.show_password.toggled.connect(
            lambda on: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )

        self.interface = QLineEdit(settings.get("interface") or "")
        self.interface.setPlaceholderText("auto")

        self.subnet = QLineEdit(settings.get("subnet") or "")
        self.subnet.setPlaceholderText("es. 192.168.1.0/24 (solo se il multicast è bloccato)")

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1.0, 30.0)
        self.timeout.setSingleStep(0.5)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(float(settings.get("timeout") or 4.0))

        self.discovery = QCheckBox("discovery ONVIF automatica all'avvio")
        self.discovery.setChecked(bool(settings.get("discovery", True)))

        form = QFormLayout()
        form.addRow("Utente", self.user)
        pw_row = QWidget()
        pw_layout = QGridLayout(pw_row)
        pw_layout.setContentsMargins(0, 0, 0, 0)
        pw_layout.addWidget(self.password, 0, 0)
        pw_layout.addWidget(self.show_password, 0, 1)
        form.addRow("Password", pw_row)
        form.addRow("Interfaccia locale", self.interface)
        form.addRow("Subnet (fallback)", self.subnet)
        form.addRow("Timeout discovery", self.timeout)
        form.addRow("", self.discovery)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "user": self.user.text().strip(),
            "password": self.password.text(),
            "interface": self.interface.text().strip() or None,
            "subnet": self.subnet.text().strip() or None,
            "timeout": self.timeout.value(),
            "discovery": self.discovery.isChecked(),
        }

    @staticmethod
    def edit(parent, settings: dict) -> dict | None:
        dialog = SettingsDialog(settings, parent)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.values()
        return None


class MainWindow(QMainWindow):

    settings_changed = Signal(dict)
    camera_configured = Signal(dict)

    def __init__(self, title: str = "NVR Viewer"):
        super().__init__()
        self.setWindowTitle(title)
        self.setWindowIcon(QIcon.fromTheme("nvr-viewer"))
        self.resize(1600, 900)
        self.setStyleSheet(STYLE)

        self._settings: dict = {}

        self.wall = VideoWall(self)
        self.wall.configure_requested.connect(self._configure_camera)
        self.setCentralWidget(self.wall)
        self.setStatusBar(QStatusBar())
        self.status("pronto — tasto destro su un riquadro per configurare una telecamera")

        self._build_menu()

        self._bind("1", lambda: self.wall.set_layout(1))
        self._bind("2", lambda: self.wall.set_layout(4))
        self._bind("3", lambda: self.wall.set_layout(9))
        self._bind("4", lambda: self.wall.set_layout(16))
        self._bind("Right", lambda: self.wall.turn_page(1))
        self._bind("Left", lambda: self.wall.turn_page(-1))
        self._bind("Esc", self._escape)
        self._bind("F11", self._toggle_fullscreen)
        self._bind("Ctrl+Q", self.close)
        self._bind("Ctrl+,", self.open_settings)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("Impostazioni")
        action = QAction("Credenziali ONVIF…", self)
        action.setShortcut(QKeySequence("Ctrl+,"))
        action.triggered.connect(self.open_settings)
        menu.addAction(action)

    def set_settings(self, settings: dict) -> None:
        """Valori correnti (user/password/interface/subnet/timeout/discovery)."""
        self._settings = dict(settings)

    def open_settings(self) -> None:
        result = SettingsDialog.edit(self, self._settings)
        if result is not None:
            self._settings = result
            self.settings_changed.emit(result)

    def _configure_camera(self, tile) -> None:
        camera, entry = CameraDialog.edit(self, tile.camera, self._settings)
        if camera is None:
            return
        self.wall.replace_camera(tile.camera, camera)
        self.camera_configured.emit(entry)
        self.status(f"telecamera '{camera.name}' salvata")

    def _bind(self, keys: str, slot) -> None:
        shortcut = QShortcut(QKeySequence(keys), self)
        shortcut.activated.connect(slot)

    def _escape(self) -> None:
        if self.wall.zoomed:
            self.wall.restore()
        elif self.isFullScreen():
            self.showNormal()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def add_camera(self, camera) -> None:
        self.wall.set_cameras(self.wall.cameras + [camera])
        n = len(self.wall.cameras)
        self.status(f"{n} telecamere · pagina {self.wall.page + 1}/{self.wall.pages}"
                    f" · 1-4 layout · ←/→ pagina · doppio click zoom · F11 fullscreen")

    def reset_cameras(self, cameras: list) -> None:
        """Sostituisce l'elenco telecamere, es. dopo un cambio di credenziali."""
        self.wall.set_cameras(list(cameras))
        n = len(self.wall.cameras)
        self.status(f"{n} telecamere")

    def closeEvent(self, event):
        self.wall.shutdown()
        QTimer.singleShot(0, lambda: None)   # lascia chiudere i thread mpv
        super().closeEvent(event)
