#!/usr/bin/env bash
#
# install.sh - installer per NVR Viewer su Linux Mint / Ubuntu / Debian.
#
# Sceglie automaticamente il percorso giusto in base ai pacchetti disponibili
# nei repository della distribuzione:
#
#   - Mint 22.x, Ubuntu 24.04+, Debian 12+  -> python3-pyqt6 e' in repo:
#     si compila e installa il pacchetto .deb (vedi packaging/build-deb.sh).
#   - Mint 21.x, Ubuntu 22.04 "jammy" e piu' vecchi -> python3-pyqt6 non e'
#     impacchettato li': si crea un virtualenv utente con PyQt6 da pip,
#     un lanciatore in ~/.local/bin e una voce di menu in ~/.local/share.
#     Vedi README.md, sezione "Compatibilita'".
#
# Uso:
#   ./install.sh                # installa (rileva la modalita' da solo)
#   ./install.sh --deb           # forza il percorso .deb
#   ./install.sh --pip           # forza il percorso virtualenv
#   ./install.sh --uninstall     # rimuove tutto cio' che install.sh ha creato
#
set -euo pipefail

APP="nvr-viewer"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR="${HOME}/.local/share/${APP}/venv"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

log()  { echo ">> $*" >&2; }
die()  { echo "errore: $*" >&2; exit 1; }

refresh_desktop_caches() {
    command -v update-desktop-database >/dev/null && \
        update-desktop-database -q "$DESKTOP_DIR" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && \
        gtk-update-icon-cache -qtf "$(dirname "$(dirname "$ICON_DIR")")" 2>/dev/null || true
}

# --------------------------------------------------------------------------- #
uninstall() {
    log "rimozione pacchetto apt (se presente)"
    if dpkg -s "$APP" >/dev/null 2>&1; then
        sudo apt purge -y "$APP"
    fi

    log "rimozione installazione utente (se presente)"
    rm -rf "$VENV_DIR"
    rm -f "${BIN_DIR}/${APP}"
    rm -f "${DESKTOP_DIR}/${APP}.desktop"
    rm -f "${ICON_DIR}/${APP}.svg"
    refresh_desktop_caches

    log "fatto. La configurazione in ~/.config/nvr-viewer/ non e' stata toccata."
    exit 0
}

# --------------------------------------------------------------------------- #
detect_mode() {
    command -v apt >/dev/null || die "serve un sistema basato su apt (Mint/Ubuntu/Debian)"

    # shellcheck disable=SC1091
    . /etc/os-release
    local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [ -n "$codename" ] || die "impossibile determinare la versione da /etc/os-release"
    log "distribuzione rilevata: ${PRETTY_NAME:-sconosciuta} (base ${codename})"

    sudo apt update -qq >&2

    if apt-cache show python3-pyqt6 >/dev/null 2>&1; then
        echo "deb"
    else
        echo "pip"
    fi
}

# --------------------------------------------------------------------------- #
install_deb() {
    command -v dpkg-deb >/dev/null || sudo apt install -y dpkg-dev

    log "compilazione del pacchetto"
    "${ROOT}/packaging/build-deb.sh"

    local deb
    deb=$(ls -t "${ROOT}"/build/*.deb | head -1)
    log "installazione di ${deb}"
    sudo apt install -y "$deb"

    log "installato. Menu: Audio e video -> NVR Viewer, oppure: nvr-viewer"
    log "configura le credenziali ONVIF con: nvr-viewer --write-config"
    log "oppure dall'interfaccia grafica: Impostazioni -> Credenziali ONVIF..."
}

# --------------------------------------------------------------------------- #
install_pip() {
    log "installazione dipendenze di sistema (venv, libmpv, Qt xcb)"
    sudo apt install -y python3-venv || die "impossibile installare python3-venv"
    sudo apt install -y libmpv2 2>/dev/null || sudo apt install -y libmpv1 \
        || die "impossibile installare libmpv (ne' libmpv2 ne' libmpv1 in repo)"
    # Qt >= 6.5 non carica il plugin xcb senza questa libreria: senza,
    # l'app stampa "no Qt platform plugin could be initialized" e crasha
    # all'avvio, con l'unico indizio in stderr (invisibile se lanciata dal menu).
    sudo apt install -y libxcb-cursor0 \
        || die "impossibile installare libxcb-cursor0 (richiesta dal plugin xcb di Qt)"

    log "creazione virtualenv in ${VENV_DIR}"
    rm -rf "$VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    python3 -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install -r "${ROOT}/requirements.txt" -q

    log "installazione dei moduli nvr_viewer nel virtualenv"
    local site_packages
    site_packages="$("${VENV_DIR}/bin/python3" -c \
        'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    rm -rf "${site_packages:?}/nvr_viewer"
    cp -r "${ROOT}/nvr_viewer" "${site_packages}/nvr_viewer"

    log "lanciatore in ${BIN_DIR}/${APP}"
    mkdir -p "$BIN_DIR"
    cat > "${BIN_DIR}/${APP}" <<LAUNCHER
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/python3" -m nvr_viewer "\$@"
LAUNCHER
    chmod 755 "${BIN_DIR}/${APP}"

    log "voce di menu"
    mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
    sed -e "s|^Exec=.*|Exec=${BIN_DIR}/${APP}|" \
        -e "s|^TryExec=.*|TryExec=${BIN_DIR}/${APP}|" \
        "${ROOT}/packaging/${APP}.desktop" > "${DESKTOP_DIR}/${APP}.desktop"
    cp "${ROOT}/packaging/${APP}.svg" "${ICON_DIR}/${APP}.svg"
    refresh_desktop_caches

    case ":$PATH:" in
        *":${BIN_DIR}:"*) ;;
        *) log "nota: ${BIN_DIR} non e' nel PATH della shell corrente."
           log "      apri un nuovo terminale, oppure aggiungi questa riga a ~/.profile:"
           log "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac

    log "installato. Cerca 'NVR Viewer' nel menu, oppure lancia: ${BIN_DIR}/${APP}"
    log "configura le credenziali ONVIF con: ${BIN_DIR}/${APP} --write-config"
    log "oppure dall'interfaccia grafica: Impostazioni -> Credenziali ONVIF..."
}

# --------------------------------------------------------------------------- #
mode="${1:-}"
case "$mode" in
    --uninstall) uninstall ;;
    --deb)       mode="deb" ;;
    --pip)       mode="pip" ;;
    "")          mode="$(detect_mode)" ;;
    *)           die "opzione sconosciuta: $mode (usa --deb, --pip o --uninstall)" ;;
esac

log "modalita' di installazione: ${mode}"
case "$mode" in
    deb) install_deb ;;
    pip) install_pip ;;
esac
