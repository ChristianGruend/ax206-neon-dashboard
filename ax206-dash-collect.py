#!/usr/bin/env python3
"""Sammelt die Werte fuer das AX206-Dashboard, die lcd4linux nicht selbst
guenstig liefern kann, und legt sie zeilenweise in /run/lcd4linux/metrics ab.

Warum ueberhaupt: lcd4linux kennt kein Per-Core-CPU und startet fuer jedes
exec() einen eigenen Worker-Prozess. Zwoelf Kerne plus Dienste-Checks waeren
ein gutes Dutzend Dauerprozesse. Dieser Collector laeuft stattdessen einmal,
haelt den /proc/stat-Vorzustand fuer die Delta-Rechnung im Speicher und
staffelt teure Abfragen (Wetter, Backup, Ping) ueber eigene Intervalle.

lcd4linux liest die Werte mit file::readline('/run/lcd4linux/metrics', N).
Die Reihenfolge in METRICS ist damit ein Vertrag mit build_theme.py -
Zeilen nur anhaengen, nie dazwischenschieben.
"""
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib

OUT_DIR = "/run/lcd4linux"
OUT_FILE = os.path.join(OUT_DIR, "metrics")
DEBUG_FILE = os.path.join(OUT_DIR, "metrics.debug")
GAUGE_DIR = os.path.join(OUT_DIR, "gauge")
INTERVAL = 2.0

# Alles Maschinenspezifische steht in der TOML - Schnittstelle, Platten,
# Sensoren, Ort. build_theme.py liest dieselbe Datei ueber dieses Modul, damit
# ein Wert nur an einer Stelle existiert.
CONFIG_NAME = "config.toml"
CONFIG_PATHS = (
    os.environ.get("AX206_CONFIG"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME),
    "/etc/lcd4linux/ax206-dashboard.toml",
)


def load_config():
    for path in CONFIG_PATHS:
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                cfg = tomllib.load(fh)
            cfg["_path"] = path
            return cfg
    raise SystemExit(
        f"Keine Konfiguration gefunden. Gesucht in: "
        f"{', '.join(p for p in CONFIG_PATHS if p)}. "
        f"config.example.toml nach {CONFIG_NAME} kopieren und anpassen."
    )


CFG = load_config()
_SYS = CFG.get("system", {})

IFACE_DEFAULT = _SYS.get("interface") or "eth0"
# 0 heisst "so viele Ringe wie Threads" - auf fremden Maschinen die einzige
# Angabe, die man sonst zwingend von Hand nachziehen muesste.
N_CORES = _SYS.get("cpu_threads") or os.cpu_count() or 4
MOUNT_OWNER = _SYS.get("mount_owner") or ""
BACKUP_DIR = _SYS.get("backup_dir") or ""
BOARD_SENSOR = _SYS.get("board_sensor") or ""
WEATHER = CFG.get("weather", {}).get("location") or ""

_DISPLAY = CFG.get("display", {})
# Kantenlaenge der Ring-PNGs. build_theme.py liest den Wert von hier, damit
# gerenderte Groesse und Layout-Box nicht auseinanderlaufen koennen.
GAUGE_SIZE = _DISPLAY.get("gauge_size", 44)
N_TOP = _DISPLAY.get("top_processes", 5)
N_SCREENS = _DISPLAY.get("screens", 3)

# Feste Zahl an Plattenplaetzen, damit der Zeilenvertrag stabil bleibt: ein
# nicht eingehaengter Platz zeigt 0 % und "-", statt die Zeilen zu verschieben.
# Reihenfolge = Reihenfolge der Ringe auf dem Display.
DISKS = tuple(
    (d["label"], os.path.expanduser(d["path"])) for d in CFG.get("disks", [])
)

# Der Container-Block ist optional - ohne [stack] faellt die Sektion weg.
STACK = CFG.get("stack", {})
STACK_LAMPS = tuple(STACK.get("lamps", []))
SERVICES = tuple(CFG.get("services", []))

