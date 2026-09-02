"""
Stronghold Crusader HD - Live Ressourcen-Reader (Multiplayer/KI)
======================================================================
Portierte, für die App-Nutzung leicht angepasste Version von
shc_resource_reader.py (siehe dort für die vollständige Herleitungs-
Historie der einzelnen Adressen). Die Kern-Lesefunktionen sind unverändert
- nur write_overlay_json/write_overlay_file wurden in eine reine
Payload-Bau-Funktion (build_overlay_payload) plus dünne Datei-Wrapper
aufgeteilt, und connect() bekam einen verbose-Schalter, damit der
gepackte (fensterlose) Build nicht ins Leere druckt.
"""

import json
import re
import time

import pymem
import pymem.process

PROCESS_NAME = "Stronghold Crusader.exe"

# Basis-Adresse von Spieler 1 (fruit_basket, erstes Feld des Structs).
BASE_ADDR = 0x115FCBC

# Fester Byte-Abstand zwischen den Spieler-Slots.
PLAYER_STRIDE = 0x39F4

# SHC Extreme/HD erlaubt bis zu 8 Spieler-Slots (Mensch + KI zusammen).
PLAYER_INDEX_RANGE = range(0, 8)

# Ressourcen-Offsets relativ zur jeweiligen Spieler-Basis.
RESOURCE_OFFSETS = {
    "wood":           0x08,
    "hops":           0x0C,
    "stone":          0x10,
    "iron":           0x18,
    "pitch":          0x1C,
    "wheat":          0x24,
    "bread":          0x28,
    "cheese":         0x2C,
    "meat":           0x30,
    "apples":         0x34,
    "ale":            0x38,
    "gold":           0x3C,
    "flour":          0x40,
    "bows":           0x44,
    "crossbows":      0x48,
    "spears":         0x4C,
    "pikes":          0x50,
    "maces":          0x54,
    "swords":         0x58,
    "leather_armor":  0x5C,
    "metal_armor":    0x60,
    "troops_total":   0x6C,

    "unit_archer":      0x1BE8,  # Bogenschütze
    "unit_crossbowman": 0x1BEC,  # Armbrustschütze
    "unit_spearman":    0x1BF0,  # Speerträger
    "unit_pikeman":     0x1BF4,  # Pikenier
    "unit_maceman":     0x1BF8,  # Streitkolbenkämpfer
    "unit_swordsman":   0x1BFC,  # Schwertkämpfer
    "unit_knight":      0x1C00,  # Ritter
    "unit_laddercarrier": 0x1C04,  # Leiterträger
    "unit_tunneler":    0x1C0C,  # Tunnelbauer
    "unit_builder":     0x1C08,  # Baumeister

    "unit_arab_archer":    0x2558,  # Arabischer Bogenschütze
    "unit_horse_archer":   0x255C,  # Berittener Bogenschütze
    "unit_slave":          0x2560,  # Sklave
    "unit_slinger":        0x2568,  # Schleuderschütze
    "unit_fire_thrower":   0x256C,  # Feuerwerfer
    "unit_arab_swordsman": 0x2570,  # Arabischer Schwertkämpfer
    "unit_assassin":       0x2564,  # Assassine
}

# Popularity (Gesamtbeliebtheit) liegt VOR dem fruit_basket-Feld, Rohwert
# 10000 = 100%. Live per Increased-value-Scan über einen Monatswechsel
# gefunden und bestätigt (2026-09-01): der ursprüngliche Kandidat -0x40C
# war falsch (zeigte in leeren Slots fälschlich Speichermüll als 1-3% an);
# der neue Wert -0x470 stimmte exakt mit der Ingame-Anzeige überein (97%->
# 98% nach einem Monat mit ausschließlich "Steuern +1" als aktivem Faktor).
POPULARITY_OFFSET = -0x470

