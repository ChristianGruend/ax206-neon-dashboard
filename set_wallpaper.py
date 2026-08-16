#!/usr/bin/env python3
"""Bereitet ein beliebiges Bild als Hintergrund fuer das AX206-Display auf.

    sudo ./set_wallpaper.py bild.jpg [--dim 0.30] [--keine-config]
    sudo ./set_wallpaper.py --entfernen

Das Bild wird auf 320x480 zugeschnitten (mittig, formatfuellend) und
abgedunkelt - ohne das Abdunkeln verschwinden die Werte auf hellen Stellen.
Danach wird die Config neu erzeugt und das Display neu gestartet.

Die Kern-Ringe sind RGBA-PNGs und der Text zeichnet transparent, beides steht
also frei auf dem Bild. Die Balken haben dagegen einen eigenen Hintergrund.
"""
import argparse
import os
import subprocess
import sys

from PIL import Image, ImageEnhance

TARGET = "/etc/lcd4linux/wallpaper.png"
WIDTH, HEIGHT = 320, 480
HERE = os.path.dirname(os.path.abspath(__file__))


def prepare(src, dim, scanlines=True):
    img = Image.open(src)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Formatfuellend zuschneiden: skalieren bis beide Seiten reichen, dann
    # mittig beschneiden. Oben etwas mehr stehen lassen - bei Portraetmotiven
    # sitzt das Interessante meist nicht in der exakten Mitte.
    scale = max(WIDTH / img.width, HEIGHT / img.height)
    new_size = (max(WIDTH, round(img.width * scale)),
                max(HEIGHT, round(img.height * scale)))
    img = img.resize(new_size, Image.LANCZOS)
    left = (img.width - WIDTH) // 2
    top = int((img.height - HEIGHT) * 0.35)
    img = img.crop((left, top, left + WIDTH, top + HEIGHT))

    img = ImageEnhance.Brightness(img).enhance(dim)
    # Etwas Kontrast zuruecknehmen, damit helle Kanten die Schrift nicht stoeren
    img = ImageEnhance.Contrast(img).enhance(0.85)
    if scanlines:
        img = add_scanlines(img)
    return img


def add_scanlines(img, spacing=3, strength=0.55):
    """Jede dritte Zeile abdunkeln - Roehrenmonitor-Effekt.

    Kostet nebenbei Helligkeit genau dort, wo sonst Schrift auf hellem Grund
    steht, und macht die Werte damit besser lesbar statt schlechter.
    """
    dark = img.point(lambda v: int(v * strength))
    mask = Image.new("L", img.size, 0)
    line = Image.new("L", (img.width, 1), 255)
    for y in range(0, img.height, spacing):
        mask.paste(line, (0, y))
    return Image.composite(dark, img, mask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bild", nargs="?", help="Quellbild (jpg/png/...)")
    ap.add_argument("--dim", type=float, default=0.30,
                    help="Helligkeit 0.0-1.0, Standard 0.30")
    ap.add_argument("--entfernen", action="store_true",
                    help="Hintergrundbild loeschen")
    ap.add_argument("--keine-config", action="store_true",
                    help="nur das Bild schreiben, Config/Dienst nicht anfassen")
    ap.add_argument("--keine-scanlines", action="store_true",
                    help="ohne Roehrenmonitor-Effekt")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Bitte mit sudo ausfuehren.")

    if args.entfernen:
        if os.path.exists(TARGET):
            os.remove(TARGET)
            print(f"{TARGET} geloescht")
        else:
            print("Kein Hintergrundbild gesetzt")
    else:
        if not args.bild:
            sys.exit("Kein Bild angegeben (oder --entfernen benutzen)")
        if not os.path.isfile(args.bild):
            sys.exit(f"Nicht gefunden: {args.bild}")
        if not 0.0 < args.dim <= 1.0:
            sys.exit("--dim muss zwischen 0 und 1 liegen")
        prepare(args.bild, args.dim, not args.keine_scanlines).save(TARGET)
        os.chmod(TARGET, 0o644)
        print(f"{TARGET} geschrieben ({WIDTH}x{HEIGHT}, Helligkeit {args.dim})")

    if args.keine_config:
        return

    # Im Repo liegt der Generator daneben, installiert unter /etc/lcd4linux.
    build = next(
        (p for p in (os.path.join(HERE, "build_theme.py"),
                     "/etc/lcd4linux/build_theme.py")
         if os.path.exists(p)),
        None,
    )
    if build is None:
        sys.exit("build_theme.py nicht gefunden - Bild ist gesetzt, aber die "
                 "Config wurde nicht neu erzeugt.")
    subprocess.run([sys.executable, build, "/tmp/ax206-dashboard.conf"], check=True)
    subprocess.run(
        ["install", "-m", "600", "/tmp/ax206-dashboard.conf",
         "/etc/lcd4linux/lcd4linux.conf"], check=True,
    )
    os.remove("/tmp/ax206-dashboard.conf")
    subprocess.run(["systemctl", "restart", "lcd4linux-ax206.service"], check=True)
    print("Config neu erzeugt, Display neu gestartet.")


if __name__ == "__main__":
    main()