# Zeilenvertrag: (key, intervall_sekunden). Position = Zeilennummer in metrics.
METRICS = (
    [(f"core{i}", 0) for i in range(N_CORES)]
    + [(f"slamp{i}", 10) for i in range(len(STACK_LAMPS))]
    + [(f"lamp{i}", 10) for i in range(len(SERVICES))]
    + [
        ("weather", 900),
        ("toproc", 5),
        ("cpu_fan", 4),
        ("gpu_fan", 4),
        ("mb_temp", 4),
        ("gpu_watt", 3),
        ("docker_count", 15),
        ("backup_age", 300),
        ("cpu_temp", 4),
        ("gpu_clock", 3),
        ("updates", 1800),
        ("net_peak", 0),
        # Ab hier nur angehaengt - die GPU-Pfade tragen Globs (card*/hwmon*),
        # die lcd4linux mit file::readline nicht aufloesen kann.
        ("gpu_pct", 2),
        ("gpu_temp", 3),
        ("vram_used", 3),
        ("vram_total", 60),
        ("vram_pct", 3),
        # Ab hier nur angehaengt - Platten-I/O (Delta aus /proc/diskstats) und
        # je Plattenplatz Fuellstand und Groesse.
        ("disk_read", 0),
        ("disk_write", 0),
    ]
    + [t for i in range(len(DISKS)) for t in ((f"dsk{i}_pct", 20),
                                              (f"dsk{i}_txt", 20))]
    # Groesste Speicherverbraucher (Text je Zeile) und die Bildschirme.
    + [(f"rtop{i}", 5) for i in range(N_TOP)]
    + [(f"screen{i}", 30) for i in range(N_SCREENS)]
    # Ab hier nur angehaengt - uebertragene Gesamtmenge und der Container-Block.
    + [
        ("net_rx_total", 20),
        ("net_tx_total", 20),
        ("stack_up", 30),
        ("stack_ram", 15),
    ]
)

STACK_CONTAINER = STACK.get("container", "")