# Bevölkerung (aktuelle Einwohner) und die Kapazität (steigt um +8 pro
# gebautem Haus, Start bei 10) - im Spiel als "x/y" im Bevölkerungsbuch
# angezeigt. Live per Speicher-Scan gefunden und bestätigt (2/10 -> 5/18
# nach einem neuen Haus, exakt getroffen). Bei leeren Slots beide 0.
POPULATION_OFFSET = -0x458
POPULATION_CAPACITY_OFFSET = -0x45C

# Steuersatz als 0-indizierter Skalen-Wert (0 = "Großzügige Spende" / +7
# Beliebtheit ... 3 = "Keine Steuern" / +1 Beliebtheit, Standard ... 11 =
# "Grausamste Steuern" / -24 Beliebtheit). Live per Delta-Scan gefunden:
# mehrfach exakt +1 pro Steuerstufe nach oben und -2 nach einem 2er-Schritt
# nach unten bestätigt. Über alle 8 Slots verifiziert: KI-Slots liegen
# alle auf 3 (Standard "Keine Steuern"), der Mensch-Slot passend zum
# tatsächlich eingestellten Wert - zwei andere Kandidaten-Adressen zeigten
# bei KI-Slots überall 0 (kein gültiger Steuersatz) und wurden verworfen.
TAX_RATE_OFFSET = 0x1CB8

# Live-HP des Burgherren pro Slot - eigene Basis/Schrittweite, siehe
# read_player() für die Herleitung.
LORD_HP_BASE = 0x1388DA4
LORD_HP_STRIDE = 0x490

# Max-HP eines Burgherren = LORD_BASE_HP * Multiplikator je nach
# KI-Persönlichkeit (Feld "lord.StrengthMultiplier" in einem lokalen
# Community-.aic-Editor-Tool gefunden, KEIN offizielles Firefly-Feld - die
# echte vanilla.json der Spielinstallation kennt dieses Feld nicht). Die
# volle 16-Lord-Tabelle stammt aus einem Mehrheitskonsens über 14
# unabhängige Custom-AI-Mod-Exporte (jeder Mod überschreibt nur seinen
# einen Lord-Slot) - zwei Werte (Ratte, Wolf) zusätzlich live im Speicher
# bestätigt (100000*1.5=150000 für Wolf, exakt getroffen). Menschliche
# Spieler haben keinen dieser Namen im Roster-String und fallen auf den
# Standard-Multiplikator 1.0 zurück.
LORD_BASE_HP = 100000
LORD_STRENGTH_MULTIPLIER = {
    "rat": 0.5, "ratte": 0.5,
    "snake": 0.7, "schlange": 0.7,
    "pig": 1.0, "schwein": 1.0,
    "wolf": 1.5,
    "saladin": 1.4,
    "caliph": 1.0, "kalif": 1.0,
    "sultan": 0.9,
    "richard": 1.4,
    "frederick": 1.2, "friedrich": 1.2,
    "phillip": 0.8,
    "wazir": 1.1,
    "emir": 1.0,
    "nizar": 1.3,
    "sheriff": 1.1,
    "marshal": 1.1,
    "abbot": 1.2, "abt": 1.2,
}


def get_lord_strength_multiplier(display_name):
    """Sucht im (Roster-)Anzeigenamen nach einem bekannten KI-Charakter-
    Namen als eigenständigem Wort (Wortgrenzen-Match, kein roher Teilstring
    - sonst würden z.B. menschliche Spieler namens "Wolfgang" oder "Murat"
    fälschlich als Wolf/Ratte erkannt) und gibt dessen Stärke-Multiplikator
    zurück. Kein Treffer (z.B. menschlicher Spieler mit eigenem Namen) ->
    Standard-Multiplikator 1.0. Einzige Quelle dieser Matching-Logik - auch
    von strength_score.py genutzt, um den Substring-Bug nicht an einer
    zweiten Stelle zu wiederholen."""
    if not display_name:
        return 1.0
    lower = display_name.lower()
    for name, multiplier in LORD_STRENGTH_MULTIPLIER.items():
        if re.search(rf"\b{re.escape(name)}\b", lower):
            return multiplier
    return 1.0


