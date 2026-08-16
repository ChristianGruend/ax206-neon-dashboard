#!/usr/bin/env python3
"""Erzeugt die lcd4linux-Config fuer das AX206-Dashboard (AX206 Neon Dashboard).

Warum generiert und nicht von Hand: das Layout ist reine Pixelarithmetik auf
320x480, und jede eingeschobene Zeile verschiebt alles darunter. Der Generator
fuehrt einen Zeilencursor, kennt die Zeilennummern der Collector-Metriken und
prueft zum Schluss auf Ueberlappungen, Ueberlauf und zu schmale Textfelder.

    python3 build_theme.py [ausgabe.conf] [--vnc]

--vnc erzeugt die Variante fuer die Vorschau (VNC statt echtem DPF-Display).

Zwei am Geraet nachgemessene Eigenheiten, die man nirgends dokumentiert findet:

* In der Layout-Sektion steht 'X<zeile>.Y<spalte>' - die erste Zahl laeuft nach
  unten (0..479), die zweite nach rechts (0..319). Die Buchstaben legen das
  Gegenteil nahe.
* **Layer 0 liegt vorn**, hoehere Layer liegen dahinter. Das Hintergrundbild
  muss also auf den hoechsten Layer, der Inhalt auf Layer 0. Truetype-Text
  zeichnet transparent und steht damit sauber auf dem Bild; Bilder und
  graphicbars sind opak (Gauges kommen deshalb als RGBA-PNG).
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Zeilenvertrag aus dem Collector holen, statt ihn hier zu duplizieren ---
# Im Repo liegt er daneben, installiert unter /usr/local/bin.
COLLECTOR = next(
    (p for p in (os.path.join(HERE, "ax206-dash-collect.py"),
                 "/usr/local/bin/ax206-dash-collect.py")
     if os.path.exists(p)),
    None,
)
if COLLECTOR is None:
    sys.exit("ax206-dash-collect.py nicht gefunden - ohne den Zeilenvertrag "
             "laesst sich die Config nicht erzeugen.")
spec = importlib.util.spec_from_file_location("collector", COLLECTOR)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
LINE = {key: i + 1 for i, (key, _) in enumerate(collector.METRICS)}
DISKS = collector.DISKS          # Plattenplaetze, Reihenfolge = Ringreihenfolge

# Maschinenspezifisches kommt aus derselben TOML wie beim Collector - er hat
# sie beim Import schon gelesen, hier wird nur abgegriffen.
_SYSTEM = collector.CFG.get("system", {})
IFACE = collector.IFACE_DEFAULT
CPU_MODEL = _SYSTEM.get("cpu_model", "CPU")
GPU_MODEL = _SYSTEM.get("gpu_model", "GPU")
# Zweite Adresse neben der LAN-IP, meist ein VPN. Leer laesst das Feld weg.
VPN_DEVICE = _SYSTEM.get("vpn_device", "")
VPN_LABEL = _SYSTEM.get("vpn_label", "VPN")

# AX206_DEMO=1 setzt Beispieladressen statt der echten - fuer Screenshots in
# der Dokumentation. Betrifft nur die beiden IP-Felder; alles andere zeigt
# weiterhin die tatsaechlichen Werte der Maschine.
DEMO = bool(os.environ.get("AX206_DEMO"))
N_CORES = collector.N_CORES
STACK = collector.STACK
STACK_LAMPS = collector.STACK_LAMPS
SYS_LAMPS = collector.SERVICES

METRICS_FILE = "/run/lcd4linux/metrics"
GAUGE_DIR = "/run/lcd4linux/gauge"
WALLPAPER = "/etc/lcd4linux/wallpaper.png"

# --- Geometrie ---
WIDTH, HEIGHT = 320, 480
PAD = 4
INNER = WIDTH - 2 * PAD          # 312

CONTENT_LAYER = 0                # vorn
BACKGROUND_LAYER = 3             # hinten - dort liegt das Hintergrundbild

# --- Palette: Neon von Tuerkis ueber Blau und Cyan bis Violett und Magenta.
#     Kein Gruen, Grau, Weiss, Orange oder Rot. Jede Kategorie hat einen
#     kraeftigen Ton fuer die Beschriftung und einen hellen fuer den Wert.
#     Die drei Blautoene sitzen dicht beieinander, trennen sich aber ueber die
#     Sektionen: CPU zieht ins Gruenliche, GPU ist sattes Blau, Platten sind
#     das helle Eisblau dazwischen.
TEAL, TEAL_HI = "00e5d0", "7dfff0"          # CPU
BLUE, BLUE_HI = "2979ff", "82b1ff"          # GPU
PURPLE, PURPLE_HI = "b14cff", "d9a5ff"      # RAM
CYAN, CYAN_HI = "18d9ff", "9aecff"          # Platten
PINK, PINK_HI = "ff2d9e", "ff9ad0"          # Netz
VIOLET, VIOLET_HI = "c77dff", "e4c2ff"      # Container-Stack - die einzige
                                            # Sektion, die keine Hardware
                                            # beschreibt, sondern Dienste
DIM = "5f8fd0"                              # Nebeninfos - blau, nicht grau
TRACK = "12303a"                            # Ring- und Balkenrinne
LED_BG = "4a0f2e"                           # Dienst aus: dunkles Magenta

FONT_BOLD = "fonts/jetbrains-mono/JetBrainsMono-Bold.ttf"
FONT_REG = "fonts/jetbrains-mono/JetBrainsMono-Regular.ttf"
FONT_XBOLD = "fonts/jetbrains-mono/JetBrainsMono-ExtraBold.ttf"

# Segmentierte Balken. Nur noch dort, wo ein Ring nicht traegt: beim
# Netzdurchsatz (kein festes Maximum, ein Ring waere frei erfunden) und bei
# den Dienste-Ampeln (an/aus, da gibt es nichts zu fuellen).
SEG_W, SEG_GAP = 10, 4
SEG_H, SEG_H_SMALL = 10, 8

# Alle Ringe haben dieselbe Groesse - so genuegt ein PNG-Satz je Kategoriefarbe
# statt je Farbe und Groesse. Der Wert kommt aus dem Collector, der die Bilder
# rendert; stuenden hier und dort verschiedene Zahlen, waeren die Boxen falsch.
GAUGE_SIZE = collector.GAUGE_SIZE
GAUGE_COLS = 6                   # Kern-Ringe: 6 pro Reihe, zwei Reihen

# JetBrains Mono rendert in lcd4linux mit rund 0.8 * size pro Zeichen -
# empirisch an einer abgeschnittenen "LAN <ipv4>"-Zeile nachgemessen.
CHAR_W = 0.8

texts = []        # (name, laengster_beispieltext, size, width)
widgets = []      # (name, [(key, value), ...])
placed = {}       # layer -> [(row, col, name)]
boxes = []        # (name, row, col, w, h) - nur Inhalts-Layer


def metric(key):
    """Expression, die eine Collector-Zeile liest."""
    return f"file::readline('{METRICS_FILE}', {LINE[key]})"


def add(name, fields, row, col, w, h, layer=CONTENT_LAYER):
    widgets.append((name, fields))
    placed.setdefault(layer, []).append((row, col, name))
    if layer == CONTENT_LAYER:
        boxes.append((name, row, col, w, h))


def text(name, expression, row, col, width, height, size, color,
         font=FONT_REG, align="L", update=1000, sample=None):
    """sample = laengster real erwarteter Text; check() prueft, ob er passt."""
    if sample:
        texts.append((name, sample, size, width))
    add(
        name,
        [
            ("class", "'Truetype'"),
            ("expression", expression),
            ("font", f"'{font}'"),
            ("fcolor", f"'{color}'"),
            ("size", str(size)),
            ("align", f"'{align}'"),
            ("width", str(width)),
            ("height", str(height)),
            ("reload", "1"),
            ("update", str(update)),
        ],
        row, col, width, height,
    )


def bar(name, expression, row, col, length, thickness, color,
        maximum=100, minimum=0, background=TRACK, direction="E", update=1000):
    add(
        name,
        [
            ("class", "'graphicbar'"),
            ("expression", expression),
            ("color", f"'{color}'"),
            ("colorhigh", f"'{color}'"),
            ("colorlow", f"'{color}'"),
            ("background", f"'{background}'"),
            ("min", f"{minimum:g}"),
            ("max", f"{maximum:g}"),
            ("length", str(length)),
            ("width", str(thickness)),
            ("direction", f"'{direction}'"),
            ("update", str(update)),
        ],
        row, col,
        length if direction in ("E", "W") else thickness,
        thickness if direction in ("E", "W") else length,
    )


def segbar(name, expression, row, col, span, height, color,
           seg_w=SEG_W, gap=SEG_GAP, maximum=100, background=TRACK,
           update=1000):
    """Balkenanzeige als Reihe kurzer senkrechter Segmente.

    graphicbar kann nicht segmentieren, also bekommt jedes Segment denselben
    Ausdruck und ein eigenes Wertefenster ueber min/max: Segment i deckt
    [i/n, (i+1)/n] des Bereichs ab und ist damit leer, teilweise oder ganz
    gefuellt.
    """
    n = (span + gap) // (seg_w + gap)
    for i in range(n):
        bar(
            f"{name}{i}", expression,
            row, col + i * (seg_w + gap),
            height, seg_w, color,
            minimum=round(maximum * i / n, 4),
            maximum=round(maximum * (i + 1) / n, 4),
            background=background, direction="N", update=update,
        )
    return n * (seg_w + gap) - gap


def gauge(name, expression, row, col, size, subdir="cpu", update=2000):
    """Runde Auslastungsanzeige - vorgerendertes PNG je Prozentwert."""
    add(
        name,
        [
            ("class", "'image'"),
            ("file", f"'{GAUGE_DIR}/{subdir}/'.precision({expression}, 0).'.png'"),
            ("reload", "1"),
            ("update", str(update)),
            ("visible", "1"),
        ],
        row, col, size, size,
    )


def rule(name, row, color, thickness=2, col=PAD, length=INNER, block=26):
    """HUD-Trennlinie: kraeftiger Block, Luecke, duenner Strich.

    Ein durchgehender Strich wirkt wie eine Tabellenlinie; der abgesetzte Block
    davor liest sich als Marke und gibt der Sektion einen Anfang.
    """
    bar(f"{name}_B", "100", row, col, block, thickness + 1, color,
        background=color, update=60000)
    bar(name, "100", row + 1, col + block + 4, length - block - 4, 1, color,
        background=color, update=60000)


# =====================================================================
# Layout
# =====================================================================
y = 2

# --- Kopf: kein Rechnername mehr. Kernel und Laufzeit links, Uhrzeit rechts
#     ins Eck - ungefettet, damit sie sich nicht vordraengt. ---
text("UPT", "uname('release').'   UP '.uptime('%dd %H:%M')",
     y + 5, PAD, 200, 12, 8, DIM, update=60000,
     sample="7.0.0-28-generic   UP 0d 08:55")
text("CLOCK", "strftime('%H:%M:%S', time())", y, PAD + 204, 108, 17, 13,
     TEAL_HI, FONT_REG, align="R", update="tick", sample="08:55:22")
y += 18

rule("SEP1", y, TEAL)
y += 5

# --- CPU: Kopfzeile mit Modell und Gesamtwerten, darunter ein Ring je Thread ---
text("CPU_L", "'CPU'", y, PAD, 38, 16, 13, TEAL, FONT_XBOLD, update=60000,
     sample="CPU")
text("CPU_MODEL", f"'{CPU_MODEL}'", y, PAD + 40, 98, 16, 10, TEAL,
     update=60000, sample=CPU_MODEL)
text("CPU_TEMP", f"{metric('cpu_temp')}.'°C'", y, PAD + 140, 48, 16, 12,
     TEAL_HI, FONT_BOLD, update=3000, sample="100°C")
text("CPU_FREQ",
     "precision(file::readline('/sys/devices/system/cpu/cpu0/cpufreq/"
     "scaling_cur_freq', 1) / 1000000, 2).'GHz'",
     y, PAD + 190, 58, 16, 10, TEAL_HI, update=2000, sample="4.85GHz")
text("CPU_FAN", f"{metric('cpu_fan')}.'rpm'", y, PAD + 250, 62, 16, 10, DIM,
     align="R", update=3000, sample="1850rpm")
y += 18

# Ein Ring je Thread, sechs je Reihe - bei anderer Kernzahl waechst die
# Reihenzahl mit, und der Layout-Check meldet es, wenn es unten nicht mehr passt.
gauge_gap = (INNER - GAUGE_COLS * GAUGE_SIZE) // (GAUGE_COLS - 1)
core_rows = -(-N_CORES // GAUGE_COLS)
for i in range(N_CORES):
    row = y + (i // GAUGE_COLS) * (GAUGE_SIZE + 2)
    col = PAD + (i % GAUGE_COLS) * (GAUGE_SIZE + gauge_gap)
    gauge(f"CORE{i}", metric(f"core{i}"), row, col, GAUGE_SIZE)
y += core_rows * (GAUGE_SIZE + 2)

rule("SEP2", y, BLUE)
y += 5

# --- GPU ---
text("GPU_L", "'GPU'", y, PAD, 38, 15, 13, BLUE, FONT_XBOLD, update=60000,
     sample="GPU")
text("GPU_MODEL", f"'{GPU_MODEL}'", y, PAD + 40, 88, 15, 10, BLUE, update=60000,
     sample=GPU_MODEL)
text("GPU_TEMP", f"{metric('gpu_temp')}.'°C'", y, PAD + 172, 48, 15, 12,
     BLUE_HI, FONT_BOLD, update=3000, sample="100°C")
text("GPU_WATT", f"{metric('gpu_watt')}.'W'", y, PAD + 222, 32, 15, 9,
     BLUE_HI, update=2000, sample="230W")
text("GPU_FAN", f"{metric('gpu_fan')}.'rpm'", y, PAD + 256, 56, 15, 9, DIM,
     align="R", update=3000, sample="2400rpm")
y += 16

# Zwei Ringe links, die Detailwerte rechts daneben. Der Ring traegt den
# Prozentwert selbst, die Textzeilen sagen, wovon.
gpu_text_col = PAD + 2 * (GAUGE_SIZE + 6)
gauge("GPU_G", metric("gpu_pct"), y, PAD, GAUGE_SIZE, subdir="gpu")
gauge("VRAM_G", metric("vram_pct"), y, PAD + GAUGE_SIZE + 6, GAUGE_SIZE,
      subdir="gpu", update=3000)
text("GPU_VRAM", f"'VRAM  '.{metric('vram_used')}.' / '.{metric('vram_total')}.'G'",
     y + 4, gpu_text_col, INNER - gpu_text_col + PAD, 18, 12, BLUE_HI,
     update=3000, sample="VRAM  11.9 / 12.0G")
text("GPU_CLK", f"'TAKT  '.{metric('gpu_clock')}.' MHz'",
     y + 24, gpu_text_col, INNER - gpu_text_col + PAD, 16, 11, DIM,
     update=3000, sample="TAKT  2600 MHz")
y += GAUGE_SIZE + 1

text("GPU_GL", "'LAST'", y, PAD, GAUGE_SIZE, 10, 8, BLUE, align="C",
     update=60000, sample="LAST")
text("VRAM_GL", "'VRAM'", y, PAD + GAUGE_SIZE + 6, GAUGE_SIZE, 10, 8, BLUE,
     align="C", update=60000, sample="VRAM")
y += 10

# Bildschirme nebeneinander in einer Zeile - Aufloesung samt
# Bildwiederholrate; bei gedrehten Panels die sichtbare, nicht die native.
third = INNER // collector.N_SCREENS
for i in range(collector.N_SCREENS):
    text(f"SCR{i}", metric(f"screen{i}"), y, PAD + i * third, third, 10, 7,
         DIM, update=30000, sample="DP-1 2560x1440 144")
y += 12

rule("SEP3", y, PURPLE)
y += 5

# --- RAM: der Ring ist in Abschnitte geteilt, einer je groesstem Verbraucher,
#     daneben dieselben fuenf als Liste. Abschnitt und Zeile tragen dieselbe
#     Farbe - sonst liesse sich das eine dem anderen nicht zuordnen. ---
text("RAM_L", "'RAM'", y, PAD, 40, 13, 12, PURPLE, FONT_XBOLD, update=60000,
     sample="RAM")
text("RAM_USE",
     "precision((meminfo('MemTotal') - meminfo('MemAvailable')) / 1048576, 1)"
     ".' / '.precision(meminfo('MemTotal') / 1048576, 1).'G   SWAP '."
     "precision((meminfo('SwapTotal') - meminfo('SwapFree')) / 1048576, 1).'G'",
     y, PAD + 42, INNER - 42, 13, 10, PURPLE_HI, align="R", update="tick",
     sample="31.2 / 31.2G   SWAP 33.9G")
y += 14

# Feste Datei statt Prozent-Auswahl: dieser Ring haengt an fuenf Werten
# gleichzeitig und wird deshalb vom Collector zur Laufzeit gezeichnet.
add("RAM_RING",
    [("class", "'image'"), ("file", f"'{GAUGE_DIR}/ramtop.png'"),
     ("reload", "1"), ("update", "4000"), ("visible", "1")],
    y, PAD, GAUGE_SIZE, GAUGE_SIZE)

# Zweispaltig: nutzt die Breite neben dem Ring und spart zwei Zeilen Hoehe.
TOP_COLORS = (PURPLE, CYAN, PINK, TEAL_HI, DIM)
top_col = PAD + GAUGE_SIZE + 8
top_w = (INNER - top_col + PAD) // 2
for i in range(collector.N_TOP):
    text(f"RTOP{i}", metric(f"rtop{i}"),
         y + (i % 3) * 11, top_col + (i // 3) * top_w,
         top_w - 4, 11, 8, TOP_COLORS[i], update=5000,
         sample="Isolated 99.9%")
y += max(GAUGE_SIZE, 3 * 11) + 3

rule("SEP4", y, CYAN)
y += 5

# --- Platten: ein Ring je Platz, darunter Name und Groesse. Nicht
#     eingehaengte Plaetze (etwa OneDrive, solange nichts gemountet ist)
#     stehen auf 0 % und "-", statt die Reihe zu verschieben. ---
# Durchsatz als Zahl *und* Balken: die Zahl sagt wie viel, der Balken auf
# einen Blick ob gerade ueberhaupt etwas laeuft. Skala 200 MB/s - darueber
# steht der Balken voll, das ist fuer SSD-Spitzen bewusst in Kauf genommen.
DISK_SCALE = 200
text("DISK_L", "'DISK'", y, PAD, 42, 14, 13, CYAN, FONT_XBOLD, update=60000,
     sample="DISK")
text("DISK_R", f"'R '.{metric('disk_read')}", y + 2, PAD + 44, 54, 12, 9,
     CYAN_HI, update=2000, sample="R 188.8")
segbar("DISK_RS", metric("disk_read"), y + 3, PAD + 100, 56, 8, CYAN,
       seg_w=6, gap=2, maximum=DISK_SCALE, update=2000)
text("DISK_W", f"'W '.{metric('disk_write')}", y + 2, PAD + 162, 54, 12, 9,
     CYAN_HI, update=2000, sample="W 188.8")
segbar("DISK_WS", metric("disk_write"), y + 3, PAD + 218, 56, 8, CYAN,
       seg_w=6, gap=2, maximum=DISK_SCALE, update=2000)
text("DISK_U", "'MB/S'", y + 3, PAD + 278, 34, 11, 8, DIM, align="R",
     update=60000, sample="MB/S")
y += 15

disk_gap = (INNER - len(DISKS) * GAUGE_SIZE) // (len(DISKS) - 1)
for i, (label, _path) in enumerate(DISKS):
    col = PAD + i * (GAUGE_SIZE + disk_gap)
    gauge(f"DSK{i}", metric(f"dsk{i}_pct"), y, col, GAUGE_SIZE, subdir="disk",
          update=20000)
    text(f"DSKL{i}", f"'{label}'", y + GAUGE_SIZE + 1, col, GAUGE_SIZE, 10, 8,
         CYAN, align="C", update=60000, sample=label)
    text(f"DSKS{i}", metric(f"dsk{i}_txt"), y + GAUGE_SIZE + 11, col,
         GAUGE_SIZE, 10, 8, DIM, align="C", update=20000, sample="913G")
y += GAUGE_SIZE + 22

rule("SEP5", y, PINK)
y += 5

# --- Netz ---
# Aktueller Durchsatz gross, die seit dem Start uebertragene Gesamtmenge
# klein daneben - zwei Groessen, die man selten verwechseln will.
text("DN_L", "'DN'", y, PAD, 22, 16, 11, PINK, FONT_XBOLD, update=60000,
     sample="DN")
text("DN_V",
     f"precision(netdev('{IFACE}', 'Rx_bytes', 500) / 1048576, 1).' M/S'",
     y, PAD + 24, 90, 16, 12, PINK_HI, FONT_BOLD, update="tick",
     sample="118.0 M/S")
text("DN_T", f"'\u03a3 '.{metric('net_rx_total')}", y + 4, PAD + 116, 40, 12, 7,
     DIM, align="R", update=20000, sample="Σ 308M")
text("UP_L", "'UP'", y, PAD + 160, 22, 16, 11, PINK, FONT_XBOLD, update=60000,
     sample="UP")
text("UP_V",
     f"precision(netdev('{IFACE}', 'Tx_bytes', 500) / 1048576, 1).' M/S'",
     y, PAD + 184, 90, 16, 12, PINK_HI, FONT_BOLD, update="tick",
     sample="118.0 M/S")
text("UP_T", f"'\u03a3 '.{metric('net_tx_total')}", y + 4, PAD + 276, 36, 12, 7,
     DIM, align="R", update=20000, sample="Σ 174M")
y += 16

segbar("DN_SEG", f"netdev('{IFACE}', 'Rx_bytes', 500) / 1048576",
       y, PAD, 152, SEG_H_SMALL, PINK, seg_w=7, gap=3, maximum=25)
segbar("UP_SEG", f"netdev('{IFACE}', 'Tx_bytes', 500) / 1048576",
       y, PAD + 160, 152, SEG_H_SMALL, PINK, seg_w=7, gap=3, maximum=25)
y += SEG_H_SMALL + 2

# Breit genug fuer die laengstmoegliche IPv4 - eine kuerzere Beispieladresse
# haette hier jahrelang gepasst und waere im Zweifel abgeschnitten worden.
text("LAN_V",
     "'LAN 192.168.0.42'" if DEMO else f"'LAN '.netinfo::ipaddr('{IFACE}')",
     y, PAD, 154, 13, 10, PINK_HI, update=60000, sample="LAN 255.255.255.255")
if VPN_DEVICE:
    text("TS_V",
         f"'{VPN_LABEL} 100.64.0.7'" if DEMO
         else f"'{VPN_LABEL} '.netinfo::ipaddr('{VPN_DEVICE}')",
         y, PAD + 158, 154, 13, 10, PINK_HI, align="R", update=60000,
         sample=f"{VPN_LABEL} 255.255.255.255")
y += 14

rule("SEP6", y, VIOLET)
y += 5

# --- Container-Stack: eigene Sektion, weil hier als einziges kein Blech
#     gemessen wird, sondern ein Dienst. Laufzeit, Speicher und Alter der
#     Sicherung in der Kopfzeile, die Begleitdienste als Ampeln. ---
if STACK:
    stack_label = STACK.get("label", "STACK")
    text("STACK_L", f"'{stack_label}'", y, PAD, 74, 13, 11, VIOLET, FONT_XBOLD,
         update=60000, sample=stack_label)
    text("STACK_INFO",
         f"'UP '.{metric('stack_up')}.'   RAM '.{metric('stack_ram')}"
         f".'   BKP '.{metric('backup_age')}",
         y + 1, PAD + 76, INNER - 76, 12, 9, VIOLET_HI, align="R", update=15000,
         sample="UP 120d   RAM 12.5G   BKP 18H")
    y += 14

# Ampeln: gefuellt = laeuft, sichtbarer Grund = aus. Die Stack-Dienste in der
# Sektionsfarbe, die uebrigen Systemdienste daneben in Tuerkis.
SERVICES = (
    [(lamp.get("label", f"S{i}"), f"slamp{i}", VIOLET)
     for i, lamp in enumerate(STACK_LAMPS)]
    + [(lamp.get("label", f"L{i}"), f"lamp{i}", TEAL)
       for i, lamp in enumerate(SYS_LAMPS)]
)
LED_GAP = 3
LED_W = (INNER - (len(SERVICES) - 1) * LED_GAP) // len(SERVICES)
for i, (label, key, color) in enumerate(SERVICES):
    col = PAD + i * (LED_W + LED_GAP)
    bar(f"LED_{label}", metric(key), y, col, LED_W, 9, color,
        background=LED_BG, update=5000)
    text(f"LEDL_{label}", f"'{label}'", y + 10, col, LED_W, 10, 8, DIM,
         align="C", update=60000, sample="OLLA")
y += 21

# --- Fuss: Datum, Restsensorik, Wetter. Die Uhrzeit steht oben rechts im
#     Kopf, das Backup-Alter jetzt beim Stack (dessen Sicherung es ist), und
#     der Top-Prozess ist entfallen - die RAM-Liste zeigt ihn ohnehin, und
#     zwar mit vier weiteren dazu. ---
y += 3

text("DATE", "strftime('%a %d.%m.%Y', time())", y, PAD, 114, 13, 10, CYAN,
     FONT_BOLD, update=60000, sample="Sat 15.08.2026")
text("SENS",
     f"'MB '.{metric('mb_temp')}.'°  UPD '.{metric('updates')}",
     y + 1, PAD + 116, 92, 12, 8, DIM, align="C", update=5000,
     sample="MB 40°  UPD 12")
text("WX", metric("weather"), y, PAD + 210, 102, 13, 10, TEAL_HI, align="R",
     update=60000, sample="+20°C Sunny")
y += 13

TOTAL_HEIGHT = y

# --- Hintergrundbild ganz nach hinten, falls vorhanden ---
if os.path.exists(WALLPAPER):
    add(
        "WALL",
        [
            ("class", "'image'"),
            ("file", f"'{WALLPAPER}'"),
            ("reload", "0"),
            ("update", "60000"),
            ("visible", "1"),
        ],
        0, 0, WIDTH, HEIGHT, layer=BACKGROUND_LAYER,
    )


# =====================================================================
# Ausgabe + Selbstpruefung
# =====================================================================
def render(vnc=False):
    lines = [
        f"# AX206 Neon Dashboard ({WIDTH}x{HEIGHT}, portrait)",
        "#",
        "# ERZEUGT von build_theme.py - Aenderungen hier gehen beim naechsten",
        "# Lauf verloren. Layout und Farben im Generator anpassen.",
        "#",
        "# Werte, die lcd4linux nicht guenstig selbst liefert (Per-Core-Last,",
        "# Dienste, GPU-Globs, Wetter), kommen zeilenweise aus",
        f"# {METRICS_FILE}, geschrieben von ax206-dash-collect.py.",
        f"# Die Kern-Ringe sind vorgerenderte PNGs aus {GAUGE_DIR}.",
        "",
    ]
    if vnc:
        lines += [
            "Display dpf {",
            "    Driver     'VNC'",
            f"    Xres       {WIDTH}",
            f"    Yres       {HEIGHT}",
            "    Port       5987",
            "    Maxclients 2",
            "    Font       '6x8'",
            "    Foreground 'ffffff'",
            "    Background '000000'",
            "    Basecolor  '000000'",
            "}",
        ]
    else:
        lines += [
            "Display dpf {",
            "    Driver     'DPF'",
            "    Port       'usb0'",
            "    Font       '6x8'",
            "    Foreground 'ffffff'",
            "    Background '000000'",
            "    Basecolor  '000000'",
            "    Orientation 3",
            "    Backlight  backlight",
            "}",
        ]
    lines.append("")

    for name, fields in widgets:
        lines.append(f"Widget {name} {{")
        for key, value in fields:
            lines.append(f"    {key} {value}")
        lines.append("}")
    lines.append("")

    lines.append("Display 'DPF'")
    lines.append("")
    lines.append("Layout layout_ax206 {")
    for layer in sorted(placed):
        lines.append(f"    Layer {layer} {{")
        for row, col, name in sorted(placed[layer]):
            lines.append(f"        X{row}.Y{col}".ljust(22) + f"'{name}'")
        lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("Layout 'layout_ax206'")
    lines.append("")
    lines.append("Variables {")
    lines.append("    backlight 7")
    lines.append("    tick 1000")
    lines.append("    tack 500")
    lines.append("}")
    return "\n".join(lines) + "\n"


def check():
    """Ueberlauf, Ueberlappung und zu enge Textfelder melden."""
    problems = []
    if TOTAL_HEIGHT > HEIGHT:
        problems.append(f"Layout ist {TOTAL_HEIGHT}px hoch, Display hat {HEIGHT}px")

    for name, row, col, w, h in boxes:
        if col + w > WIDTH:
            problems.append(f"{name}: ragt rechts raus (col {col} + {w} > {WIDTH})")
        if row + h > HEIGHT:
            problems.append(f"{name}: ragt unten raus (row {row} + {h} > {HEIGHT})")

    for name, sample, size, width in texts:
        needed = len(sample) * size * CHAR_W
        if needed > width:
            problems.append(
                f"{name}: '{sample}' braucht ~{needed:.0f}px, hat aber {width}px"
            )

    for i, (n1, r1, c1, w1, h1) in enumerate(boxes):
        for n2, r2, c2, w2, h2 in boxes[i + 1:]:
            if r1 < r2 + h2 and r2 < r1 + h1 and c1 < c2 + w2 and c2 < c1 + w1:
                problems.append(f"Ueberlappung: {n1} <-> {n2}")
    return problems


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    vnc = "--vnc" in sys.argv
    out_path = args[0] if args else "lcd4linux.conf"

    with open(out_path, "w") as fh:
        fh.write(render(vnc=vnc))

    wall = "mit" if os.path.exists(WALLPAPER) else "ohne"
    print(f"{out_path} geschrieben - {len(widgets)} Widgets, "
          f"Layout {TOTAL_HEIGHT}px von {HEIGHT}px hoch, {wall} Hintergrundbild")
    issues = check()
    if issues:
        print("\nPROBLEME:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    print("Geometrie geprueft: kein Ueberlauf, keine Ueberlappung")