def sh(cmd, timeout=8):
    """Shell-Kommando ausfuehren, Ausgabe als eine Zeile - Fehler werden zu ''."""
    try:
        out = subprocess.run(
            ["/bin/sh", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout.strip()
        return out.replace("\n", " ")[:40]
    except Exception:
        return ""


def read_first(path_glob):
    """Erste Datei eines Globs lesen; leerer String, wenn nichts passt."""
    import glob

    for path in sorted(glob.glob(path_glob)):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            continue
    return ""


def port_open(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return 100 if s.connect_ex((host, port)) == 0 else 0


def hwmon_path(driver, sensor):
    """hwmonN-Nummern wechseln beim Boot - deshalb ueber den Treibernamen suchen."""
    import glob

    for name_file in glob.glob("/sys/class/hwmon/*/name"):
        try:
            with open(name_file) as fh:
                if fh.read().strip() == driver:
                    return os.path.join(os.path.dirname(name_file), sensor)
        except OSError:
            continue
    return None


def read_hwmon(driver, sensor, divisor=1, digits=0):
    path = hwmon_path(driver, sensor)
    if not path:
        return ""
    try:
        with open(path) as fh:
            value = float(fh.read().strip()) / divisor
        return f"{value:.{digits}f}"
    except (OSError, ValueError):
        return ""


class CoreSampler:
    """Per-Core-Auslastung aus /proc/stat, als Delta zwischen zwei Abrufen."""

    def __init__(self):
        self.prev = {}

    def sample(self):
        busy_pct = {}
        try:
            with open("/proc/stat") as fh:
                lines = fh.readlines()
        except OSError:
            return busy_pct
        for line in lines:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            name = parts[0]
            try:
                values = [int(v) for v in parts[1:11]]
            except ValueError:
                continue
            total = sum(values)
            idle = values[3] + values[4]          # idle + iowait
            prev_total, prev_idle = self.prev.get(name, (0, 0))
            d_total = total - prev_total
            d_idle = idle - prev_idle
            self.prev[name] = (total, idle)
            if d_total > 0:
                busy_pct[name] = max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))
            else:
                busy_pct[name] = 0.0
        return busy_pct


class NetPeak:
    """Groesster Durchsatz (Rx+Tx) der letzten Minute in MB/s - fuer die Balkenskala."""

    def __init__(self, iface):
        self.iface = iface
        self.prev = None
        self.history = []

    def sample(self):
        try:
            with open(f"/sys/class/net/{self.iface}/statistics/rx_bytes") as fh:
                rx = int(fh.read())
            with open(f"/sys/class/net/{self.iface}/statistics/tx_bytes") as fh:
                tx = int(fh.read())
        except OSError:
            return 0.0
        now = time.time()
        if self.prev is None:
            self.prev = (rx, tx, now)
            return 0.0
        prev_rx, prev_tx, prev_t = self.prev
        dt = max(0.001, now - prev_t)
        rate = ((rx - prev_rx) + (tx - prev_tx)) / dt / 1048576.0
        self.prev = (rx, tx, now)
        self.history.append((now, rate))
        self.history = [(t, r) for t, r in self.history if now - t <= 60]
        return max(r for _, r in self.history) if self.history else 0.0


class DiskIO:
    """Lese-/Schreibdurchsatz aller physischen Platten in MB/s.

    /proc/diskstats zaehlt Sektoren (512 B) seit Boot, also Delta ueber die
    Zeit. Partitionen tauchen dort ebenfalls auf und wuerden doppelt zaehlen -
    deshalb nur die ganzen Geraete.
    """

    DEVICE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+)$")
    SECTOR = 512

    def __init__(self):
        self.prev = None

    def _totals(self):
        read = write = 0
        try:
            with open("/proc/diskstats") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10 or not self.DEVICE.match(parts[2]):
                        continue
                    read += int(parts[5])
                    write += int(parts[9])
        except (OSError, ValueError):
            return None
        return read, write

    def sample(self):
        totals = self._totals()
        if totals is None:
            return 0.0, 0.0
        now = time.time()
        if self.prev is None:
            self.prev = (*totals, now)
            return 0.0, 0.0
        prev_r, prev_w, prev_t = self.prev
        dt = max(0.001, now - prev_t)
        self.prev = (*totals, now)
        scale = self.SECTOR / 1048576.0 / dt
        return (max(0.0, (totals[0] - prev_r) * scale),
                max(0.0, (totals[1] - prev_w) * scale))


def mounted(path):
    """Haengt dort wirklich etwas?

    Wichtig fuer die Cloud-Platze: liegt ~/OneDrive nur als leeres Verzeichnis
    herum, wuerde statvfs die Werte des Wurzeldateisystems liefern und einen
    Fuellstand melden, den es gar nicht gibt. /proc/mounts ist auch dann
    lesbar, wenn der Mount selbst fuer root gesperrt ist.
    """
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 1 and parts[1].replace("\\040", " ") == path:
                    return True
    except OSError:
        pass
    return False


def human_size(num_bytes):
    for unit, limit in (("T", 1099511627776), ("G", 1073741824), ("M", 1048576)):
        if num_bytes >= limit:
            value = num_bytes / limit
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    return f"{num_bytes / 1024:.0f}K"


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

_cache = {}


