#!/usr/bin/env bash
#
# build-deb.sh - costruisce nvr-viewer_<versione>_all.deb
#
# Il pacchetto e' Architecture: all (Python puro) e dipende solo da pacchetti
# presenti nei repository Ubuntu/Mint: niente wheel PyPI impacchettate dentro.
#
# Uso:
#   ./packaging/build-deb.sh
#   VERSION=1.0.1 MAINTAINER="Mario Rossi <mario@example.com>" ./packaging/build-deb.sh
#
set -euo pipefail

VERSION="${VERSION:-1.0.0}"
MAINTAINER="${MAINTAINER:-NVR Viewer contributors <nobody@example.com>}"
PACKAGE="nvr-viewer"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
STAGE="${BUILD}/${PACKAGE}_${VERSION}_all"

command -v dpkg-deb >/dev/null || {
    echo "manca dpkg-deb: sudo apt install dpkg-dev" >&2; exit 1; }

echo ">> pulizia"
rm -rf "${STAGE}"
mkdir -p "${STAGE}"/DEBIAN \
         "${STAGE}"/usr/bin \
         "${STAGE}"/usr/lib/python3/dist-packages/nvr_viewer \
         "${STAGE}"/usr/share/applications \
         "${STAGE}"/usr/share/icons/hicolor/scalable/apps \
         "${STAGE}"/usr/share/man/man1 \
         "${STAGE}"/usr/share/doc/"${PACKAGE}"

echo ">> copia dei moduli Python"
# I .pyc verranno generati da py3compile in postinst, non li spediamo.
install -m 644 "${ROOT}"/nvr_viewer/*.py \
    "${STAGE}"/usr/lib/python3/dist-packages/nvr_viewer/

echo ">> eseguibile"
cat > "${STAGE}/usr/bin/${PACKAGE}" <<'LAUNCHER'
#!/usr/bin/python3
import sys

from nvr_viewer.main import main

sys.exit(main())
LAUNCHER
chmod 755 "${STAGE}/usr/bin/${PACKAGE}"

echo ">> risorse desktop"
install -m 644 "${ROOT}/packaging/${PACKAGE}.desktop" \
    "${STAGE}/usr/share/applications/${PACKAGE}.desktop"
install -m 644 "${ROOT}/packaging/${PACKAGE}.svg" \
    "${STAGE}/usr/share/icons/hicolor/scalable/apps/${PACKAGE}.svg"

echo ">> documentazione"
install -m 644 "${ROOT}/packaging/copyright" \
    "${STAGE}/usr/share/doc/${PACKAGE}/copyright"
install -m 644 "${ROOT}/README.md" \
    "${STAGE}/usr/share/doc/${PACKAGE}/README.md"
install -m 644 "${ROOT}/cameras.example.json" \
    "${STAGE}/usr/share/doc/${PACKAGE}/cameras.example.json"

gzip -9nc "${ROOT}/packaging/${PACKAGE}.1" \
    > "${STAGE}/usr/share/man/man1/${PACKAGE}.1.gz"

# Il changelog compresso e' richiesto dalla policy Debian.
# Pacchetto nativo (versione senza revisione Debian): il nome atteso e'
# changelog.gz, non changelog.Debian.gz.
cat <<CHANGELOG | gzip -9nc > "${STAGE}/usr/share/doc/${PACKAGE}/changelog.gz"
${PACKAGE} (${VERSION}) unstable; urgency=medium

  * Prima versione: discovery ONVIF, muro video 16 canali, zoom su
    mainstream, paginazione.

 -- ${MAINTAINER}  $(date -R)
CHANGELOG

echo ">> metadati di controllo"
SIZE=$(du -ks "${STAGE}" | cut -f1)

cat > "${STAGE}/DEBIAN/control" <<CONTROL
Package: ${PACKAGE}
Version: ${VERSION}
Section: video
Priority: optional
Architecture: all
Maintainer: ${MAINTAINER}
Installed-Size: ${SIZE}
Depends: python3 (>= 3.10), python3-mpv, python3-pyqt6 | python3-pyside6.qtwidgets, libxcb-cursor0
Recommends: mpv
Suggests: va-driver-all | nvidia-driver-libs
Description: video wall for up to 16 ONVIF network cameras
 Discovers ONVIF cameras on the local network using WS-Discovery and shows
 their RTSP streams in a grid of up to 16 tiles. Decoding is handled by
 libmpv with hardware acceleration when the driver allows it.
 .
 Grid tiles use each camera's substream and switch to the high resolution
 profile only when zoomed, which is what makes sixteen simultaneous streams
 practical on ordinary hardware.
 .
 Features WS-Discovery with a TCP scan fallback for networks where multicast
 is filtered, WS-Security and HTTP Digest authentication, adjustable 1/4/9/16
 layouts and paging when more than sixteen cameras are present.
CONTROL

# py3compile genera i bytecode a installazione avvenuta; il resto aggiorna
# le cache del desktop. Ogni comando e' opzionale: || true ovunque.
cat > "${STAGE}/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    if command -v py3compile >/dev/null 2>&1; then
        py3compile -p nvr-viewer /usr/lib/python3/dist-packages/nvr_viewer || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
    fi
fi

exit 0
POSTINST

cat > "${STAGE}/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "upgrade" ]; then
    if command -v py3clean >/dev/null 2>&1; then
        py3clean -p nvr-viewer || true
    fi
    rm -rf /usr/lib/python3/dist-packages/nvr_viewer/__pycache__
fi

exit 0
PRERM

cat > "${STAGE}/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -qtf /usr/share/icons/hicolor || true
    fi
fi

exit 0
POSTRM

chmod 755 "${STAGE}/DEBIAN/postinst" "${STAGE}/DEBIAN/prerm" "${STAGE}/DEBIAN/postrm"

echo ">> md5sums"
( cd "${STAGE}" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
    | xargs -0 md5sum > DEBIAN/md5sums )

echo ">> dpkg-deb"
dpkg-deb --root-owner-group --build "${STAGE}" > /dev/null
DEB="${STAGE}.deb"

echo
echo "Pacchetto pronto: ${DEB}"
echo "   $(du -h "${DEB}" | cut -f1)"
echo
echo "Installazione (risolve anche le dipendenze):"
echo "   sudo apt install ${DEB}"
