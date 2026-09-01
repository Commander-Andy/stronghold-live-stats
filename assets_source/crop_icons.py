"""Schneidet die Sammel-Grafik 'Truppen und waren.png' in einzelne Icons.

Layout (automatisch per Zeilen/Spalten-Projektion ermittelt):
  LINKS (Schild-Icons, Truppen):
    Block 1: y=32-140,  x=28-448   -> 8 Icons (Basis-Truppen)
    Block 2: y=175-276, x=30-398   -> 7 Icons (arabische Truppen)
    Block 3: y=296-384, x=29-135   -> 2 Icons (Leitertraeger/Tunnelgraeber)
    Block 4: y=398-493, x=28-90    -> 1 Icon  (Einzelfigur)
    Block 5: y=515-602, x=26-455   -> 7 Icons (Belagerungsgeraete)
  RECHTS (rechteckige Icons, Ressourcen):
    Block 6: y=44-103,  x=653-1093 -> 8 Icons (Nahrung/Waren)
    Block 7: y=131-192, x=654-1063 -> 4 Icons (Rohstoffe)
    Block 8: y=227-288, x=655-1090 -> 8 Icons (Waffen-Rohmaterial)
"""

from PIL import Image
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "Truppen und waren.png"
OUT_DIR = HERE / "icons_cropped"
OUT_DIR.mkdir(exist_ok=True)

BLOCKS = [
    ("basis_truppen",   32, 140, 28, 448, 8),
    ("arabische_truppen", 175, 276, 30, 398, 7),
    ("leiter_tunnel",   296, 384, 29, 135, 2),
    ("einzelfigur",     398, 493, 28, 90, 1),
    ("belagerung",      515, 602, 26, 455, 7),
    ("nahrung",         44, 103, 653, 1093, 8),
    ("rohstoffe",       131, 192, 654, 1063, 4),
    ("waffen_material", 227, 288, 655, 1090, 8),
]

im = Image.open(SRC).convert("RGB")

for name, y0, y1, x0, x1, count in BLOCKS:
    width = x1 - x0
    step = width / count
    for i in range(count):
        cx0 = int(round(x0 + i * step))
        cx1 = int(round(x0 + (i + 1) * step))
        crop = im.crop((cx0, y0, cx1, y1 + 1))
        out_path = OUT_DIR / f"{name}_{i+1:02d}.png"
        crop.save(out_path)

print(f"Fertig. {sum(c for *_, c in BLOCKS)} Icons gespeichert in {OUT_DIR}/")