def cached(key, max_age, fn):
    """Ergebnis kurz halten - manche Werte braucht mehr als ein Abnehmer."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < max_age:
        return hit[1]
    try:
        value = fn()
    except Exception:                     # noqa: BLE001 - lieber alter Wert
        return hit[1] if hit else None
    _cache[key] = (now, value)
    return value


def ram_total_kb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


# Pfadteile, die keinen Programmnamen darstellen - Ablageorte und
# Versionsverzeichnisse.
_PATH_NOISE = {"bin", "sbin", "lib", "lib64", "libexec", "usr", "local",
               "share", "opt", "srv", "home", "versions", "current", "app",
               "dist", "build", "target", "node_modules", ".local"}


def name_from_path(pid):
    """Programmname fuer Prozesse, deren comm keine Buchstaben enthaelt.

    Claude Code etwa liegt unter .../claude/versions/2.1.233, und genau diese
    Versionsnummer landet in comm - als Anzeige unbrauchbar. Aus dem Pfad
    laesst sich der eigentliche Name gewinnen: das letzte Segment, das
    Buchstaben hat und kein blosser Ablageort ist.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv0 = fh.read().split(b"\0")[0].decode("utf-8", "replace")
    except (OSError, IndexError):
        return None
    if not argv0:
        return None

    parts = argv0.split("/")
    # Erst der Aufrufname selbst: wird das Programm ohne Pfad gestartet
    # ("claude bg-spare"), steckt der Name schon dort.
    for part in [parts[-1]] + list(reversed(parts[:-1])):
        if part and part not in _PATH_NOISE and any(c.isalpha() for c in part):
            return part
    return None


def ram_top(count=N_TOP):
    """Groesste Speicherverbraucher als [(name, prozent), ...], absteigend.

    Nach Prozessnamen summiert: Browser laufen als Dutzende Prozesse und
    wuerden die Liste sonst allein fuellen, obwohl sie eine Anwendung sind.
    Gelesen wird direkt aus /proc - ein ps-Unterprozess alle paar Sekunden
    waere fuer dieselbe Information zu teuer.
    """
    total_kb = ram_total_kb()
    if not total_kb:
        return []

    by_name = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/statm") as fh:
                resident = int(fh.read().split()[1])
            with open(f"/proc/{entry}/comm") as fh:
                name = fh.read().strip()
        except (OSError, ValueError, IndexError):
            continue                      # Prozess ist zwischendrin beendet
        if not resident:
            continue
        if not any(c.isalpha() for c in name):
            name = name_from_path(entry) or name
        # comm fasst 15 Zeichen und traegt manchmal ein Argument mit
        # ("claude bg-spare"). Am ersten Leerzeichen trennen, sonst steht
        # dieselbe Anwendung mehrfach in der Liste.
        name = name.split()[0] if name.split() else name
        by_name[name] = by_name.get(name, 0) + resident * PAGE_SIZE

    ranked = sorted(by_name.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, 100.0 * size / (total_kb * 1024.0))
            for name, size in ranked[:count]]


def x_authority():
    """Pfad zum X-Cookie der angemeldeten Sitzung, oder None.

    Die Sitzung laeuft unter Wayland; xrandr spricht mit XWayland, und dessen
    Cookie heisst .mutter-Xwaylandauth.<zufall> - der Suffix wechselt bei jeder
    Anmeldung, der Pfad darf also nicht festgeschrieben werden. Die beiden
    anderen Kandidaten decken reine X11-Sitzungen ab.
    """
    import glob
    import pwd

    try:
        uid = pwd.getpwnam(MOUNT_OWNER).pw_uid
    except KeyError:
        return None

    candidates = (glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*")
                  + [f"/run/user/{uid}/gdm/Xauthority",
                     os.path.expanduser(f"~{MOUNT_OWNER}/.Xauthority")])
    existing = [p for p in candidates if os.path.exists(p)]
    if not existing:
        return None
    return max(existing, key=os.path.getmtime)


