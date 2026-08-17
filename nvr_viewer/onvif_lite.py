"""
onvif_lite - Discovery ONVIF (WS-Discovery) e client SOAP minimale.

Nessuna dipendenza esterna: solo stdlib. Implementa quel poco di ONVIF che
serve a un visualizzatore:

  * WS-Discovery su multicast 239.255.255.250:3702  -> lista di XAddr
  * scansione TCP della subnet come fallback       -> host candidati
  * Media.GetProfiles                              -> profili + risoluzioni
  * Media.GetStreamUri                             -> URI RTSP per profilo

L'autenticazione usa WS-Security UsernameToken (PasswordDigest), che e' lo
schema previsto dal Core Spec ONVIF; per i device che invece pretendono HTTP
Digest c'e' un fallback automatico.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import socket
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, quote
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
    urlopen,
)

log = logging.getLogger(__name__)

WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702

NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"

# Porte tipiche del servizio ONVIF/HTTP dei device TVCC.
COMMON_ONVIF_PORTS = (80, 8000, 8080, 8899, 2020, 5000)

# Ultima spiaggia: se ONVIF non risponde (o non abbiamo le credenziali)
# proviamo i path RTSP piu' diffusi. {main} e {sub} vengono sostituiti.
FALLBACK_RTSP_PATHS = [
    # Hikvision e cloni
    ("Hikvision", "/Streaming/Channels/101", "/Streaming/Channels/102"),
    # Dahua / Amcrest / Lorex
    ("Dahua", "/cam/realmonitor?channel=1&subtype=0",
              "/cam/realmonitor?channel=1&subtype=1"),
    # Axis
    ("Axis", "/axis-media/media.amp", "/axis-media/media.amp?resolution=640x360"),
    # Reolink
    ("Reolink", "/h264Preview_01_main", "/h264Preview_01_sub"),
    # Generici / ONVIF profile S
    ("Generic", "/live/ch00_0", "/live/ch00_1"),
]


# --------------------------------------------------------------------------- #
# Modello dati
# --------------------------------------------------------------------------- #

@dataclass
class Stream:
    """Un profilo video pubblicato dalla telecamera."""
    token: str
    name: str
    uri: str
    width: int = 0
    height: int = 0
    encoding: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        if self.width:
            return f"{self.name} ({self.width}x{self.height} {self.encoding})"
        return self.name


@dataclass
class Camera:
    host: str
    port: int = 80
    name: str = ""
    xaddr: str = ""
    streams: list[Stream] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.host

    @property
    def main(self) -> Stream | None:
        """Flusso a risoluzione piu' alta (per la vista a tutto schermo)."""
        return max(self.streams, key=lambda s: s.pixels, default=None)

    @property
    def sub(self) -> Stream | None:
        """Flusso a risoluzione piu' bassa (per la griglia 4x4)."""
        return min(self.streams, key=lambda s: s.pixels, default=None)

    def stream(self, prefer: str = "sub") -> Stream | None:
        return self.sub if prefer == "sub" else self.main


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #

def _lname(tag: str) -> str:
    """Nome locale di un tag, senza namespace."""
    return tag.rpartition("}")[2]


def _child(el, name: str):
    for c in el:
        if _lname(c.tag) == name:
            return c
    return None


def _text(el, name: str, default: str = "") -> str:
    c = _child(el, name)
    return (c.text or default).strip() if c is not None else default