def get_lord_max_hp(display_name):
    """LORD_BASE_HP skaliert mit get_lord_strength_multiplier()."""
    return int(LORD_BASE_HP * get_lord_strength_multiplier(display_name))


# Team-/Bündnis-Zugehörigkeit: KEIN Live-Feld gefunden (Stand 2026-09-01),
# trotz mehrerer breiter Korrelations-Scans (bis zu 160 KB Umkreis um die
# Spieler-Basis, mit 2-, 3- und 4-Team-Mustern, auch mit absichtlich
# gemischten KI-Personas um Zufallstreffer auszuschließen) UND einem
# separaten Scan der Namens-Roster-Tabelle. EIN scheinbarer Treffer
# (Offset +0x9218, 2x2x2x2-Teams) erwies sich im Gegentest mit 3er-Gruppen
# (3+3+2) als Zufall - siehe project_shc_overlay_status.md für die volle
# Historie. Team-Zuordnung im Overlay läuft deshalb ausschließlich über
# die manuelle team_assignment-Einstellung (config["team_assignment"]).

# Steuersatz -> Beliebtheits-Effekt ist KEIN Live-Speicherwert, sondern eine
# feste Tabelle im Spiel (ändert sich nie zur Laufzeit) - deshalb per
# manueller Ingame-Ablese-Tabelle hinterlegt statt per Speicher-Scan (ein
# Exact-Value-Scan nach einer Steuerstufen-Änderung lieferte 0 Treffer, weil
# es eben keine live geschriebene Adresse dafür gibt). Quelle: User hat alle
# 12 Stufen im Beliebtheits-Aufschlüsselungs-Dialog abgelesen (2026-09-01).
TAX_POPULARITY_EFFECT = {
    0: 7,    # Großzügige Spende
    1: 5,    # Große Spende
    2: 3,    # Kleine Spende
    3: 1,    # Keine Steuern
    4: -2,   # Niedrige Steuern
    5: -4,   # Moderate Steuern
    6: -6,   # Hohe Steuern
    7: -8,   # Gemeine Steuern
    8: -12,  # Wuchersteuern
    9: -16,  # Grausame Steuern
    10: -20,  # Grausamere Steuern
    11: -24,  # Grausamste Steuern
}

# Sammel-Zähler für BEWEGLICHE Belagerungsgeräte (Schild, Rammbock,
# Belagerungsturm, Katapult, Tribok, Feuerballiste zusammen). Siehe
# shc_resource_reader.py für die vollständige Herleitung.
SIEGE_MOVABLE_OFFSET = -0x114

# Live-Namens-Roster: fester globaler Zeiger auf die aktuelle 8-Slot-
# Namenstabelle des Matches, Reihenfolge = Spieler-Slot-Reihenfolge.
ROSTER_POINTER_ADDR = 0x004423A8
ROSTER_SLOT_SIZE = 90
ROSTER_NUM_SLOTS = 8

# Unit-Typen, für die es echte Live-Einzel-Offsets gibt (für die
# "Truppen-Aufschlüsselung"-Anzeige im Overlay) - alle RESOURCE_OFFSETS
# Keys, die mit "unit_" beginnen.
UNIT_TYPE_KEYS = [k for k in RESOURCE_OFFSETS if k.startswith("unit_")]

# Die 4 essbaren Nahrungsmittel, die im Kornspeicher gezählt werden (Mehl
# und Hopfen sind Zwischenprodukte für Brot/Bier und zählen NICHT mit).
# Bier zählt bewusst NICHT als Nahrung: es wird nicht von der Bevölkerung
# gegessen, sondern von der Schenke verbraucht und erzeugt dort - analog zur
# Religion - einen eigenen Beliebtheits-Bonus.
FOOD_KEYS = ["bread", "cheese", "meat", "apples"]


def _food_total(values):
    """Summe aller Nahrungsmittel (Kornspeicher-Gesamtwert). None, wenn
    keiner der Einzelwerte lesbar war."""
    present = [values.get(k) for k in FOOD_KEYS if values.get(k) is not None]
    return sum(present) if present else None