def screens():
    """Angeschlossene Bildschirme als 'NAME BREITExHOEHE HZ'.

    Der Collector laeuft als root und hat damit keine X-Sitzung; xrandr wird
    deshalb als der angemeldete Benutzer und mit dessen Cookie aufgerufen.
    """
    auth = x_authority()
    if not auth:
        return []
    out = subprocess.run(
        ["/bin/sh", "-c",
         f"runuser -u {MOUNT_OWNER} -- env DISPLAY=:0 "
         f"XAUTHORITY={shlex.quote(auth)} xrandr --query 2>/dev/null"],
        capture_output=True, text=True, timeout=10,
    ).stdout

    found = []
    name = geometry = None
    for line in out.splitlines():
        if " connected" in line:
            name = line.split()[0]
            # Die Aufloesung aus der Geometrie nehmen, nicht aus der Modusliste:
            # bei einem gedrehten Bildschirm steht dort die Panel-Aufloesung
            # (1920x1080), sichtbar ist aber die gedrehte (1080x1920).
            match = re.search(r"\b(\d+x\d+)\+\d+\+\d+", line)
            geometry = match.group(1) if match else None
        elif name and geometry and line.startswith("   ") and "*" in line:
            # Die aktive Zeile traegt den Stern: "   2560x1440    143.83*+"
            rate = line.split()[1].rstrip("*+")
            try:
                found.append(f"{name} {geometry} {float(rate):.0f}")
            except ValueError:
                found.append(f"{name} {geometry}")
            name = geometry = None
    return found


_disk_cache = {}


def disk_usage(path, max_age=10.0):
    """(Fuellstand in Prozent, Gesamtgroesse als Text) fuer einen Mountpunkt.

    Kurz zwischengespeichert, weil Fuellstand und Groesse als zwei getrennte
    Metriken abgeholt werden - sonst liefe fuer jede Cloud-Platte zweimal ein
    df-Unterprozess.
    """
    now = time.time()
    hit = _disk_cache.get(path)
    if hit and now - hit[0] < max_age:
        return hit[1]

    result = _disk_usage_uncached(path)
    _disk_cache[path] = (now, result)
    return result


def _disk_usage_uncached(path):
    if not mounted(path):
        return "0", "-"

    total = used = None
    try:
        st = os.statvfs(path)
        if st.f_blocks:
            total = st.f_blocks * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        pass

    if not total and MOUNT_OWNER:
        # rclone & Co. haengen ohne allow_other ein. Fuer root ist der Mount
        # dann gesperrt - und zwar lautlos: statvfs liefert dort keinen Fehler,
        # sondern f_blocks = 0. Deshalb haengt der Umweg ueber den Eigentuemer
        # an der Bedingung, nicht am Ausnahmezweig.
        out = sh(
            f"runuser -u {MOUNT_OWNER} -- df -B1 --output=size,used "
            f"{shlex.quote(path)} 2>/dev/null | tail -1",
            8,
        )
        fields = out.split()
        if len(fields) == 2 and all(f.isdigit() for f in fields):
            total, used = int(fields[0]), int(fields[1])

    if not total:
        return "0", "-"
    return f"{100.0 * used / total:.0f}", human_size(total)


def container_uptime(name):
    """Laufzeit eines Containers, kurz: '50m', '5d', '3h'."""
    status = sh(f"docker ps --filter name=^{name}$ --format '{{{{.Status}}}}'", 6)
    if not status.startswith("Up"):
        return "-"
    match = re.search(r"Up\s+(?:About\s+)?(\d+)\s*(second|minute|hour|day|week)",
                      status)
    if not match:
        return "UP"
    value, unit = int(match.group(1)), match.group(2)
    return f"{value}{ {'second': 's', 'minute': 'm', 'hour': 'h', 'day': 'd', 'week': 'w'}[unit] }"


def container_ram(name):
    """Speicher eines Containers in GB, aus dessen cgroup.

    docker stats waere der naheliegende Weg, braucht aber rund eine Sekunde
    und wuerde die Sammelschleife jedes Mal anhalten. Die Container-ID wird
    deshalb einmal pro Minute geholt, der Wert selbst kommt danach aus einer
    Datei.
    """
    import glob

    cid = cached(f"cid_{name}", 60.0,
                 lambda: sh(f"docker ps -q --filter name=^{name}$", 6))
    if not cid:
        return "-"
    for path in glob.glob(f"/sys/fs/cgroup/system.slice/docker-{cid}*.scope/memory.current"):
        try:
            with open(path) as fh:
                return f"{int(fh.read().strip()) / 1073741824:.1f}G"
        except (OSError, ValueError):
            break
    return "-"


