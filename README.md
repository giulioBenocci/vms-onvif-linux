# NVR Viewer

Muro video fino a 16 telecamere IP, con discovery ONVIF automatica sulla rete
locale. Python + Qt6 per l'interfaccia, libmpv per la decodifica.

Pacchettizzato come `.deb` per **Linux Mint 22.x** e Ubuntu 24.04 "noble".

## Perché Python e non Go

La parte pesante — demux RTSP, decodifica H.264/H.265, rendering — non la fa
mai il codice Python: la fa libmpv, cioè ffmpeg più un renderer GPU. Python
gestisce discovery, parsing SOAP e layout, quindi il GIL non è nel percorso dei
frame e non è un fattore limitante.

Go sarebbe la scelta giusta per un servizio headless (registrazione,
restreaming, gateway RTSP→WebRTC), dove `gortsplib` è ottimo. Per una GUI
desktop con video accelerato servirebbe CGO verso SDL/ffmpeg e finiresti a
riscrivere a mano ciò che Qt fornisce già.

## Installazione

```bash
git clone https://github.com/giulioBenocci/vms-onvif-linux.git
cd vms-onvif-linux
./install.sh
```

`install.sh` rileva da solo la distribuzione e sceglie il percorso giusto,
passando **sempre da `apt`** per tutto ciò che i repository della
distribuzione offrono:

- **Mint 22.x, Ubuntu 24.04+, Debian 12+** — `python3-pyqt6` è in repo:
  compila e installa il pacchetto `.deb` con `sudo apt install`.
- **Mint 21.x, Ubuntu 22.04 "jammy" e più vecchi** — `python3-pyqt6` non è
  impacchettato lì: installa comunque via `apt` le dipendenze di sistema
  disponibili (`python3-venv`, `libmpv`), poi crea un virtualenv utente per
  PyQt6/python-mpv (non impacchettati su questa base), un lanciatore in
  `~/.local/bin/nvr-viewer` e una voce nel menu applicazioni.

Chiede la password `sudo` quando serve installare pacchetti di sistema.
Puoi forzare un percorso con `./install.sh --deb` o `./install.sh --pip`.

Per disinstallare (rimuove pacchetto/virtualenv/lanciatore/voce di menu, ma
non tocca `~/.config/nvr-viewer/`):

```bash
./install.sh --uninstall
```

Poi lo trovi nel menu sotto Audio e video, oppure lo lanci con `nvr-viewer`.

### Installazione manuale del pacchetto .deb

Se preferisci non usare `install.sh`:

```bash
./packaging/build-deb.sh
sudo apt install ./build/nvr-viewer_1.0.0_all.deb
```

`apt` tira dentro da solo `python3-pyqt6`, `python3-mpv`, `libmpv2` e
`libxcb-cursor0` (richiesta dal plugin xcb di Qt 6.5+, senza la quale l'app
crasha all'avvio con "no Qt platform plugin could be initialized"). Il
pacchetto è `Architecture: all` e pesa poche decine di KB, perché usa i
pacchetti della distribuzione invece di includere copie proprie delle
librerie. Rimozione: `sudo apt purge nvr-viewer`.

### Compatibilità

| Distribuzione | Base | Stato |
|---|---|---|
| Linux Mint 22.x | Ubuntu 24.04 noble | dipendenze verificate, ciclo installa/rimuovi testato |
| Ubuntu 24.04 / 24.10 | noble | testato |
| Linux Mint 21.x | Ubuntu 22.04 jammy | `python3-pyqt6` **non** è in repo su jammy (verificato): `install.sh` passa da solo al percorso pip, testato end-to-end su Ubuntu 22.04 |
| Debian 12/13 | — | dovrebbe funzionare, i nomi dei pacchetti coincidono |

Su Mint 21 / jammy `install.sh` sceglie automaticamente il percorso pip
descritto più sotto invece del `.deb`.

## Configurazione

Lanciando dal menu non ci sono argomenti da riga di comando, quindi le
credenziali ONVIF vanno nel file di configurazione:

```bash
nvr-viewer --write-config      # crea ~/.config/nvr-viewer/config.json a 0600
```

```json
{
  "user": "onvif_user",
  "password": "segreta",
  "subnet": null,
  "interface": null,
  "timeout": 4.0,
  "layout": 16,
  "discovery": true,
  "cameras": [
    {"name": "Ingresso",
     "main": "rtsp://192.168.1.64:554/Streaming/Channels/101",
     "sub":  "rtsp://192.168.1.64:554/Streaming/Channels/102"},
    {"name": "Garage", "host": "192.168.1.71"}
  ]
}
```

Le voci in `cameras` si aggiungono a quelle trovate dalla discovery: con `main`
e `sub` si va diretti in RTSP, con il solo `host` la telecamera viene
interrogata via ONVIF. Gli argomenti da riga di comando, quando presenti,
hanno la precedenza sul file.

Il file contiene una password: il programma lo crea già con permessi `600` e
avvisa se lo trova leggibile da altri utenti.

### Credenziali dall'interfaccia grafica

Non serve editare `config.json` a mano: dal menu **Impostazioni → Credenziali
ONVIF…** (o `Ctrl+,`) si aprono utente, password, interfaccia locale, subnet
di fallback, timeout e l'interruttore della discovery automatica. Al salvataggio
la configurazione viene scritta su disco (stessi permessi `600`) e la
discovery riparte subito con le nuove credenziali, senza riavviare il
programma. Queste sono le credenziali *globali*, usate per la discovery e da
ogni telecamera che non ne dichiari di proprie.

### Credenziali per singola telecamera

Tasto destro su un riquadro → **Configura telecamera…** apre due modalità:

- **ONVIF**, con host/porta/utente/password propri — utile quando un device
  vuole un account ONVIF diverso da quello globale. Lascia utente/password
  vuoti per ereditare quelli globali.
- **RTSP manuale**, con gli URL principale e secondario inseriti a mano —
  serve quando la discovery non trova l'endpoint ONVIF del device (device non
  ONVIF, firmware che non risponde, o dietro un firewall che blocca solo la
  porta ONVIF ma non RTSP).

