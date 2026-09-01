# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-Spec fuer den gepackten Endanwender-Build.
#
# Bauen (aus dem Projekt-Wurzelverzeichnis):
#   pyinstaller build/shc_live_stats.spec --clean
#
# Ergebnis liegt danach in dist/SHCLiveStats.exe (Release, fensterlos) und
# dist/SHCLiveStatsDebug.exe (mit Konsole, fuer Fehlersuche).

import os

block_cipher = None

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
APP_ICON = os.path.join(APP_DIR, "assets", "icons", "app_icon.ico")

a = Analysis(
    [os.path.join(APP_DIR, "main.py")],
    pathex=[APP_DIR],
    binaries=[],
    datas=[
        (os.path.join(APP_DIR, "assets", "overlay.html"), "assets"),
        (os.path.join(APP_DIR, "assets", "settings.html"), "assets"),
        (os.path.join(APP_DIR, "assets", "overview.html"), "assets"),
        (os.path.join(APP_DIR, "assets", "overview_settings.html"), "assets"),
        (os.path.join(APP_DIR, "assets", "icons"), "assets/icons"),
        (os.path.join(APP_DIR, "assets", "backgrounds"), "assets/backgrounds"),
    ],
    hiddenimports=["pymem", "pymem.process", "pystray", "PIL", "PIL._tkinter_finder"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Release-Build: kein Konsolenfenster, fragt automatisch nach Administrator-
# Rechten (das Spiel selbst laeuft elevated, ohne das schlaegt pymem fehl).
exe_release = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SHCLiveStats",
    console=False,
    uac_admin=True,
    onefile=True,
    icon=APP_ICON,
)

# Debug-Build: identisch, aber mit Konsolenfenster fuer Fehlersuche (eigene
# EXE, da PyInstaller console-Flag nicht innerhalb eines Analysis-Objekts
# nachtraeglich umschaltbar ist).
exe_debug = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SHCLiveStatsDebug",
    console=True,
    uac_admin=True,
    onefile=True,
    icon=APP_ICON,
)
