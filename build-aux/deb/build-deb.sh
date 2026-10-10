#!/usr/bin/env bash
# Build an architecture-independent .deb (Ubuntu 26.04+ / Debian with GNOME 50+).
set -euo pipefail

cd "$(dirname "$0")/../.."
PACKAGE=dotdeck
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' src/config.py)"
STAGE="build/deb/${PACKAGE}_${VERSION}_all"
OUTPUT="dist/${PACKAGE}_${VERSION}_all.deb"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" dist

make --no-print-directory install DESTDIR="$PWD/$STAGE" PREFIX=/usr PYTHON=/usr/bin/python3

DOC="$STAGE/usr/share/doc/$PACKAGE"
install -d "$DOC"
install -m644 build-aux/deb/copyright "$DOC/copyright"
{
    echo "$PACKAGE ($VERSION) unstable; urgency=medium"
    echo
    echo "  * Release $VERSION. See https://github.com/EuVinicios/Dotdeck/releases"
    echo
    echo " -- EuVinicios <EuVinicios@users.noreply.github.com>  $(date -R)"
} | gzip -9n > "$DOC/changelog.gz"

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/usr" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/dotdeck"

INSTALLED_SIZE="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PACKAGE
Version: $VERSION
Architecture: all
Maintainer: EuVinicios <EuVinicios@users.noreply.github.com>
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.11), python3-gi (>= 3.50), python3-gi-cairo, python3-cairo, gir1.2-glib-2.0, gir1.2-gtk-4.0 (>= 4.20), gir1.2-adw-1 (>= 1.9), gir1.2-webkit-6.0, gir1.2-soup-3.0, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gir1.2-gst-plugins-bad-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-nice
Recommends: gir1.2-secret-1, gstreamer1.0-pipewire | gstreamer1.0-pulseaudio, xdg-desktop-portal-gnome | xdg-desktop-portal-kde
Section: net
Priority: optional
Homepage: https://euvinicios.github.io/Dotdeck/
Description: native desktop client for OpenDots AI coworkers
 Dotdeck connects to an OpenDots server (the self-hosted workspace of
 always-on AI coworkers called Dots) on this computer, the local network,
 Tailscale or the cloud, and brings it to the desktop: guided setup, several
 servers, owner tokens in the keyring, voice calls with a native WebRTC
 engine, notifications for finished or failed tasks, a tray icon,
 background mode and global shortcuts.
 .
 Dotdeck is an independent project, not affiliated with CopilotKit.
CONTROL

dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUTPUT" >/dev/null
echo "Pacote gerado: $OUTPUT"
dpkg-deb --info "$OUTPUT" | sed -n '/Package:/,$p' | head -12
