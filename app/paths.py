"""Pfad-Hilfsfunktionen fuer Dev- und PyInstaller-Betrieb.

Im gepackten Build liegen die Bundled-Assets (assets/, icons/) in einem
temporaeren Ordner, den PyInstaller unter sys._MEIPASS bereitstellt. Im
Dev-Betrieb (python app/main.py) liegen sie einfach relativ zu dieser Datei.
"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(*parts) -> Path:
    """Liefert den absoluten Pfad zu einer gebuendelten Ressource
    (z.B. resource_path("assets", "overlay.html"))."""
    if is_frozen():
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def app_data_dir() -> Path:
    """Persistenter, immer beschreibbarer Ordner fuer Konfiguration/Logo
    (ueberlebt exe-Umzuege und PyInstaller-Onefile-Neuextraktion)."""
    import os

    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home())
    d = Path(base) / "SHCLiveStats"
    d.mkdir(parents=True, exist_ok=True)
    return d
