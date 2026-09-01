"""Vereinheitlicht die von Hand freigestellten Ressourcen-Icons (Pictures/)
auf eine gemeinsame Leinwandgroesse, OHNE sie zu verzerren.

Vorgehen pro Icon:
  1. Transparente Raender automatisch wegschneiden (Bounding-Box des
     sichtbaren Inhalts) - gleicht unterschiedlich grosszuegige manuelle
     Zuschnitte aus.
  2. Den sichtbaren Inhalt (Seitenverhaeltnis erhalten!) so skalieren, dass
     er in ein gemeinsames Zielfeld passt.
  3. Zentriert auf eine einheitliche, transparente Leinwand einfuegen.

Ergebnis: alle Icons haben exakt dieselbe Pixelgroesse UND denselben
"visuellen Fuellgrad" - lassen sich also spaeter alle gleich skalieren,
ohne dass eins ploetzlich viel kleiner/groesser wirkt als die anderen.
"""

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "Pictures"
OUT_DIR = HERE.parent / "app" / "assets" / "icons"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CANVAS_SIZE = 64      # finale Bildgroesse (Breite = Hoehe)
CONTENT_MAX = 56      # max. Groesse des sichtbaren Inhalts innerhalb der Leinwand

# Dateiname (ohne .png) -> Ziel-Key aus RESOURCE_OFFSETS in app/reader.py
NAME_MAP = {
    "Armbrust": "crossbows",
    "Bier": "ale",
    "Bogen": "bows",
    "Brot": "bread",
    "Eisen": "iron",
    "Fleisch": "meat",
    "Gold": "gold",
    "Harnisch": "leather_armor",
    "Holz": "wood",
    "Hopfen": "hops",
    "Käse": "cheese",
    "Lanze": "spears",
    "Mehl": "flour",
    "Obst": "apples",
    "Pech": "pitch",
    "Pike": "pikes",
    "Rüstung": "metal_armor",
    "Schwert": "swords",
    "Stein": "stone",
    "Streitkolben": "maces",
    "Weizen": "wheat",
}


def normalize_one(src_path: Path, dst_path: Path):
    im = Image.open(src_path).convert("RGBA")

    bbox = im.getbbox()
    if bbox is not None:
        im = im.crop(bbox)

    w, h = im.size
    scale = min(CONTENT_MAX / w, CONTENT_MAX / h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    im = im.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    x = (CANVAS_SIZE - new_w) // 2
    y = (CANVAS_SIZE - new_h) // 2
    canvas.paste(im, (x, y), im)
    canvas.save(dst_path)


def main():
    missing = []
    for src_name, key in NAME_MAP.items():
        src_path = SRC_DIR / f"{src_name}.png"
        if not src_path.exists():
            missing.append(src_name)
            continue
        dst_path = OUT_DIR / f"resource_{key}.png"
        normalize_one(src_path, dst_path)
        print(f"{src_name}.png -> {dst_path.name}")

    if missing:
        print("\n[WARNUNG] Nicht gefunden:", ", ".join(missing))


if __name__ == "__main__":
    main()