def net_total(iface, direction):
    """Seit dem Systemstart uebertragene Menge, als Text mit Einheit."""
    try:
        with open(f"/sys/class/net/{iface}/statistics/{direction}_bytes") as fh:
            return human_size(int(fh.read().strip()))
    except (OSError, ValueError):
        return "-"


def backup_age():
    """Alter der juengsten Sicherung in Stunden, oder '-' wenn keine da ist."""
    if not BACKUP_DIR:
        return "-"
    backup_dir = os.path.expanduser(BACKUP_DIR)
    try:
        entries = [
            os.path.join(backup_dir, f)
            for f in os.listdir(backup_dir)
            if f.endswith((".tar.gz", ".tgz", ".tar.zst"))
        ]
    except OSError:
        return "-"
    if not entries:
        return "-"
    newest = max(os.path.getmtime(p) for p in entries)
    hours = (time.time() - newest) / 3600.0
    return f"{hours:.0f}H" if hours < 100 else "ALT"


def lamp_value(spec):
    """Ampelwert: 100 = laeuft, 0 = aus. Vier Pruefarten, siehe config.example."""
    kind = spec.get("kind", "port")
    if kind == "systemd":
        unit = shlex.quote(spec.get("unit", ""))
        return "100" if sh(f"systemctl is-active {unit}", 4) == "active" else "0"
    if kind == "netdev":
        return "100" if os.path.exists(
            f"/sys/class/net/{spec.get('device', '')}") else "0"
    if kind == "ping":
        host = shlex.quote(spec.get("host", "1.1.1.1"))
        return "100" if sh(
            f"ping -c1 -W1 {host} >/dev/null 2>&1 && echo y", 5) == "y" else "0"
    # Mehrere Ports heisst "einer reicht" - etwa wenn ein Dienst nach einem
    # Umzug auf dem alten wie dem neuen Port erreichbar sein darf.
    ports = spec.get("ports") or ([spec["port"]] if "port" in spec else [])
    return str(max((port_open(p) for p in ports), default=0))