In entrambi i casi la configurazione risultante viene salvata in
`config.json` (upsert per nome/host: si riconfigura la stessa telecamera
senza duplicarla) e il riquadro passa subito al nuovo flusso.

## Uso

```bash
nvr-viewer                                   # usa il file di configurazione
nvr-viewer --user admin --password segreta   # credenziali al volo
nvr-viewer --list                            # solo discovery, stampa gli URI RTSP
nvr-viewer --subnet 192.168.1.0/24           # multicast bloccato: scansiona
nvr-viewer --no-discovery                    # solo le telecamere in config
```

`--list` è il primo comando da provare: isola i problemi di rete o di
credenziali prima di tirare in ballo l'interfaccia grafica.

Documentazione completa delle opzioni: `man nvr-viewer`.

### Comandi da tastiera e mouse

| Tasto / azione | Effetto |
|---|---|
| `1` `2` `3` `4` | layout 1 / 4 / 9 / 16 riquadri |
| `←` `→` | pagina precedente / successiva |
| doppio click | ingrandisce il riquadro e passa al flusso HD |
| tasto destro | Configura telecamera… (credenziali o RTSP di quel riquadro) |
| `Esc` | esce dallo zoom o dal fullscreen |
| `F11` | fullscreen |
| `Ctrl+,` | Impostazioni → Credenziali ONVIF (globali) |
| `Ctrl+Q` | esci |

## Ricostruire il pacchetto

```bash
chmod +x packaging/build-deb.sh
./packaging/build-deb.sh
# oppure con i tuoi dati:
VERSION=1.0.1 MAINTAINER="Mario Rossi <mario@example.com>" ./packaging/build-deb.sh
```

Il risultato finisce in `build/`. Serve solo `dpkg-deb` (pacchetto `dpkg-dev`),
niente toolchain di compilazione: il pacchetto è Python puro.

Lo script è deliberatamente un `dpkg-deb` diretto invece di `debhelper`: per un
pacchetto arch-independent di sei moduli, `debian/rules` e contorno
aggiungerebbero cerimonia senza vantaggi. Se un giorno volessi pubblicarlo su
un PPA o in Debian, allora il passaggio a `dh` diventa obbligatorio.

Il pacchetto costruito passa `lintian` senza rilievi e `desktop-file-validate`
senza avvisi.

## Installazione alternativa senza .deb

`install.sh --pip` automatizza questi passi (venv utente + lanciatore +
voce di menu). Per farlo a mano, ad esempio per sviluppo:

```bash
sudo apt install libmpv2 python3-venv   # dipendenze di sistema via apt
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt         # PyQt6 + python-mpv, non in repo su jammy
python -m nvr_viewer --list
```

## Come funziona

1. **WS-Discovery** — una `Probe` SOAP in multicast su `239.255.255.250:3702`
   con tipo `NetworkVideoTransmitter`. I device rispondono con i loro `XAddrs`.
