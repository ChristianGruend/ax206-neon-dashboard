#!/usr/bin/env bash
# Rendert das Theme in ein PNG, ohne das echte AX206-Display anzufassen:
# lcd4linux laeuft dafuer mit dem VNC-Treiber statt mit DPF.
#
#   ./preview.sh [ausgabe.png]
#
# Braucht python3 mit Pillow. Der laufende Display-Dienst bleibt unberuehrt.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/preview.png}"
SETTLE="${2:-6}"
PORT=5987
PIDFILE="${TMPDIR:-/tmp}/ax206-preview.pid"
CONF="${TMPDIR:-/tmp}/ax206-preview.conf"
LOG="${TMPDIR:-/tmp}/ax206-preview.log"

# Vorherige Vorschau beenden - ueber PID-Datei, damit kein pkill-Muster
# versehentlich die aufrufende Shell erwischt.
if [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE")
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        kill "$OLD" 2>/dev/null
        for _ in $(seq 1 10); do
            kill -0 "$OLD" 2>/dev/null || break
            sleep 0.3
        done
        kill -9 "$OLD" 2>/dev/null
    fi
    rm -f "$PIDFILE"
fi

python3 "$HERE/build_theme.py" "$CONF" --vnc || exit 1

# Ohne laufenden Collector bleiben Kern-Last, Ampeln und Wetter leer
if [ ! -r /run/lcd4linux/metrics ]; then
    echo "Hinweis: /run/lcd4linux/metrics fehlt - Collector laeuft nicht," >&2
    echo "         die davon gespeisten Felder bleiben in der Vorschau leer." >&2
fi

# cwd muss /usr/share/lcd4linux sein, sonst greifen die relativen Font-Pfade nicht
cd /usr/share/lcd4linux || exit 1
setsid lcd4linux -F -f "$CONF" > "$LOG" 2>&1 &
echo $! > "$PIDFILE"

for _ in $(seq 1 40); do
    ss -tln 2>/dev/null | grep -q ":$PORT " && break
    sleep 0.25
done

python3 "$HERE/vncshot.py" 127.0.0.1 "$PORT" "$OUT" "$SETTLE"
RC=$?

if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi

echo "--- Meldungen von lcd4linux ---"
grep -iE "error|bad |failed|cannot|no such|unknown" "$LOG" \
    | grep -v "skip next" | sort -u | head -20 || true

exit $RC
