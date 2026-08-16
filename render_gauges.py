#!/usr/bin/env python3
"""Rendert die Auslastungs-Uhren (Ring-Gauges) als PNG-Satz.

lcd4linux kann nur waagerechte/senkrechte Balken zeichnen. Runde Anzeigen gibt
es deshalb als vorgerenderte Bilder: fuer jeden ganzzahligen Prozentwert eine
Datei, die das Image-Widget ueber den Dateinamen auswaehlt:

    file '/run/lcd4linux/gauge/cpu/'.precision(<wert>, 0).'.png'

Damit muss zur Laufzeit nichts neu gezeichnet werden - der Satz entsteht einmal
beim Start des Collectors. Die Prozentzahl steht mit im Bild, weil ein
Truetype-Widget darueber seinen eigenen Hintergrund malen und den Ring
ueberdecken wuerde.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = "/usr/share/lcd4linux/fonts/jetbrains-mono"
SS = 4                      # Supersampling: gross zeichnen, klein skalieren = weiche Kanten

# Ein Satz je Kategoriefarbe, damit die Ringe dieselbe Farbe tragen wie die
# Beschriftung ihrer Sektion. Die Werte muessen zur Palette in build_theme.py
# passen - die Ringe entstehen hier, das uebrige Layout dort.
GAUGE_SETS = {
    "cpu": "00e5d0",        # TEAL
    "gpu": "2979ff",        # BLUE
    "ram": "b14cff",        # PURPLE
    "disk": "18d9ff",       # CYAN
}
GAUGE_TRACK = "12303a"

# Farben der fuenf groessten Speicherfresser - Ringsegment und Listeneintrag
# tragen dieselbe, sonst laesst sich das eine dem anderen nicht zuordnen.
# Muessen zu TOP_COLORS in build_theme.py passen.
TOP_COLORS = ("b14cff", "18d9ff", "ff2d9e", "7dfff0", "5f8fd0")


def load_font(size, bold=True):
    name = "JetBrainsMono-ExtraBold.ttf" if bold else "JetBrainsMono-Regular.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


def hex_rgb(value, alpha=255):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def glow_layer(size, draw_fn, radius, alpha=170):
    """Weicher Schein hinter einer Form - der Neon-Anteil am Neon.

    Die Form wird ein zweites Mal auf eine eigene Ebene gezeichnet, weich
    gezeichnet und dahintergelegt. Auf schwarzem Grund wirkt der Ring damit
    leuchtend statt aufgemalt.
    """
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer))
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    if alpha < 255:
        layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
    return layer


def render_gauge(percent, size, fg, track, text_color=None, thickness_ratio=0.15,
                 label=None):
    """Ein Ring, der im Uhrzeigersinn ab 12 Uhr auf `percent` fuellt."""
    text_color = text_color or fg
    big = size * SS
    # RGBA mit transparentem Grund: lcd4linux wertet den Alphakanal aus, der
    # Ring schwebt damit ueber dem Hintergrundbild statt in einem schwarzen
    # Kasten zu sitzen (am Geraet geprueft).
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    thickness = max(2 * SS, int(big * thickness_ratio))
    pad = thickness // 2 + SS
    box = [pad, pad, big - pad, big - pad]
    sweep = 360.0 * min(100.0, percent) / 100.0

    if percent > 0:
        # Schein zuerst, damit er hinter dem Ring liegt
        img = Image.alpha_composite(
            img,
            glow_layer(
                big,
                lambda d: d.arc(box, -90, -90 + sweep, fill=hex_rgb(fg),
                                width=int(thickness * 1.4)),
                radius=big * 0.045,
            ),
        )

    draw = ImageDraw.Draw(img)
    draw.arc(box, 0, 360, fill=hex_rgb(track), width=thickness)
    if percent > 0:
        # -90 Grad: PIL beginnt bei 3 Uhr, der Ring soll oben starten
        draw.arc(box, -90, -90 + sweep, fill=hex_rgb(fg), width=thickness)

    inner = big - 2 * (thickness + 2 * SS)
    text = f"{percent:.0f}"
    # Klein genug, dass die Ziffern den Ring nicht beruehren - dreistellig
    # ("100") muss deutlich schmaler ausfallen als zweistellig.
    font_size = int(inner * (0.50 if len(text) < 3 else 0.34))
    font = load_font(font_size)
    tb = draw.textbbox((0, 0), text, font=font)
    tx = (big - (tb[2] - tb[0])) / 2 - tb[0]
    ty = (big - (tb[3] - tb[1])) / 2 - tb[1]
    if label:
        ty -= inner * 0.10
    draw.text((tx, ty), text, font=font, fill=hex_rgb(text_color))

    if label:
        lf = load_font(int(inner * 0.28), bold=False)
        lb = draw.textbbox((0, 0), label, font=lf)
        draw.text(
            ((big - (lb[2] - lb[0])) / 2 - lb[0], big / 2 + inner * 0.20),
            label, font=lf, fill=hex_rgb(track),
        )

    return img.resize((size, size), Image.LANCZOS)


def render_set(out_dir, size, fg, track, text_color=None, steps=101):
    os.makedirs(out_dir, exist_ok=True)
    for value in range(steps):
        path = os.path.join(out_dir, f"{value}.png")
        render_gauge(value, size, fg, track, text_color).save(path)
    return steps


def render_segments(values, size, colors=TOP_COLORS, track=GAUGE_TRACK,
                    thickness_ratio=0.15, total_text=None):
    """Ring aus mehreren Abschnitten - je Wert einer, Rest bleibt Rinne.

    Anders als die Prozent-Ringe laesst sich das nicht vorrendern: der Zustand
    haengt an fuenf Werten gleichzeitig, das waere ein fuenfdimensionaler Satz.
    Dieses eine Bild entsteht deshalb zur Laufzeit im Collector (rund 20 ms)
    und wird vom Image-Widget als feste Datei mit reload 1 immer neu geladen.
    """
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    thickness = max(2 * SS, int(big * thickness_ratio))
    pad = thickness // 2 + SS
    box = [pad, pad, big - pad, big - pad]

    def arcs(draw, width):
        angle = -90.0                  # oben beginnen, im Uhrzeigersinn
        for i, value in enumerate(values):
            sweep = 360.0 * max(0.0, min(100.0, value)) / 100.0
            if sweep <= 0:
                continue
            draw.arc(box, angle, angle + sweep,
                     fill=hex_rgb(colors[i % len(colors)]), width=width)
            angle += sweep

    img = Image.alpha_composite(
        img,
        glow_layer(big, lambda d: arcs(d, int(thickness * 1.4)),
                   radius=big * 0.045),
    )

    draw = ImageDraw.Draw(img)
    draw.arc(box, 0, 360, fill=hex_rgb(track), width=thickness)
    arcs(draw, thickness)

    inner = big - 2 * (thickness + 2 * SS)
    text = total_text if total_text is not None else f"{sum(values):.0f}"
    font_size = int(inner * (0.50 if len(text) < 3 else 0.34))
    font = load_font(font_size)
    tb = draw.textbbox((0, 0), text, font=font)
    draw.text(((big - (tb[2] - tb[0])) / 2 - tb[0],
               (big - (tb[3] - tb[1])) / 2 - tb[1]),
              text, font=font, fill=hex_rgb(colors[0]))

    return img.resize((size, size), Image.LANCZOS)


def render_all(base, size, track=GAUGE_TRACK):
    """Alle Kategoriefarben auf einmal - je Satz ein Unterverzeichnis."""
    written = 0
    for name, fg in GAUGE_SETS.items():
        written += render_set(os.path.join(base, name), size, fg, track)
    return written


def contact_sheet(size, fg, track, values=(0, 3, 12, 25, 40, 55, 68, 80, 92, 100)):
    """Uebersichtsbild zum Ansehen - nicht fuer das Display."""
    pad = 8
    sheet = Image.new(
        "RGB",
        (len(values) * (size + pad) + pad, size + 2 * pad),
        (10, 10, 14),
    )
    for i, value in enumerate(values):
        tile = render_gauge(value, size, fg, track)
        sheet.paste(tile, (pad + i * (size + pad), pad), tile)
    return sheet


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "/run/lcd4linux/gauge"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 44
    n = render_all(base, size)
    print(f"{n} Gauges à {size}px in {len(GAUGE_SETS)} Farben nach {base} "
          f"geschrieben ({', '.join(GAUGE_SETS)})")
