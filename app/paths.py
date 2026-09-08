"""Pfad-Hilfsfunktionen für Dev- und PyInstaller-Betrieb.

Im gepackten Build liegen die Bundled-Assets (assets/, icons/) in einem
temporären Ordner, den PyInstaller unter sys._MEIPASS bereitstellt. Im
Dev-Betrieb (python app/main.py) liegen sie einfach relativ zu dieser Datei.
"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(*parts) -> Path:
    """Liefert den absoluten Pfad zu einer gebündelten Ressource
    (z.B. resource_path("assets", "overlay.html"))."""
    if is_frozen():
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def app_data_dir() -> Path:
    """Persistenter, immer beschreibbarer Ordner für Konfiguration/Logo -
    liegt PORTABEL im Tool-Ordner selbst (Unterordner "config" direkt neben
    der laufenden exe bzw. im Projekt-Root im Dev-Betrieb), nicht mehr unter
    %APPDATA%. Dadurch bleibt die Config erhalten, wenn der komplette
    Tool-Ordner verschoben oder z.B. auf einen anderen Rechner kopiert wird.

    Wichtig bei PyInstaller-Onefile-Builds: NICHT sys._MEIPASS verwenden
    (das ist ein bei JEDEM Start neu angelegter, temporärer Extraktions-
    Ordner, der beim Beenden wieder verschwindet) - sondern sys.executable,
    der stabile, tatsächliche Pfad zur laufenden exe-Datei selbst.

    Einmalige Migration: falls hier noch keine config.json liegt, aber die
    alte Version unter %APPDATA%/SHCLiveStats/ existiert (Stand vor
    2026-09-08), wird deren Inhalt einmalig hierher kopiert, damit
    bestehende Einstellungen (Farben, Presets, Logo etc.) nicht verloren
    gehen."""
    import os
    import shutil

    if is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    d = base / "config"
    d.mkdir(parents=True, exist_ok=True)

    if not (d / "config.json").exists():
        old_base = os.environ.get("APPDATA")
        if old_base:
            old_d = Path(old_base) / "SHCLiveStats"
            if old_d.is_dir() and (old_d / "config.json").exists():
                for item in old_d.iterdir():
                    if item.is_file():
                        try:
                            shutil.copy2(item, d / item.name)
                        except OSError:
                            pass

    return d