def connect(verbose: bool = True):
    """Verbindet mit dem laufenden Spielprozess. Gibt (pm, error_message)
    zurück - error_message ist None bei Erfolg."""
    try:
        pm = pymem.Pymem(PROCESS_NAME)
        if verbose:
            print(f"[OK] Verbunden mit {PROCESS_NAME}")
        return pm, None
    except pymem.exception.ProcessNotFound:
        msg = f"Prozess '{PROCESS_NAME}' nicht gefunden. Läuft das Spiel?"
        if verbose:
            print(f"[FEHLER] {msg}")
        return None, msg
    except pymem.exception.CouldNotOpenProcess:
        msg = "Zugriff verweigert - App muss als Administrator laufen."
        if verbose:
            print(f"[FEHLER] {msg}")
        return None, msg


def read_player(pm, player_index):
    """Liest alle bekannten Werte für einen Spieler-Slot aus."""
    base = BASE_ADDR + player_index * PLAYER_STRIDE
    values = {}
    for name, offset in RESOURCE_OFFSETS.items():
        try:
            values[name] = pm.read_int(base + offset)
        except Exception:
            values[name] = None

    try:
        raw_pop = pm.read_int(base + POPULARITY_OFFSET)
        values["popularity_percent"] = raw_pop / 100.0
    except Exception:
        values["popularity_percent"] = None

    try:
        values["siege_movable_total"] = pm.read_int(base + SIEGE_MOVABLE_OFFSET)
    except Exception:
        values["siege_movable_total"] = None

    try:
        values["population"] = pm.read_int(base + POPULATION_OFFSET)
    except Exception:
        values["population"] = None

    try:
        values["population_capacity"] = pm.read_int(base + POPULATION_CAPACITY_OFFSET)
    except Exception:
        values["population_capacity"] = None

    try:
        values["tax_rate"] = pm.read_int(base + TAX_RATE_OFFSET)
    except Exception:
        values["tax_rate"] = None
    values["tax_popularity_effect"] = TAX_POPULARITY_EFFECT.get(values["tax_rate"])

    # NOCH NICHT FUNKTIONAL (Stand 2026-09-01, zweiter Anlauf gescheitert).
    # LORD_HP_BASE + player_index*LORD_HP_STRIDE sah in einem Sandbox-Test
    # (8 isolierte, nacheinander gesetzte Treffer, sonst keine gleichzeitige
    # Kampfhandlung) überzeugend nach einem festen Pro-Spieler-Array aus -
    # war aber ein Trugschluss: es ist derselbe Ereignis-Ringpuffer wie bei
    # den Mönchen, und die 8 isolierten Treffer landeten nur zufällig in
    # Slot-Reihenfolge, weil nichts anderes gleichzeitig Ereignisse erzeugte.
    # In einem echten Match mit gleichzeitigem Hintergrundgeschehen (Bewegung,
    # KI etc.) zeigte diese Formel für ALLE Spieler denselben (veralteten/
    # generischen) Wert - live bestätigt falsch. Eine echte Umsetzung
    # braucht dieselbe Ereignis-Zähler-Technik wie poll_monk_events()
    # (Ereignis-Typ für "Schaden mit resultierender HP" identifizieren,
    # Bereich durchlaufen), nicht eine feste Adresse. Siehe
    # project_shc_overlay_status.md für die volle Historie.
    values["lord_hp"] = None

    return values


