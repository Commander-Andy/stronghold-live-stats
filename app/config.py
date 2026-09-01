"""Konfigurations-Schema, Laden/Speichern.

Liegt unter %APPDATA%\\SHCLiveStats\\config.json - pro Benutzer, immer
beschreibbar, überlebt exe-Umzüge und PyInstaller-Onefile-Neuextraktion
(im Gegensatz zu sys._MEIPASS oder einem Installationsort unter
Program Files, der ohne Admin-Rechte evtl. nicht beschreibbar ist).
"""

import json
import os
import threading
from pathlib import Path

from paths import app_data_dir

SCHEMA_VERSION = 17

# Flags, die tatsächlich als einzelne Icons im Overlay gerendert werden
# (siehe STAT_DEFS in overlay.html) - für die einmalige line_count-Migration
# unten. show_troop_breakdown zählt NICHT mit (eigener Bereich, kein Icon
# in dieser Zeilen-Grid-Logik).
_STAT_DISPLAY_FLAGS = [
    "show_gold", "show_wood", "show_stone", "show_iron",
    "show_food_total", "show_bread", "show_cheese", "show_meat", "show_apples",
    "show_ale", "show_pitch", "show_hops", "show_wheat", "show_flour",
    "show_troops_total", "show_siege_movable",
    "show_bows", "show_crossbows", "show_spears", "show_pikes", "show_maces", "show_swords",
    "show_leather_armor", "show_metal_armor",
    "show_popularity", "show_population", "show_tax_rate", "show_monks_trained",
    "show_lord_hp",
]

DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "aic_file_path": None,
    "process_name": "Stronghold Crusader.exe",
    "http_port": 8765,
    "poll_interval_ms": 500,
    "attack_poll_interval_ms": 1000,
    # Wie lange (Sekunden) weder Einstellungs-Seite noch OBS aktiv gewesen
    # sein müssen, bevor sich die App von selbst beendet. 0 = nie automatisch
    # beenden (nur noch über "Beenden" im Tray-Menü möglich).
    "idle_shutdown_seconds": 60,
    "display": {
        "show_gold": True,
        "show_wood": True,
        "show_stone": True,
        "show_iron": True,
        "show_troops_total": True,
        "show_troop_breakdown": False,
        "show_siege_movable": False,
        "show_popularity": True,
        # Aktuelle Einwohnerzahl / Kapazität, im Overlay als "x/y" (wie im
        # Bevölkerungsbuch) angezeigt.
        "show_population": False,
        # Aktueller Steuersatz (0=Großzügige Spende ... 3=Keine Steuern,
        # Standard ... 11=Grausamste Steuern) - im Overlay als Stufe 1-12
        # angezeigt.
        "show_tax_rate": False,
        # EXPERIMENTELL: seit Tool-Start trainierte Mönche pro Spieler -
        # KEIN Live-Bestand (stirbt ein Mönch, sinkt die Zahl nicht), da es
        # kein festes Zählerfeld dafür gibt - wird aus einem kleinen,
        # sich ständig überschreibenden Ereignis-Stapel mitgezählt (siehe
        # reader.poll_monk_events()). Zählung beginnt erst, sobald das Tool
        # läuft (kein rückwirkendes Zählen möglich). Kann in seltenen
        # Fällen (sehr viele gleichzeitige Bau-/Verlust-Ereignisse aller
        # Spieler binnen ~150ms) einzelne Mönche verpassen.
        "show_monks_trained": False,
        # NOCH NICHT FUNKTIONAL (Stand 2026-09-01) - Live-HP des Burgherren.
        # Basis-HP, Multiplikator-Tabelle und die grobe Speicherregion sind
        # bekannt (siehe reader.py), aber die naive feste Adress-Formel
        # erwies sich in einem echten Match als falsch (Ereignis-Ringpuffer-
        # Trugschluss, siehe project_shc_overlay_status.md) - braucht noch
        # dieselbe Ereignis-Zähler-Technik wie show_monks_trained.
        "show_lord_hp": False,
        "highlight_attack_status": True,
        # Zeigt den Angriffs-Status ("baut auf" etc.) zusätzlich als
        # Text-Badge mittig über der Spielerzeile - aus, da das im OBS-
        # Overlay zusätzlichen Platz braucht (der farbige Rahmen/Puls von
        # highlight_attack_status bleibt davon unberührt).
        "show_attack_badge": False,
        "show_logo": False,
        "logo_path": None,
        # Zeigt Platzhalter-Spieler mit 0/"-"-Werten an, solange kein Spiel
        # verbunden ist - damit man Ausrichtung/Farben/Icons schon vorab
        # einstellen kann, ohne extra ein Match starten zu müssen.
        "show_demo_data": True,
        # Nahrung (Kornspeicher-Inhalt) - Summe + Einzelwerte, alle
        # standardmäßig aus, um das Overlay nicht zuzumüllen.
        "show_food_total": False,
        "show_bread": False,
        "show_cheese": False,
        "show_meat": False,
        "show_apples": False,
        # Bier zählt NICHT zur Nahrung (show_food_total) - es wird von der
        # Schenke verbraucht und erzeugt dort einen eigenen Beliebtheits-
        # Bonus, ähnlich wie Religion. Wird zusammen mit Pech/Hopfen als
        # "Rohstoff" einsortiert (siehe Kategorien in den Einstellungen).
        "show_ale": False,
        "show_pitch": False,
        "show_hops": False,
        # Getreide und Mehl (Zwischenprodukt für Brot) - zählen NICHT zur
        # Nahrung gesamt (show_food_total), genau wie Hopfen/Pech als
        # "Rohstoff" einsortiert.
        "show_wheat": False,
        "show_flour": False,
        # Waffen-/Rüstungslager (hergestellt, aber noch nicht an Truppen
        # ausgegeben).
        "show_bows": False,
        "show_crossbows": False,
        "show_spears": False,
        "show_pikes": False,
        "show_maces": False,
        "show_swords": False,
        "show_leather_armor": False,
        "show_metal_armor": False,
    },
    "layout": {
        "right_offset_px": 110,
        # War bisher fest auf 6px verdrahtet (kein Bevölkerungsbuch o.ä. auf
        # der linken Seite, das man umgehen müsste) - jetzt trotzdem
        # einstellbar, z.B. um die Seiten symmetrisch auszurichten.
        "left_offset_px": 6,
        "bottom_offset_px": 6,
        "row_gap_px": 4,
        # "split" = wie bisher hälftig links/rechts (nach Slot-Reihenfolge),
        # "left"/"right" = alle Spieler auf einer Seite (z.B. für 1vN-
        # Konstellationen), "teams" = anhand von team_assignment sortiert
        # (eigenes Team links, alle anderen rechts) - fällt auf "split"
        # zurück, wenn Slot 0 keinem Team zugeordnet ist.
        "side_mode": "split",
        # "rgb" = frei einstellbare Farben (Standardverhalten, siehe Farben
        # unten). "shc_single" = EIN gemeinsames Pergament liegt hinter allen
        # Rahmen einer Seite (wie eine durchgehende Schriftrolle).
        # "shc_multi" = jeder einzelne Rahmen bekommt sein eigenes Pergament.
        # Bei shc_single/shc_multi gewinnt das Pergament immer gegenüber
        # individuell hochgeladenen Pro-Spieler-Hintergrundbildern; Rahmen-
        # farben (Standard/Du/Angriff, auch pro Spieler) bleiben unverändert
        # aktiv und werden oben auf dem Pergament angezeigt.
        "theme": "rgb",
        "border_color": "#c6a15b",
        # Wie viele Werte NEBENEINANDER in eine Zeile passen - legt die
        # BREITE der Karte fest (Breite = diese Zahl * stat_width_px).
        "stats_per_line": 6,
        # Feste Anzahl Zeilen - legt die HÖHE der Karte fest. Beide Werte
        # zusammen (stats_per_line x line_count) sind eine feste Kapazität,
        # die sich NIE automatisch an die Anzahl aktivierter Werte anpasst -
        # damit sich die Kartengröße nicht verändert, nur weil während
        # eines laufenden Streams mal ein Wert an-/abgeschaltet wird. Passt
        # nicht alles rein, werden überzählige Werte einfach nicht
        # angezeigt (siehe line_distribution für die Reihenfolge).
        "line_count": 1,
        # "category" = Werte werden nach Rubrik geordnet einsortiert
        # (Rohstoffe, Nahrung, Waffen&Rüstung, Militär, Sonstiges) - ein
        # bestimmter Wert landet dadurch immer an einer vorhersagbaren
        # Stelle, unabhängig davon, was sonst noch an-/ausgeschaltet ist.
        # "even" = Werte werden schlicht der Reihe nach (Anzeige-Reihenfolge)
        # gleichmäßig auf die Zeilen aufgeteilt.
        "line_distribution": "category",
        "icon_size_px": 16,
        # Höhe einer Werte-Zeile in px - UNABHÄNGIG von icon_size_px, damit
        # z.B. etwas Luft über/unter kleinen Icons entstehen kann, ohne die
        # Icons selbst größer zu machen. Bleibt IMMER exakt so hoch (siehe
        # .stats-line in overlay.html), egal was an-/ausgeschaltet ist.
        "line_height_px": 16,
        # GESAMTBREITE einer Werte-Zeile in px (nicht Breite pro einzelnem
        # Icon!) - bleibt IMMER exakt so breit, egal was an-/ausgeschaltet
        # ist. "Werte pro Zeile" teilt diese feste Breite in entsprechend
        # viele gleich große Felder auf (mehr Werte = kleinere Icons/Zahlen).
        "stat_width_px": 312,
        # Farbe für dein eigenes Feld (Spieler 1 / Slot 0) bzw. für die
        # Angriffs-Warnung - eigene Farben, unabhängig von border_color.
        "you_color": "#78c8ff",
        "attack_color": "#e6463c",
        # Ob das Angriffs-Feld zusätzlich pulsierend leuchten soll (CSS-
        # Animation), statt nur einen statischen roten Rahmen zu zeigen.
        "attack_pulse": True,
        # Textfarbe für Beschriftung/Werte - v.a. bei SHC Single/Multi
        # nützlich, wenn das helle Pergament mit dem hellen Standardtext
        # (fast) verschmilzt und dunklerer Text besser lesbar wäre.
        "text_color": "#f2e9d8",
    },
    "logo": {
        "corner": "bottom-left",  # bottom-left | bottom-right | top-left | top-right
        "offset_x_px": 6,
        "offset_y_px": 6,
        "width_px": 90,
    },
    # Individuelle Rahmen-/Hintergrundfarbe pro Spieler-Slot (0=Spieler 1..7=Spieler 8).
    # null = übernimmt die globale Standardfarbe (layout.border_color bzw.
    # die Standard-Zeilenfarbe). Ein individuelles Hintergrundbild pro Slot
    # wird separat als Datei verwaltet (kein Pfad hier nötig) - siehe
    # server.py Routen /player-bg/<slot>.png.
    "player_colors": {str(i): {"border_color": None, "bg_color": None} for i in range(8)},
    # Manuelle Team-Zuordnung pro Slot (0=Spieler 1..7=Spieler 8): null = keinem Team
    # zugeordnet, sonst eine beliebige Team-Nummer (z.B. 1/2). Es gibt KEINE
    # live ausgelesene Team-/Bündnis-Adresse im Spielspeicher (ausführlich
    # gesucht, siehe project_shc_overlay_status.md) - der User trägt das pro
    # Match von Hand ein. Wird von BEIDEN Einstellungsseiten (Overlay +
    # Übersicht) gemeinsam genutzt (kein separates "overview"-Duplikat,
    # siehe server.py) - Team-Zugehörigkeit ist Match-Realität, kein
    # Design-Unterschied zwischen Overlay und Übersicht. "teams" als
    # side_mode (layout.side_mode / overview.side_mode) sortiert dann anhand
    # dessen links (eigenes Team, inkl. Slot 0) vs. rechts (alle anderen).
    "team_assignment": {str(i): None for i in range(8)},
    # 5 Speicherplätze für komplette, selbst benannte Anzeige-Setups (nicht
    # nur welche Werte, sondern das volle Aussehen: Design, Farben, Zeilen/
    # Breite, Logo, Pro-Spieler-Farben). null = Platz noch leer. Jeder Platz
    # ist ein dict {"name": str, "display": {...}, "layout": {...},
    # "logo": {...}, "player_colors": {...}} - eine vollständige Kopie
    # dieser vier Config-Bereiche zum Zeitpunkt des Speicherns.
    "saved_layouts": [None, None, None, None, None],
    # Eigene, vom OBS-Overlay komplett unabhängige Einstellungen für den
    # Übersichtsmodus (overview.html) - der Streamer soll dort z.B. mehr
    # Details anzeigen können, ohne das schlanke Stream-Overlay zu
    # verändern, und umgekehrt.
    "overview": {
        "show_gold": True,
        "show_wood": True,
        "show_stone": True,
        "show_iron": True,
        "show_troops_total": True,
        "show_troop_breakdown": False,
        "show_siege_movable": False,
        "show_popularity": True,
        # Aktuelle Einwohnerzahl / Kapazität, im Overlay als "x/y" (wie im
        # Bevölkerungsbuch) angezeigt.
        "show_population": False,
        "show_tax_rate": False,
        "show_monks_trained": False,
        "show_lord_hp": False,
        "highlight_attack_status": True,
        "show_demo_data": True,
        "show_food_total": False,
        "show_bread": False,
        "show_cheese": False,
        "show_meat": False,
        "show_apples": False,
        "show_ale": False,
        "show_pitch": False,
        "show_hops": False,
        "show_wheat": False,
        "show_flour": False,
        "show_bows": False,
        "show_crossbows": False,
        "show_spears": False,
        "show_pikes": False,
        "show_maces": False,
        "show_swords": False,
        "show_leather_armor": False,
        "show_metal_armor": False,
        # Wie layout.side_mode im Overlay ("teams" nutzt das TOP-LEVEL
        # team_assignment, das sich Overlay und Übersicht teilen).
        "side_mode": "split",
        # Wie layout.theme im Overlay, aber komplett unabhängig davon.
        "theme": "rgb",
        "row_gap_px": 14,
        # Icon-Größe für die Truppen-Aufschlüsselung (show_troop_breakdown)
        # - unabhängig von der Größe der Haupt-Stat-Icons, da hier bis zu
        # 17 Icons gleichzeitig in einer Zeile stehen können.
        "breakdown_icon_size_px": 22,
        "you_color": "#78c8ff",
        "attack_color": "#e6463c",
        "attack_pulse": True,
        "text_color": "#f2e9d8",
        # 5 Speicherplätze wie oben, aber komplett unabhängig - hier ist
        # ein Platz einfach eine vollständige Kopie dieses ganzen (flachen)
        # "overview"-Bereichs zum Zeitpunkt des Speicherns.
        "saved_layouts": [None, None, None, None, None],
    },
}