def collect(key, state):
    if key.startswith("core"):
        idx = int(key[4:])
        return f"{state['cores'].get(f'cpu{idx}', 0.0):.0f}"
    if key.startswith("slamp"):
        return lamp_value(STACK_LAMPS[int(key[5:])])
    if key.startswith("lamp"):
        return lamp_value(SERVICES[int(key[4:])])
    if key == "weather":
        if not WEATHER:
            return "-"
        query = shlex.quote(f"wttr.in/{WEATHER}?format=%t+%C")
        return sh(f"curl -s --max-time 8 {query}", 12)[:15] or "-"
    if key == "toproc":
        # ps und der Collector selbst fliegen raus - sonst misst die Abfrage
        # regelmaessig sich selbst und meldet "ps 100%".
        return sh(
            "ps -eo comm,pcpu --sort=-pcpu --no-headers "
            "| grep -vE '^(ps|awk|grep|sh|ax206-dash-col)\\b' | head -1 "
            "| awk '{printf \"%s %.0f%%\", substr($1,1,11), $2}'",
            5,
        ) or "-"
    if key == "cpu_fan":
        return read_hwmon(BOARD_SENSOR, "fan1_input") or "0"
    if key == "gpu_fan":
        return read_first("/sys/class/drm/card*/device/hwmon/hwmon*/fan1_input") or "0"
    if key == "mb_temp":
        return read_hwmon(BOARD_SENSOR, "temp3_input", 1000) or "0"
    if key == "cpu_temp":
        return read_hwmon(BOARD_SENSOR, "temp1_input", 1000) or "0"
    if key == "gpu_watt":
        raw = read_first("/sys/class/drm/card*/device/hwmon/hwmon*/power1_average")
        return f"{int(raw) / 1000000:.0f}" if raw.isdigit() else "0"
    if key == "gpu_clock":
        raw = read_first("/sys/class/drm/card*/device/hwmon/hwmon*/freq1_input")
        return f"{int(raw) / 1000000:.0f}" if raw.isdigit() else "0"
    if key == "docker_count":
        return sh("docker ps -q 2>/dev/null | wc -l", 6) or "0"
    if key == "backup_age":
        return backup_age()
    if key == "updates":
        return sh(
            "apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null "
            "| grep -c '^Inst ' || :",
            25,
        ) or "0"
    if key == "net_peak":
        return f"{state['net_peak']:.1f}"
    if key == "gpu_pct":
        return read_first("/sys/class/drm/card*/device/gpu_busy_percent") or "0"
    if key == "gpu_temp":
        raw = read_first("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input")
        return f"{int(raw) / 1000:.0f}" if raw.isdigit() else "0"
    if key == "vram_used":
        raw = read_first("/sys/class/drm/card*/device/mem_info_vram_used")
        return f"{int(raw) / 1073741824:.1f}" if raw.isdigit() else "0"
    if key == "vram_total":
        raw = read_first("/sys/class/drm/card*/device/mem_info_vram_total")
        return f"{int(raw) / 1073741824:.1f}" if raw.isdigit() else "0"
    if key == "vram_pct":
        used = read_first("/sys/class/drm/card*/device/mem_info_vram_used")
        total = read_first("/sys/class/drm/card*/device/mem_info_vram_total")
        if used.isdigit() and total.isdigit() and int(total) > 0:
            return f"{100.0 * int(used) / int(total):.0f}"
        return "0"
    if key == "disk_read":
        return f"{state['disk_io'][0]:.1f}"
    if key == "disk_write":
        return f"{state['disk_io'][1]:.1f}"
    if key.startswith("dsk"):
        index, _, field = key[3:].partition("_")
        pct, size = disk_usage(DISKS[int(index)][1])
        return pct if field == "pct" else size
    if key.startswith("rtop"):
        top = state["ram_top"]
        index = int(key[4:])
        if index >= len(top):
            return "-"
        name, pct = top[index]
        return f"{name[:11]} {pct:.1f}%"
    if key.startswith("screen"):
        found = state["screens"]
        index = int(key[6:])
        return found[index] if index < len(found) else "-"
    if key == "net_rx_total":
        return net_total(state["iface"], "rx")
    if key == "net_tx_total":
        return net_total(state["iface"], "tx")
    if key == "stack_up":
        return container_uptime(STACK_CONTAINER) if STACK_CONTAINER else "-"
    if key == "stack_ram":
        return container_ram(STACK_CONTAINER) if STACK_CONTAINER else "-"
    return ""


RAM_RING = os.path.join(GAUGE_DIR, "ramtop.png")
REST_COLOR = "2a4a6a"          # belegt, aber nicht unter den ersten fuenf


