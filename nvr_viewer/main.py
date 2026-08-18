#!/usr/bin/env python3
"""
NVR Viewer - muro video fino a 16 telecamere con discovery ONVIF automatica.

Lanciato dal menu applicazioni non ci sono argomenti da riga di comando,
quindi le impostazioni si leggono da ~/.config/nvr-viewer/config.json.
Gli argomenti CLI, quando presenti, hanno la precedenza.
"""

from __future__ import annotations

import argparse
import json
import locale
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

from . import onvif_lite as onvif

__version__ = "1.0.0"

log = logging.getLogger("nvr")


# --------------------------------------------------------------------------- #
# Configurazione persistente
# --------------------------------------------------------------------------- #

def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "nvr-viewer"


CONFIG_TEMPLATE = {
    "user": "",
    "password": "",
    "subnet": None,
    "interface": None,
    "timeout": 4.0,
    "layout": 16,
    "discovery": True,
    "cameras": [],
}


def load_config() -> dict:
    path = config_dir() / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("config %s illeggibile: %s", path, exc)
        return {}
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) and data.get("password"):
        log.warning("%s contiene una password ed e' leggibile da altri utenti; "
                    "esegui: chmod 600 %s", path, path)
    return data


def write_default_config() -> Path:
    """Crea un config.json di esempio con permessi 600."""
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    path.write_text(json.dumps(CONFIG_TEMPLATE, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def save_config(updates: dict) -> Path:
    """Aggiorna le chiavi indicate in config.json, preservando il resto
    (in particolare l'elenco 'cameras'). Crea il file se non esiste."""
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**CONFIG_TEMPLATE, **load_config(), **updates}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --------------------------------------------------------------------------- #
# Discovery in background
# --------------------------------------------------------------------------- #

def _make_worker():
    """Import ritardato di Qt: --list e --write-config non devono richiederlo."""
    from .qtcompat import QObject, Signal

    class DiscoveryWorker(QObject):
        found = Signal(object)
        finished = Signal(int)

        def __init__(self, args):
            super().__init__()
            self.args = args

        def run(self) -> None:
            try:
                cameras = onvif.discover_cameras(
                    user=self.args.user,
                    password=self.args.password,
                    timeout=self.args.timeout,
                    interface=self.args.interface,
                    fallback_scan=not self.args.no_scan,
                    subnet=self.args.subnet,
                    on_found=self.found.emit,
                )
            except Exception:                        # noqa: BLE001
                log.exception("discovery fallita")
                cameras = []
            self.finished.emit(len(cameras))

    return DiscoveryWorker


class DiscoveryController:
    """Avvia/riavvia la discovery ONVIF in un thread dedicato, cosi' la
    finestra di dialogo delle impostazioni puo' farla ripartire con
    credenziali nuove senza bloccare la UI."""

    def __init__(self, win):
        self.win = win
        self.thread = None

    def stop(self) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(3000)
            self.thread = None

    def start(self, args) -> None:
        from .qtcompat import QThread

        self.stop()
        self.win.status("discovery ONVIF in corso…")
        DiscoveryWorker = _make_worker()
        self.thread = QThread()
        worker = DiscoveryWorker(args)
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.found.connect(self.win.add_camera)
        worker.finished.connect(
            lambda n: self.win.status(f"discovery conclusa: {n} device")
            if n else self.win.status("nessun device ONVIF trovato — vedi "
                                      "/usr/share/doc/nvr-viewer/README.md")
        )
        worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(worker.deleteLater)
        self.thread.start()


# --------------------------------------------------------------------------- #
# Telecamere dichiarate a mano
# --------------------------------------------------------------------------- #

def build_cameras(entries: list[dict], user: str, password: str) -> list[onvif.Camera]:
    """
    Formato di ogni voce:

      {"name": "Ingresso", "main": "rtsp://.../101", "sub": "rtsp://.../102"}
      {"name": "Garage",   "host": "192.168.1.71"}

    Con gli URI si va diretti; con solo "host" si interroga ONVIF.
    """
    cameras: list[onvif.Camera] = []

    for entry in entries:
        name = entry.get("name") or entry.get("host") or "camera"
        u = entry.get("user", user)
        p = entry.get("password", password)

        if entry.get("main") or entry.get("sub"):
            streams = []
            for key, w, h in (("main", 1920, 1080), ("sub", 640, 360)):
                uri = entry.get(key)
                if uri:
                    streams.append(onvif.Stream(
                        token=key, name=key,
                        uri=onvif.with_credentials(uri, u, p),
                        width=w, height=h,
                    ))
            cameras.append(onvif.Camera(
                host=entry.get("host", ""), name=name, streams=streams))

        elif entry.get("host"):
            xaddr = entry.get(
                "xaddr",
                f"http://{entry['host']}:{entry.get('port', 80)}/onvif/device_service",
            )
            cam = onvif.resolve_camera(xaddr, u, p)
            if cam:
                cam.name = name
                cameras.append(cam)

    return cameras


def load_cameras_file(path: Path, user: str, password: str) -> list[onvif.Camera]:
    return build_cameras(json.loads(path.read_text(encoding="utf-8")), user, password)


def upsert_camera_entry(entries: list[dict], entry: dict) -> list[dict]:
    """Sostituisce, per host o per nome, la voce configurata dal dialogo
    'Configura telecamera'; se non trova corrispondenza la aggiunge."""
    entries = list(entries)
    for i, existing in enumerate(entries):
        same_host = entry.get("host") and existing.get("host") == entry.get("host")
        same_name = entry.get("name") and existing.get("name") == entry.get("name")
        if same_host or same_name:
            entries[i] = entry
            return entries
    return entries + [entry]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="nvr-viewer",
        description="Muro video ONVIF fino a 16 canali")
    p.add_argument("-u", "--user", default=None, help="utente ONVIF")
    p.add_argument("-p", "--password", default=None, help="password ONVIF")
    p.add_argument("-t", "--timeout", type=float, default=None,
                   help="secondi di attesa per la WS-Discovery (default 4)")
    p.add_argument("-i", "--interface", default=None,
                   help="IP locale da cui inviare la probe multicast")
    p.add_argument("-s", "--subnet", default=None,
                   help="subnet CIDR per la scansione di fallback")
    p.add_argument("-c", "--cameras", type=Path, default=None,
                   help="file JSON con telecamere dichiarate a mano")
    p.add_argument("--no-discovery", action="store_true",
                   help="salta la discovery ONVIF")
    p.add_argument("--no-scan", action="store_true",
                   help="non ripiegare sulla scansione TCP della subnet")
    p.add_argument("--layout", type=int, default=None, choices=(1, 4, 9, 16),
                   help="numero di riquadri iniziale")
    p.add_argument("--list", action="store_true",
                   help="stampa le telecamere trovate ed esce")
    p.add_argument("--write-config", action="store_true",
                   help="crea ~/.config/nvr-viewer/config.json ed esce")
    p.add_argument("--version", action="version", version=f"nvr-viewer {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)

    # Il file di configurazione riempie i valori non passati da CLI.
    cfg = load_config()
    args.user = args.user if args.user is not None else cfg.get("user", "")
    args.password = (args.password if args.password is not None
                     else cfg.get("password", ""))
    args.timeout = args.timeout or float(cfg.get("timeout") or 4.0)
    args.interface = args.interface or cfg.get("interface")
    args.subnet = args.subnet or cfg.get("subnet")
    args.layout = args.layout or int(cfg.get("layout") or 16)
    if not args.no_discovery and cfg.get("discovery") is False:
        args.no_discovery = True
    args.config_cameras = cfg.get("cameras") or []
    return args


def run_list(args) -> int:
    cameras = onvif.discover_cameras(
        user=args.user, password=args.password, timeout=args.timeout,
        interface=args.interface, fallback_scan=not args.no_scan,
        subnet=args.subnet,
    )
    if not cameras:
        print("Nessuna telecamera trovata.")
        print("Prova: --timeout 8, oppure --interface <ip locale>, "
              "oppure --subnet 192.168.1.0/24")
        return 1
    for cam in cameras:
        print(f"\n{cam.name}  [{cam.host}:{cam.port}]")
        for s in cam.streams:
            print(f"    {s}\n        {s.uri}")
    return 0


def _prepare_display() -> None:
    """
    L'embedding di mpv via --wid non e' affidabile su Wayland. Mint usa X11
    di default, ma su una sessione Wayland forziamo xcb se disponibile.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info("sessione Wayland rilevata: uso XWayland (QT_QPA_PLATFORM=xcb)")


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.write_config:
        path = write_default_config()
        print(f"Configurazione in {path}")
        print("Compila 'user' e 'password' con le credenziali ONVIF.")
        return 0

    if args.list:
        return run_list(args)

    _prepare_display()

    from .qtcompat import QApplication, QT_API
    from .viewer import MainWindow

    log.debug("binding Qt: %s", QT_API)

    app = QApplication(sys.argv)
    app.setApplicationName("NVR Viewer")
    app.setDesktopFileName("nvr-viewer")

    # Qt imposta LC_NUMERIC sulla locale di sistema (es. it_IT, virgola come
    # separatore decimale); libmpv/ffmpeg si aspettano sempre il punto e
    # vanno in segfault sul parsing dei numeri altrimenti. Vedi la nota di
    # python-mpv su LC_NUMERIC.
    locale.setlocale(locale.LC_NUMERIC, "C")

    win = MainWindow()
    win.wall.set_layout(args.layout)
    win.set_settings({
        "user": args.user, "password": args.password,
        "interface": args.interface, "subnet": args.subnet,
        "timeout": args.timeout, "discovery": not args.no_discovery,
    })
    win.show()

    for cam in build_cameras(args.config_cameras, args.user, args.password):
        win.add_camera(cam)
    if args.cameras:
        for cam in load_cameras_file(args.cameras, args.user, args.password):
            win.add_camera(cam)

    controller = DiscoveryController(win)

    def restart_discovery(settings: dict) -> None:
        save_config(settings)
        manual = build_cameras(args.config_cameras, settings["user"], settings["password"])
        if args.cameras:
            manual += load_cameras_file(args.cameras, settings["user"], settings["password"])
        win.reset_cameras(manual)
        if settings.get("discovery", True):
            controller.start(SimpleNamespace(
                user=settings["user"], password=settings["password"],
                timeout=settings["timeout"], interface=settings["interface"],
                subnet=settings["subnet"], no_scan=args.no_scan,
            ))
        else:
            controller.stop()
            win.status(f"{len(manual)} telecamere · discovery disattivata")

    win.settings_changed.connect(restart_discovery)

    def persist_camera(entry: dict) -> None:
        args.config_cameras = upsert_camera_entry(args.config_cameras, entry)
        save_config({"cameras": args.config_cameras})

    win.camera_configured.connect(persist_camera)

    if not args.no_discovery:
        controller.start(args)

    code = app.exec()
    controller.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