# Live-Ereignis-Log "Einheit fertig ausgebildet" - der einzige bisher
# gefundene Weg, Mönche pro Spieler zu zählen (es gibt KEIN festes
# Zählerfeld dafür im Spieler-Struct, siehe Projekt-Notizen: sehr
# gründlich mit 5+ unabhängigen Methoden gesucht). Reverse engineered per
# Cheat-Engine-Breakpoint auf troops_total -> Code-Pfad 0x567666 ->
# Kategorie-Dispatcher -> dieses Log. Ringpuffer mit festem Eintrags-
# Abstand; die Spielernummer-Konvention ist 1-8 (kein Spieler 0), analog zu
# CrusaderData. Live-verifiziert über mehrere Spieler (1,3,4) mit sauber
# isolierten Einzel-Bauten: Typ-ID 101 = Mönch, jedesmal exakt und korrekt
# dem bauenden Spieler zugeordnet. Belagerungsgeräte (Tribok/Rammbock/
# Katapult/Feuerballiste/sogar Turm-Balliste) zeigen hier alle dieselbe
# geteilte Typ-ID 6 - keine Einzelaufschlüsselung möglich, auch hier
# nicht (bestätigt über 5 verschiedene Typen).
EVENT_COUNTER_ADDR = 0x00EE0FC8
EVENT_STRIDE = 0x490
EVENT_PLAYER_NUM_ADDR = 0x013885E2
EVENT_TYPE_ID_ADDR = 0x0138880C
MONK_EVENT_TYPE_ID = 101

# Modul-weiter State (persistiert über mehrere poll_monk_events()-Aufrufe
# hinweg) - nötig, weil das Log ein Ringpuffer ist: wir müssen bei JEDEM
# Poll die seit dem letzten Mal neu hinzugekommenen Einträge ansehen,
# sonst gehen Ereignisse verloren, sobald der Puffer sie überschreibt.
_last_event_counter = None
_monk_counts_by_player_num = {}


def reset_monk_tracking():
    """Setzt die Mönch-Zählung komplett zurück (neue Baseline beim nächsten
    poll_monk_events-Aufruf, alle bisherigen Pro-Spieler-Zählungen verworfen).
    Muss bei jedem frischen Verbinden mit dem Spielprozess aufgerufen werden
    (siehe worker.py::_ensure_connected) - sonst überleben die Zählungen
    einen Prozess-Neustart und zeigen falsche Alt-Werte."""
    global _last_event_counter
    _last_event_counter = None
    _monk_counts_by_player_num.clear()


def poll_monk_events(pm):
    """Muss bei JEDEM Poll-Tick aufgerufen werden (nicht nur bei Bedarf!),
    damit keine Ringpuffer-Einträge verloren gehen. Zählt neue Mönche
    (Typ-ID 101) seit dem letzten Aufruf pro Spielernummer (1-8) mit.
    Der erste Aufruf nach Tool-Start (oder nach reset_monk_tracking()) setzt
    nur die Baseline (die Vergangenheit im Ringpuffer könnte längst
    überschriebene, nicht mehr gültige Einträge enthalten - deshalb wird ab
    Tool-Start gezählt, nicht rückwirkend)."""
    global _last_event_counter
    try:
        counter = pm.read_int(EVENT_COUNTER_ADDR)
    except Exception:
        return

    if _last_event_counter is None:
        _last_event_counter = counter
        return
    if counter == _last_event_counter:
        return
    if counter < _last_event_counter:
        # Zähler ist gesunken - Annahme: die Ereignis-Tabelle wurde
        # zurückgesetzt (z.B. neues Match im selben, weiterlaufenden
        # Spielprozess gestartet), alte Pro-Spieler-Zählung ist damit
        # ungültig geworden.
        # UNVERIFIZIERT (Stand 2026-09-02, kein Live-Spiel zum Testen
        # verfügbar): ob EVENT_COUNTER_ADDR bei einem Match-Neustart im
        # selben Prozess tatsächlich sinkt, ist nicht bestätigt. Zählt er
        # stattdessen über Match-Grenzen hinweg einfach weiter hoch, greift
        # dieser Zweig nie - dann bleibt der ursprüngliche Bug (alte
        # Mönch-Zahlen überleben einen Match-Neustart in derselben Session)
        # bestehen und bräuchte ein anderes Signal (z.B. game-state-Events).
        _monk_counts_by_player_num.clear()
        _last_event_counter = counter
        return

    # WICHTIG: der Zähler zeigt immer den NÄCHSTEN NOCH LEEREN Slot (noch
    # nicht geschrieben, liest sich als Spieler=0/Typ-ID=0) - Einträge
    # 0..counter-1 sind bereits gültig geschrieben. Der alte gemerkte
    # Zähler-Wert selbst ist also schon der erste NEUE Index, nicht der
    # letzte alte (off-by-one, live per Vollprotokoll-Vergleich gefunden:
    # ohne dieses "ohne +1" wurde der erste Mönch nach jedem Checkpoint
    # verschluckt).
    for idx in range(_last_event_counter, counter):
        try:
            type_id = pm.read_short(EVENT_TYPE_ID_ADDR + idx * EVENT_STRIDE)
            if type_id == MONK_EVENT_TYPE_ID:
                player_num = pm.read_short(EVENT_PLAYER_NUM_ADDR + idx * EVENT_STRIDE)
                _monk_counts_by_player_num[player_num] = _monk_counts_by_player_num.get(player_num, 0) + 1
        except Exception:
            pass

    _last_event_counter = counter


