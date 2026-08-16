#!/usr/bin/env bash
# Installiert das AX206-Neon-Dashboard auf dem Displayframe.
# Idempotent: mehrfach ausfuehrbar, sichert die bestehende Config vorher weg.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG="/etc/lcd4linux/ax206-dashboard.toml"

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit sudo ausfuehren." >&2
    exit 1
fi

if [ ! -d /etc/lcd4linux ]; then
    echo "/etc/lcd4linux fehlt - laeuft lcd4linux-ax206 auf dieser Maschine?" >&2
    exit 1
fi

if [ ! -f "$HERE/config.toml" ]; then
    echo "config.toml fehlt - config.example.toml kopieren und anpassen." >&2
    exit 1
fi

# Die Konfiguration zuerst: Collector und Generator lesen beide von dort, und
# der Generator laeuft weiter unten schon gegen den installierten Stand.
install -m 644 "$HERE/config.toml" "$CONFIG"

# Interface fuer die Unit aus derselben Quelle, damit es nicht zwei Wahrheiten
# gibt. IFACE= im Aufruf sticht, sonst gilt der Wert aus der TOML.
IFACE="${IFACE:-$(sed -n 's/^ *interface *= *"\([^"]*\)".*/\1/p' "$CONFIG" | head -1)}"
IFACE="${IFACE:-eth0}"

STAMP=$(date +%Y%m%d-%H%M%S)
if [ -f /etc/lcd4linux/lcd4linux.conf ]; then
    cp /etc/lcd4linux/lcd4linux.conf "/etc/lcd4linux/lcd4linux.conf.bak-$STAMP"
    echo "Bestehende Config gesichert: lcd4linux.conf.bak-$STAMP"
fi

# Config erzeugen und einspielen
python3 "$HERE/build_theme.py" /tmp/ax206-dashboard.conf
install -m 600 /tmp/ax206-dashboard.conf /etc/lcd4linux/lcd4linux.conf
rm -f /tmp/ax206-dashboard.conf

install -m 755 "$HERE/ax206-dash-collect.py" /usr/local/bin/ax206-dash-collect.py
# render_gauges.py muss neben dem Collector liegen - der laedt es beim Start,
# weil /run als tmpfs nach jedem Boot leer ist.
install -m 755 "$HERE/render_gauges.py" /usr/local/bin/render_gauges.py
install -m 755 "$HERE/set_wallpaper.py" /usr/local/bin/ax206-wallpaper
install -m 644 "$HERE/build_theme.py" /etc/lcd4linux/build_theme.py
install -m 644 "$HERE/config.example.toml" /etc/lcd4linux/ax206-dashboard.example.toml
install -m 644 "$HERE/README.md" /etc/lcd4linux/README-ax206-dashboard.md

# Interface in die Unit einsetzen - in der Datei steht der Platzhalter eth0
sed "s|ax206-dash-collect.py eth0|ax206-dash-collect.py $IFACE|" \
    "$HERE/ax206-dash-collect.service" > /etc/systemd/system/ax206-dash-collect.service
chmod 644 /etc/systemd/system/ax206-dash-collect.service

# lcd4linux soll den Collector mitziehen, ohne die Paket-Unit zu aendern
mkdir -p /etc/systemd/system/lcd4linux-ax206.service.d
cat > /etc/systemd/system/lcd4linux-ax206.service.d/10-collector.conf <<'DROPIN'
[Unit]
# Kern-Last, Dienste-Ampeln, GPU-Werte und Wetter kommen aus
# /run/lcd4linux/metrics - geschrieben vom Collector.
Wants=ax206-dash-collect.service
After=ax206-dash-collect.service
DROPIN

systemctl daemon-reload
systemctl enable ax206-dash-collect.service
# restart, nicht "enable --now": bei einem bereits laufenden Dienst wuerde
# --now nichts tun und der alte Collector-Code bliebe aktiv.
systemctl restart ax206-dash-collect.service
sleep 3
systemctl restart lcd4linux-ax206.service

echo
echo "Fertig. Status:"
systemctl is-active ax206-dash-collect.service lcd4linux-ax206.service