_lock = threading.Lock()


def config_path() -> Path:
    return app_data_dir() / "config.json"


def _deep_merge_defaults(defaults, loaded):
    """Füllt fehlende Schlüssel aus defaults auf, damit alte config.json-
    Dateien nach einem Update nicht mit KeyError abbrechen."""
    result = dict(defaults)
    for key, value in loaded.items():
        if key in defaults and isinstance(defaults[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_defaults(defaults[key], value)
        else:
            result[key] = value
    return result


def _migrate_line_count(cfg: dict) -> int:
    """Einmalige Migration für Configs von vor der line_count-Einführung:
    errechnet, wie viele Zeilen der bisherige automatische Umbruch (Anzahl
    aktivierter Werte / stats_per_line, aufgerundet) ergeben hätte - damit
    niemand beim Update plötzlich schon aktivierte Werte verliert. Danach
    ist line_count ein rein manueller Wert wie jeder andere Regler."""
    display = cfg.get("display", {})
    active = sum(1 for flag in _STAT_DISPLAY_FLAGS if display.get(flag))
    per_line = max(1, cfg.get("layout", {}).get("stats_per_line", 6))
    return max(1, -(-active // per_line))  # ceil-Division ohne math-Import


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        save_config(cfg)
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    needs_migration = "line_count" not in loaded.get("layout", {})
    # Einmalige Migration für Configs von vor Schema v17: stat_width_px war
    # bis dahin die Breite EINES einzelnen Wertes (Icon+Zahl), ab v17 ist es
    # die GESAMTBREITE einer ganzen Zeile - ohne Umrechnung würde ein
    # bestehender Wert wie 52 plötzlich als winzige Gesamtbreite gelten
    # statt wie bisher pro Icon. Alter effektiver Gesamtwert = alte
    # Icon-Breite * stats_per_line.
    needs_width_migration = loaded.get("schema_version", 0) < 17
    merged = _deep_merge_defaults(DEFAULT_CONFIG, loaded)
    if needs_width_migration:
        old_width = loaded.get("layout", {}).get("stat_width_px")
        per_line = max(1, loaded.get("layout", {}).get("stats_per_line", 6))
        if old_width is not None:
            merged["layout"]["stat_width_px"] = old_width * per_line
        needs_migration = True
    if needs_migration:
        if "line_count" not in loaded.get("layout", {}):
            merged["layout"]["line_count"] = _migrate_line_count(merged)
        merged["schema_version"] = SCHEMA_VERSION
        save_config(merged)
    return merged


def save_config(cfg: dict) -> None:
    path = config_path()
    with _lock:
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def merge_and_save(current: dict, patch: dict) -> dict:
    """Wendet ein (möglicherweise partielles) Update aus der Settings-UI an
    und speichert das Ergebnis."""
    merged = _deep_merge_defaults(current, patch)
    merged["schema_version"] = SCHEMA_VERSION
    save_config(merged)
    return merged