def get_monks_trained(player_index):
    """player_index: 0-7 (unsere Slot-Konvention). Das Ereignis-Log nutzt
    intern 1-8 (kein Spieler 0), also +1 zur Umrechnung. Gibt die Anzahl
    seit Tool-Start trainierter Mönche zurück (kein Live-Bestand - stirbt
    ein Mönch, sinkt dieser Wert NICHT, siehe Docstring von
    poll_monk_events)."""
    return _monk_counts_by_player_num.get(player_index + 1, 0)


def is_active_slot(values):
    """Ein Slot gilt als aktiv belegt, wenn eine Burg/ein Burgherr platziert
    wurde. Population_capacity ist dafür der zuverlässigste Indikator: jeder
    echte Spieler-Slot startet mit Kapazität >=10, während ein wirklich
    leerer Slot immer exakt 0 zeigt. Andere Felder (Gold, Truppen, sogar
    popularity_percent) können in ungenutzten Slots Speichermüll von einem
    früheren, größeren Match enthalten und sind daher NICHT zuverlässig
    (live beobachtet: leere Slots 6+7 in einem 6-Spieler-Match zeigten
    korrekt population_capacity=0, aber trotzdem popularity_percent=1-3%)."""
    return bool(values.get("population_capacity"))


def read_all_players(pm):
    """Liest alle Slots in PLAYER_INDEX_RANGE, gibt Liste von (index, values) zurück."""
    return [(i, read_player(pm, i)) for i in PLAYER_INDEX_RANGE]


def slot_label(i):
    # Kein "Du" mehr für Slot 0: per UCP3 kann auch Spieler 1 durch eine KI
    # ersetzt werden (reine KI-vs-KI-Zuschauer-Matches), Slot 0 ist also
    # nicht mehr garantiert der Mensch. Rein numerische, 1-indexierte
    # Beschriftung statt einer falschen Annahme.
    return f"Spieler {i + 1}"


def _read_roster_slot_string(pm, addr):
    try:
        buf = pm.read_bytes(addr, ROSTER_SLOT_SIZE)
    except Exception:
        return ""
    end = buf.find(b"\x00")
    if end == -1:
        end = len(buf)
    return buf[:end].decode("cp1252", errors="replace")


def read_roster_names(pm):
    """Liest die aktuellen Anzeige-Namen live über den globalen Roster-
    Zeiger. Gibt dict slot->name zurück; leer bei Fehlern (z.B. noch keine
    Karte geladen), dann fällt die Anzeige auf slot_label() zurück."""
    try:
        roster_base = pm.read_int(ROSTER_POINTER_ADDR)
        if roster_base == 0:
            return {}
    except Exception:
        return {}

    names = {}
    for i in range(ROSTER_NUM_SLOTS):
        name = _read_roster_slot_string(pm, roster_base + i * ROSTER_SLOT_SIZE)
        if name:
            names[i] = name
    return names