def _render_gauges_module():
    """render_gauges.py liegt neben dem Collector - einmal laden, dann halten."""
    module = _cache.get("render_module")
    if module is not None:
        return module[1]
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "render_gauges.py")
    spec = importlib.util.spec_from_file_location("render_gauges", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _cache["render_module"] = (time.time(), module)
    return module


def render_ram_ring(top):
    """Den Mehrsegment-Ring der Top-Speicherfresser neu zeichnen.

    Der laesst sich nicht wie die Prozent-Ringe vorrendern - sein Aussehen
    haengt an fuenf Werten zugleich. Also entsteht er hier zur Laufzeit; das
    Image-Widget liest immer dieselbe Datei mit reload 1.
    """
    if not top:
        return
    try:
        module = _render_gauges_module()
        used = 0.0
        try:
            with open("/proc/meminfo") as fh:
                info = {}
                for line in fh:
                    field, _, rest = line.partition(":")
                    info[field] = int(rest.split()[0])
            used = 100.0 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
            pass

        shares = [pct for _name, pct in top]
        # Was belegt ist, aber nicht zu den ersten fuenf gehoert, bekommt einen
        # eigenen gedaempften Abschnitt - sonst suggeriert der Ring, die fuenf
        # seien der gesamte belegte Speicher.
        rest = max(0.0, used - sum(shares))
        image = module.render_segments(
            shares + [rest], GAUGE_SIZE,
            colors=tuple(module.TOP_COLORS) + (REST_COLOR,),
            total_text=f"{used:.0f}",
        )
        tmp = RAM_RING + ".tmp"
        # Format explizit: PIL leitet es sonst aus der Endung ab, und die ist
        # hier .tmp - erst os.replace macht daraus atomar die echte Datei.
        image.save(tmp, format="PNG")
        os.chmod(tmp, 0o644)
        os.replace(tmp, RAM_RING)
    except Exception as exc:                       # noqa: BLE001
        print(f"RAM-Ring konnte nicht gezeichnet werden: {exc}", file=sys.stderr)


def ensure_gauges():
    """Die Kern-Ringe liegen unter /run - also nach jedem Boot neu erzeugen.

    Der Satz ist statisch (ein PNG je Prozentwert), das Rendern kostet nur
    beim Start etwas Zeit. Faellt es aus, bleiben die Ringe leer, alles andere
    laeuft weiter - deshalb kein harter Abbruch.
    """
    try:
        import importlib.util

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "render_gauges.py")
        spec = importlib.util.spec_from_file_location("render_gauges", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Jede Kategoriefarbe hat einen eigenen Satz; fehlt auch nur einer,
        # wird neu gerendert (billig genug, unter einer Sekunde).
        if all(os.path.exists(os.path.join(GAUGE_DIR, name, "100.png"))
               for name in module.GAUGE_SETS):
            return
        module.render_all(GAUGE_DIR, GAUGE_SIZE)
        for root, dirs, files in os.walk(GAUGE_DIR):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
    except Exception as exc:                       # noqa: BLE001
        print(f"Gauges konnten nicht gerendert werden: {exc}", file=sys.stderr)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ensure_gauges()
    cores = CoreSampler()
    cores.sample()                      # erster Abruf setzt nur die Basis
    iface = sys.argv[1] if len(sys.argv) > 1 else IFACE_DEFAULT
    netpeak = NetPeak(iface)
    diskio = DiskIO()
    diskio.sample()                     # erster Abruf setzt nur die Basis

    values = {key: "" for key, _ in METRICS}
    next_due = {key: 0.0 for key, _ in METRICS}

    while True:
        now = time.time()
        state = {
            "cores": cores.sample(),
            "net_peak": netpeak.sample(),
            "disk_io": diskio.sample(),
            "ram_top": cached("ram_top", 4.0, ram_top) or [],
            "screens": cached("screens", 30.0, screens) or [],
            "iface": iface,
        }
        render_ram_ring(state["ram_top"])

        for key, interval in METRICS:
            if now >= next_due[key]:
                try:
                    values[key] = collect(key, state)
                except Exception:
                    values[key] = values[key] or "-"
                next_due[key] = now + (interval if interval else 0)

        # Atomar schreiben, damit lcd4linux nie eine halbe Datei liest
        body = "\n".join(values[key] or "-" for key, _ in METRICS) + "\n"
        debug = "\n".join(f"{i+1}\t{k}\t{values[k]}" for i, (k, _) in enumerate(METRICS)) + "\n"
        for path, content in ((OUT_FILE, body), (DEBUG_FILE, debug)):
            with tempfile.NamedTemporaryFile(
                "w", dir=OUT_DIR, delete=False, prefix=".tmp-"
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, path)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