2. **Fallback** — se il multicast non passa (VLAN, Wi-Fi con IGMP snooping,
   container in bridge), si scansiona la subnet sulle porte ONVIF più comuni.
3. **ONVIF Media** — `GetProfiles` per elencare i profili con le risoluzioni,
   `GetStreamUri` per l'URL RTSP di ciascuno. Auth con WS-Security
   UsernameToken digest, con fallback su HTTP Digest per i firmware che lo
   pretendono.
4. **Rendering** — un'istanza mpv per riquadro, agganciata a una finestra
   nativa figlia via `--wid`.

`onvif_lite.py` non dipende da `onvif-zeep`: sono ~400 righe di stdlib, ma ti
evitano una catena di dipendenze (`zeep`, `lxml`, `suds`) storicamente fragile.

`qtcompat.py` astrae PyQt6 e PySide6. Il `.deb` usa PyQt6 perché PySide6 non è
impacchettato in Ubuntu 24.04; chi installa da pip di solito ha PySide6. Nel
codice gli enum sono sempre scritti in forma qualificata
(`Qt.WidgetAttribute.WA_NativeWindow`), l'unica accettata da entrambi.

## Nota sulle prestazioni

La cosa che rende sostenibili 16 flussi non è il linguaggio: è **usare il
substream in griglia**. Il programma sceglie automaticamente il profilo a
risoluzione più bassa per i riquadri e passa al mainstream solo sullo zoom.

- 16 substream 640×360 H.264 → carico modesto, gira anche su hardware datato.
- 16 mainstream 4 MP → circa 250 Mpixel/s da decodificare: senza accelerazione
  hardware satura qualunque CPU desktop.

Se le telecamere non espongono un substream, configuralo dalla loro interfaccia
web (di solito "Stream secondario" / "Sub stream"): è la singola modifica con
più impatto.

Per l'accelerazione hardware su Mint serve il driver VA-API giusto:

```bash
sudo apt install va-driver-all vainfo   # Intel/AMD
vainfo                                  # controlla che i profili H264/HEVC ci siano
```

Altri accorgimenti già attivi in `MPV_OPTIONS` (`nvr_viewer/viewer.py`):

- `hwdec=auto-safe` — decodifica su GPU quando il driver lo consente.
- `rtsp_transport=tcp` — RTSP su UDP perde pacchetti appena la rete si carica,
  e si vede: artefatti verdi e macroblocchi.
- `profile=low-latency` + `cache=no` — buffering minimo. Latenza tipica 0,3–1 s
  contro i 3–5 s di una configurazione con cache.

## Problemi noti

**"no Qt platform plugin could be initialized" / crash immediato all'avvio.**
Manca `libxcb-cursor0`, richiesta dal plugin xcb di Qt 6.5+ (non basta
`python3-pyqt6`/`libmpv`). Il `.deb` la dichiara come dipendenza, ma con
l'installazione pip va aggiunta a mano:

```bash
sudo apt install libxcb-cursor0
```

`install.sh` la installa da solo dalla versione più recente dello script.

**Schermo nero su Wayland.** L'embedding via `--wid` è poco affidabile lì. Il
programma se ne accorge e forza XWayland da solo; Mint con Cinnamon usa
comunque X11 di default. Per forzare a mano:

```bash
QT_QPA_PLATFORM=xcb nvr-viewer
```

**La discovery non trova nulla.** Nell'ordine: alza `--timeout 8`; specifica
l'interfaccia giusta se hai più schede di rete (`--interface 192.168.1.50`);
verifica di essere nella stessa subnet delle telecamere; usa `--subnet`. Il
multicast non attraversa i router, quindi da una VLAN diversa può funzionare
solo la scansione TCP.

**Errore 401 / nessun profilo.** Molti device richiedono un utente ONVIF
dedicato, creato dal loro pannello web: le credenziali di amministrazione
dell'interfaccia non sempre valgono per ONVIF. Se anche così non risponde, il
programma ripiega su path RTSP presunti (Hikvision di default): in quel caso il
nome del riquadro riporta `(?)`. Conviene allora inserire gli URI a mano in
`config.json`.

## Da qui in poi

Estensioni naturali, in ordine di utilità: registrazione su file
(`mpv.command('start-record')` o un processo ffmpeg separato per canale),
controlli PTZ via ONVIF `ptz/wsdl`, riconnessione automatica con backoff sui
flussi caduti, salvataggio del layout tra le sessioni.