def build_overlay_payload(all_values, lord_labels, attack_status=None, display=None):
    """Baut die JSON-Payload fürs Overlay - reine Funktion, kein Datei-I/O.

    all_values: Liste von (slot_index, values_dict), wie von read_all_players().
    lord_labels: dict slot->name, wie von read_roster_names().
    attack_status: optionales dict slot->{"status", "base", "rand", ...}.
    display: optionales Config-"display"-dict, um zu steuern welche Felder
             ausgegeben werden (aktuell werden alle bekannten Felder immer
             mitgeschickt - overlay.html filtert selbst anhand der Toggles;
             der Parameter ist hier für zukünftige serverseitige Filterung
             vorbereitet, wird aber momentan ignoriert).
    """
    attack_status = attack_status or {}
    players = []
    for i, values in all_values:
        if not is_active_slot(values):
            continue
        label = lord_labels.get(i, slot_label(i))
        lord_max_hp = get_lord_max_hp(label)
        entry = {
            "slot": i,
            "label": label,
            "gold": values.get("gold"),
            "wood": values.get("wood"),
            "stone": values.get("stone"),
            "iron": values.get("iron"),
            "troops": values.get("troops_total"),
            "siege_movable_total": values.get("siege_movable_total"),
            "popularity": values.get("popularity_percent"),
            "bread": values.get("bread"),
            "cheese": values.get("cheese"),
            "meat": values.get("meat"),
            "apples": values.get("apples"),
            "ale": values.get("ale"),
            "pitch": values.get("pitch"),
            "hops": values.get("hops"),
            "wheat": values.get("wheat"),
            "flour": values.get("flour"),
            "population": values.get("population"),
            "population_capacity": values.get("population_capacity"),
            "tax_rate": values.get("tax_rate"),
            "tax_popularity_effect": values.get("tax_popularity_effect"),
            "monks_trained": get_monks_trained(i),
            "lord_hp": values.get("lord_hp"),
            "lord_max_hp": lord_max_hp,
            "lord_hp_percent": (
                round(100 * values["lord_hp"] / lord_max_hp) if values.get("lord_hp") is not None else None
            ),
            "food_total": _food_total(values),
            "bows": values.get("bows"),
            "crossbows": values.get("crossbows"),
            "spears": values.get("spears"),
            "pikes": values.get("pikes"),
            "maces": values.get("maces"),
            "swords": values.get("swords"),
            "leather_armor": values.get("leather_armor"),
            "metal_armor": values.get("metal_armor"),
            "is_you": i == 0,
            "units": {k: values.get(k) for k in UNIT_TYPE_KEYS},
        }
        status_entry = attack_status.get(i)
        if status_entry is not None:
            entry["attack_status"] = status_entry.get("status")
            entry["attack_base"] = status_entry.get("base")
        players.append(entry)
    return {"players": players, "updated_at": time.time()}


# --- Nur für CLI-Debug (python app/reader.py), nicht von der App genutzt ---

def write_overlay_json(payload, path="shc_overlay.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def write_overlay_file(payload, path="shc_overlay.txt"):
    lines = []
    for p in payload["players"]:
        pop = p.get("popularity")
        pop_str = f"{pop:.1f}%" if pop is not None else "?"
        lines.append(
            f"{p['label']}: Gold {p.get('gold')} | Holz {p.get('wood')} | "
            f"Stein {p.get('stone')} | Eisen {p.get('iron')} | "
            f"Truppen {p.get('troops')} | Popularity {pop_str}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    pm, err = connect()
    if pm is None:
        print(f"[FEHLER] {err}")
        return

    print("Starte Live-Auslesung (Strg+C zum Beenden)...\n")
    try:
        while True:
            all_values = read_all_players(pm)
            lord_labels = read_roster_names(pm)
            poll_monk_events(pm)
            payload = build_overlay_payload(all_values, lord_labels)

            parts = [
                f"[{p['label']}] Gold:{p['gold']:>5} Holz:{p['wood']:>4} "
                f"Stein:{p['stone']:>4} Eisen:{p['iron']:>4} Truppen:{p['troops']:>3}"
                for p in payload["players"]
            ]
            print(" | ".join(parts), flush=True)

            write_overlay_file(payload)
            write_overlay_json(payload)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