def default_local_ip() -> str:
    """IP dell'interfaccia usata come route di default (nessun traffico reale)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def local_subnet(prefix: int = 24) -> str:
    ip = default_local_ip()
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def with_credentials(uri: str, user: str, password: str) -> str:
    """Inietta user:password nella netloc di un URI RTSP."""
    if not user or not uri:
        return uri
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if ":" in host:                      # IPv6
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return urlunsplit(parts._replace(netloc=f"{userinfo}@{host}"))


def rewrite_host(uri: str, host: str) -> str:
    """
    Alcuni device annunciano nell'URI RTSP il proprio IP interno o un hostname
    non risolvibile: lo sostituiamo con l'indirizzo da cui li abbiamo raggiunti.
    """
    parts = urlsplit(uri)
    if parts.hostname == host:
        return uri
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit(parts._replace(netloc=netloc))


# --------------------------------------------------------------------------- #
# WS-Discovery
# --------------------------------------------------------------------------- #

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{mid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


def ws_discover(timeout: float = 4.0, interface: str | None = None) -> list[str]:
    """
    Manda una Probe ONVIF in multicast e raccoglie gli XAddr annunciati.

    Ritorna una lista di URL del device service, es.
    ['http://192.168.1.64/onvif/device_service', ...]
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((interface or "0.0.0.0", 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        if interface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(interface))
        sock.settimeout(0.4)

        # Il multicast e' best-effort: tre probe riducono i falsi negativi.
        for _ in range(3):
            probe = _PROBE.format(mid=uuid.uuid4()).encode()
            try:
                sock.sendto(probe, (WSD_GROUP, WSD_PORT))
            except OSError as exc:
                log.warning("probe non inviata: %s", exc)
            time.sleep(0.15)

        found: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            for xaddr in _parse_probe_match(data):
                host = urlsplit(xaddr).hostname or addr[0]
                found.setdefault(host, xaddr)
        return list(found.values())
    finally:
        sock.close()


def _parse_probe_match(data: bytes) -> list[str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out: list[str] = []
    for el in root.iter():
        if _lname(el.tag) != "XAddrs" or not el.text:
            continue
        out += [u for u in el.text.split() if u.startswith("http")]
    return out


# --------------------------------------------------------------------------- #
# Fallback: scansione TCP della subnet
# --------------------------------------------------------------------------- #

def scan_subnet(cidr: str | None = None,
                ports: tuple[int, ...] = COMMON_ONVIF_PORTS,
                timeout: float = 0.35,
                workers: int = 128) -> list[tuple[str, int]]:
    """
    Utile quando il multicast e' bloccato (VLAN, Wi-Fi con IGMP snooping,
    Docker con rete bridge). Ritorna coppie (host, porta) raggiungibili.
    """
    net = ipaddress.ip_network(cidr or local_subnet(), strict=False)
    targets = [(str(ip), p) for ip in net.hosts() for p in ports]

    def probe(target):
        host, port = target
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return target if s.connect_ex((host, port)) == 0 else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        hits = [r for r in pool.map(probe, targets) if r]

    # Una porta per host: la prima che risponde.
    seen: dict[str, int] = {}
    for host, port in hits:
        seen.setdefault(host, port)
    return sorted(seen.items())


# --------------------------------------------------------------------------- #
# Client SOAP
# --------------------------------------------------------------------------- #

class SoapError(RuntimeError):
    pass


class MediaClient:
    """Client per il Media service ONVIF (ver10/media/wsdl)."""

    def __init__(self, xaddr: str, user: str = "", password: str = "",
                 timeout: float = 6.0):
        self.xaddr = xaddr
        self.user = user
        self.password = password
        self.timeout = timeout
        self.host = urlsplit(xaddr).hostname or ""

    # -- trasporto -------------------------------------------------------- #

    def _security_header(self) -> str:
        if not self.user:
            return ""
        nonce = os.urandom(16)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        wsse = ("http://docs.oasis-open.org/wss/2004/01/"
                "oasis-200401-wss-wssecurity-secext-1.0.xsd")
        wsu = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-wssecurity-utility-1.0.xsd")
        pwtype = ("http://docs.oasis-open.org/wss/2004/01/"
                  "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
        enctype = ("http://docs.oasis-open.org/wss/2004/01/"
                   "oasis-200401-wss-soap-message-security-1.0#Base64Binary")
        return (
            f'<Security s:mustUnderstand="1" xmlns="{wsse}">'
            f"<UsernameToken>"
            f"<Username>{self.user}</Username>"
            f'<Password Type="{pwtype}">{digest}</Password>'
            f'<Nonce EncodingType="{enctype}">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            f'<Created xmlns="{wsu}">{created}</Created>'
            f"</UsernameToken></Security>"
        )

    def _call(self, body: str) -> ET.Element:
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"<s:Header>{self._security_header()}</s:Header>"
            f"<s:Body>{body}</s:Body>"
            "</s:Envelope>"
        ).encode()

        req = Request(
            self.xaddr,
            data=envelope,
            headers={"Content-Type": 'application/soap+xml; charset=utf-8'},
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except HTTPError as exc:
            if exc.code == 401 and self.user:
                raw = self._call_http_digest(envelope)
            else:
                raise SoapError(f"HTTP {exc.code} da {self.xaddr}") from exc
        except (URLError, OSError, socket.timeout) as exc:
            raise SoapError(f"{self.xaddr} non raggiungibile: {exc}") from exc

        root = ET.fromstring(raw)
        for el in root.iter():
            if _lname(el.tag) == "Fault":
                raise SoapError(_fault_reason(el))
        return root

    def _call_http_digest(self, envelope: bytes) -> bytes:
        """Alcuni firmware ignorano WS-Security e vogliono HTTP Digest."""
        mgr = HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.xaddr, self.user, self.password)
        opener = build_opener(HTTPDigestAuthHandler(mgr))
        req = Request(
            self.xaddr,
            data=envelope,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            method="POST",
        )
        with opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    # -- operazioni ------------------------------------------------------- #

    def get_profiles(self) -> list[dict]:
        root = self._call(f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        profiles = []
        for el in root.iter():
            if _lname(el.tag) != "Profiles":
                continue
            entry = {
                "token": el.get("token", ""),
                "name": _text(el, "Name"),
                "width": 0,
                "height": 0,
                "encoding": "",
            }
            vec = _child(el, "VideoEncoderConfiguration")
            if vec is not None:
                entry["encoding"] = _text(vec, "Encoding")
                res = _child(vec, "Resolution")
                if res is not None:
                    entry["width"] = int(_text(res, "Width", "0") or 0)
                    entry["height"] = int(_text(res, "Height", "0") or 0)
            if entry["token"]:
                profiles.append(entry)
        return profiles

    def get_stream_uri(self, token: str) -> str:
        body = (
            f'<GetStreamUri xmlns="{NS_MEDIA}">'
            f'<StreamSetup xmlns="{NS_SCHEMA}">'
            "<Stream>RTP-Unicast</Stream>"
            "<Transport><Protocol>RTSP</Protocol></Transport>"
            "</StreamSetup>"
            f"<ProfileToken>{token}</ProfileToken>"
            "</GetStreamUri>"
        )
        root = self._call(body)
        for el in root.iter():
            if _lname(el.tag) == "Uri" and el.text:
                return el.text.strip()
        raise SoapError("GetStreamUri non ha restituito alcun Uri")


def _fault_reason(fault: ET.Element) -> str:
    texts = [(e.text or "").strip() for e in fault.iter()
             if _lname(e.tag) in ("Text", "faultstring") and e.text]
    return " / ".join(texts) or "SOAP Fault"


# --------------------------------------------------------------------------- #
# Risoluzione di una telecamera
# --------------------------------------------------------------------------- #

def resolve_camera(xaddr: str, user: str = "", password: str = "",
                   timeout: float = 6.0) -> Camera | None:
    """
    Da un XAddr ONVIF ricava profili e URI RTSP completi di credenziali.
    Ritorna None se il device non risponde in modo utilizzabile.
    """
    parts = urlsplit(xaddr)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    client = MediaClient(xaddr, user, password, timeout)

    try:
        profiles = client.get_profiles()
    except (SoapError, ET.ParseError) as exc:
        log.info("%s: ONVIF non utilizzabile (%s)", host, exc)
        return _fallback_camera(host, user, password)

    streams: list[Stream] = []
    for prof in profiles:
        try:
            uri = client.get_stream_uri(prof["token"])
        except (SoapError, ET.ParseError) as exc:
            log.debug("%s/%s: GetStreamUri fallita (%s)", host, prof["token"], exc)
            continue
        uri = with_credentials(rewrite_host(uri, host), user, password)
        streams.append(Stream(
            token=prof["token"],
            name=prof["name"] or prof["token"],
            uri=uri,
            width=prof["width"],
            height=prof["height"],
            encoding=prof["encoding"],
        ))

    if not streams:
        return _fallback_camera(host, user, password)

    return Camera(host=host, port=port, name=host, xaddr=xaddr, streams=streams)


def _fallback_camera(host: str, user: str, password: str) -> Camera | None:
    """
    Costruisce una Camera con i path RTSP piu' comuni. Non verifica che
    funzionino: sara' il player a scartare quelli morti.
    """
    if not host:
        return None
    vendor, main, sub = FALLBACK_RTSP_PATHS[0]
    base = f"rtsp://{host}:554"
    streams = [
        Stream("main", "main (guess)",
               with_credentials(base + main, user, password), 1920, 1080),
        Stream("sub", "sub (guess)",
               with_credentials(base + sub, user, password), 640, 360),
    ]
    log.info("%s: uso path RTSP presunti (%s)", host, vendor)
    return Camera(host=host, name=f"{host} (?)", streams=streams)


def discover_cameras(user: str = "", password: str = "",
                     timeout: float = 4.0,
                     interface: str | None = None,
                     fallback_scan: bool = True,
                     subnet: str | None = None,
                     on_found=None) -> list[Camera]:
    """
    Discovery completa e bloccante. `on_found(camera)` viene chiamata via
    callback appena una telecamera e' pronta, cosi' la UI puo' popolarsi
    in modo incrementale.
    """
    xaddrs = ws_discover(timeout=timeout, interface=interface)
    log.info("WS-Discovery: %d device", len(xaddrs))

    if not xaddrs and fallback_scan:
        log.info("nessuna risposta multicast, scansiono la subnet")
        for host, port in scan_subnet(subnet):
            xaddrs.append(f"http://{host}:{port}/onvif/device_service")

    cameras: list[Camera] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(resolve_camera, x, user, password)
                   for x in xaddrs]
        for fut in futures:
            try:
                cam = fut.result()
            except Exception:                       # noqa: BLE001
                log.exception("errore in resolve_camera")
                continue
            if cam is None:
                continue
            cameras.append(cam)
            if on_found:
                on_found(cam)
    return cameras
