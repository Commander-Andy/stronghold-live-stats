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
import pymem.memory
import pymem.process

PROCESS_NAME_HD = "Stronghold Crusader.exe"
PROCESS_NAME_EXTREME = "Stronghold_Crusader_Extreme.exe"
# Rueckwaerts-kompatibler Name (frueher die einzige unterstuetzte Version) -
# von aussen weiterhin lesbar, aber nicht mehr an connect() beteiligt.
PROCESS_NAME = PROCESS_NAME_HD

# HD ("Stronghold Crusader.exe") und Extreme ("Stronghold_Crusader_Extreme.exe")
# sind NACHWEISLICH unterschiedliche Binaries (unterschiedliche Dateigroesse
# UND Hash) mit komplett eigenem Speicherlayout - HDs Adressen lesen bei
# Extreme durchgehend -1/0/Muell und umgekehrt (live getestet 2026-08-26 und
# 2026-09-05). Alle vier Extreme-Werte unten wurden 2026-09-05 LIVE gegen
# den echten Prozess bestaetigt (nicht nur aus einer Quelle uebernommen):
#  - base_addr: per Exact-Value-Scan (Cheat-Engine-Stil) auf den echten,
#    vom Nutzer genannten Goldstand gefunden, dann als echtes Array
#    verifiziert (2 unterschiedliche aktive Spieler mit eigenen, plausiblen
#    Gold/Holz/Stein/Truppen-Werten - kein "nur der Mensch"-Fallstrick).
#  - object_table_base: aus dem GitHub-Repo CIO61/rebalancer
#    (addresses.lua, "Extreme"-Wert von unit_array_base_addr) uebernommen,
#    live bestaetigt ueber denselben Typ-ID-/player_num-Zensus wie bei HD
#    (plausible Einheiten-Typen 0-77, plausible player_num 0-7) UND ueber
#    das Burgherr-HP-Verfahren (zwei Lords mit sauberen, runden Vielfachen
#    der Basis-HP 150000 gefunden).
#  - diplomatic_group_array_addr: ueber denselben relativen Byte-Abstand
#    zu base_addr wie bei HD extrapoliert, live plausibel (zwei aktive
#    Solo-Spieler mit unterschiedlichen Gruppennummern, Rest 0) - NOCH NICHT
#    mit einem echten Team-Match (zwei Spieler in derselben Gruppe)
#    gegengetestet, siehe [[project_team_id_field_validation]]-Nachfolge.
#  - Roster-Namen: bei HD ein Zeiger, der erst dereferenziert werden muss
#    (roster_pointer_addr). Bei Extreme per String-Suche nach dem live vom
#    Nutzer genannten eigenen Namen ("Lord Andy") gefunden - die Tabelle
#    liegt dort OHNE Zeiger-Indirektion direkt als fester Block im
#    statischen Programm-Abbild (roster_base_direct), bestaetigt durch den
#    zweiten Spielernamen exakt 90 Byte (ROSTER_SLOT_SIZE) dahinter. Da die
#    Adresse im STATISCHEN Abbild liegt (nicht im Heap), sollte sie ueber
#    Neustarts stabil sein wie BASE_ADDR/die Objekttabelle - aber bisher nur
#    aus EINER Spielsitzung bestaetigt, noch kein Neustart-Test.
VERSION_PROFILES = {
    "hd": {
        "base_addr": 0x115FCBC,
        "event_type_id_addr": 0x0138880C,
        "event_player_num_addr": 0x013885E2,
        "diplomatic_group_array_addr": 0x0117D54C,
        "roster_pointer_addr": 0x004423A8,
        "roster_base_direct": None,
    },
    "extreme": {
        "base_addr": 0x011F28FC,
        # object_table_base selbst wird nirgends direkt gebraucht - nur die
        # daraus abgeleiteten Anker unten (siehe HD: EVENT_TYPE_ID_ADDR/
        # EVENT_PLAYER_NUM_ADDR sind ebenfalls nur Punkte INNERHALB der
        # Tabelle, kein eigener Basis-Parameter in den Nutzfunktionen).
        "event_type_id_addr": 0x0145D03C + 0x2C0,
        "event_player_num_addr": 0x0145D03C + 0x96,
        "diplomatic_group_array_addr": 0x0121018C,
        "roster_pointer_addr": None,
        "roster_base_direct": 0x024BA286,
    },
}

# Aktuell erkannte Version ("hd"/"extreme") - von connect() gesetzt, None
# davor. Externe Konsumenten (worker.py) koennen das fuer versionsabhaengige
# UI-Hinweise auslesen (z.B. "Roster-Namen nicht verfuegbar bei Extreme").
GAME_VERSION = None

# Alle vier folgenden Werte sind reine Defaults (HD) - werden von connect()
# via _apply_version_profile() auf die tatsaechlich erkannte Version
# umgeschaltet. Bewusst als einfache Modul-Globals gehalten statt ueberall
# durchgereicht: die Version aendert sich nie waehrend der Laufzeit eines
# verbundenen Prozesses, ein einmaliger Umschalter bei connect() ist hier
# pragmatischer als jede Funktion in dieser Datei um einen Versions-
# Parameter zu erweitern.
BASE_ADDR = VERSION_PROFILES["hd"]["base_addr"]

# Fester Byte-Abstand zwischen den Spieler-Slots - IDENTISCH bei HD und
# Extreme (live bestaetigt 2026-09-05), deshalb keine eigene Profil-Zeile.
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

    # Die 7 arabischen Einheiten-Zähler standen früher hier als Struct-
    # Felder (+0x2558..+0x2574). Live widerlegt (2026-09-04, echtes
    # Match): die Werte pulsen kurz nach dem Training und fallen dann auf
    # 0 zurück, obwohl die Einheit nachweislich noch lebt, plus Cross-
    # Slot-Leck in einen unbeteiligten KI-Slot und Allocator-artiges
    # Rauschen im Rohscan der Umgebung - siehe research/shc_overlay_status.md,
    # Abschnitt "NEUER BEFUND: arabischer Einheiten-Block". Ersetzt durch
    # den Live-Zensus über die 0x490-Objekttabelle, siehe
    # ARAB_UNIT_TYPE_VALUES/poll_arab_units() weiter unten.
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

# Alte, widerlegte Lord-HP-Adresse (siehe EVENT_*/LORD_TYPE_ID weiter unten
# für die tatsächlich funktionierende Live-Lösung). War derselbe geteilte
# Ereignis-Ringpuffer wie beim Mönch-Log - in einem Sandbox-Test mit rein
# sequenziellen, isolierten Treffern sah es nach einem Pro-Spieler-Array
# aus, brach aber in einem echten Match mit Hintergrundgeschehen zusammen
# (alle Spieler zeigten denselben veralteten Wert). Nur noch als
# historischer Anker für die Offset-Herleitung von LORD_HP_CURRENT_OFFSET
# unten interessant, nicht mehr verwenden.
LORD_HP_BASE = 0x1388DA4
LORD_HP_STRIDE = 0x490

# Max-HP eines Burgherren = LORD_BASE_HP * Multiplikator je nach
# KI-Persönlichkeit (Feld "lord.StrengthMultiplier" in einem lokalen
# Community-.aic-Editor-Tool gefunden, KEIN offizielles Firefly-Feld - die
# echte vanilla.json der Spielinstallation kennt dieses Feld nicht). Die
# volle 16-Lord-Tabelle stammt aus einem Mehrheitskonsens über 14
# unabhängige Custom-AI-Mod-Exporte (jeder Mod überschreibt nur seinen
# einen Lord-Slot). Menschliche Spieler haben keinen dieser Namen im
# Roster-String und fallen auf den Standard-Multiplikator 1.0 zurück.
# LORD_BASE_HP live bestätigt (2026-09-04, siehe EVENT_*/LORD_TYPE_ID
# unten): ein Multi-Wert-Snapshot über 7 KI-Lords unterschiedlicher
# Persönlichkeit traf mit 150000 als Basis bei JEDEM einzelnen exakt
# (Ratte 74635≈75000, Schlange 105000, Schwein 150000, Wolf 224967≈225000,
# Saladin 210000, Sultan 135000, Richard 210000) - der alte Wert 100000
# war falsch (stammte aus einer zufälligen Zeile der oben genannten,
# inzwischen widerlegten LORD_HP_BASE-Adresse).
LORD_BASE_HP = 150000
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


# Live-KI-Personality-Tabelle (Lead 1) - dieselben Felder wie eine
# .aic-Datei (AttForceBase/AttForceRandom etc.), aber direkt aus dem
# laufenden Spiel gelesen statt aus einer vom Nutzer geladenen Datei.
# Tabellenformel und HD-Adresse live bestätigt (siehe
# [[project_aic_memory_field_investigation]]): ein Sequenzvergleich gegen
# alle 16 Personas in vanilla.json traf 16/16 exakt für AttForceBase.
# persona_index ist NICHT 0-basiert - Index 0 ist ein leerer Platzhalter,
# echte Personas sind 1-16 in der festen Reihenfolge, die PERSONA_INDEX
# unten kodiert.
AI_TABLE_STRIDE = 676  # 169 Felder * 4 Byte
AI_TABLE_FIELD_ATTFORCEBASE = 125
AI_TABLE_FIELD_ATTFORCERANDOM = 126

# HD: statische Adresse, 1:1 von der 1.41-Referenz übernommen (kein ASLR-
# Problem, wie BASE_ADDR). Extreme: noch NICHT verifiziert - laut UCP3s
# extension-aicloader müsste sich die Basis per AOB-Scan auf den Code-
# Bytes finden lassen. Für Extreme wird das per AOB-Scan gelöst (siehe
# _scan_extreme_ai_table_base() unten, von _apply_version_profile() bei
# jedem connect() aufgerufen) - "extreme" startet hier als Platzhalter
# None und wird bei Erfolg live überschrieben. Schlägt der Scan fehl
# (z.B. Spielversion mit anderem Code-Layout), bleibt es None und
# read_ai_personality_table() liefert für Extreme weiterhin bewusst
# (None, None) statt eines falschen Werts - kein Absturz.
AI_TABLE_BASE = {
    "hd": 0x023FC8E8,
    "extreme": None,
}

# UCP3s eigenes AOB-Pattern für "extension-aicloader" (core.readInteger(
# core.AOBScan(...))) - laut UCP3-Quelle für BEIDE Versionen gültig, liest
# 4 Byte ab Match-Beginn = aicArrayBaseAddr. Für HD war das nie nötig
# (dort reicht die statische Adresse oben), für Extreme ist es der
# einzige bekannte Weg, live an die Tabellen-Basis zu kommen. Live
# bestätigt 2026-09-05 (16/16-Sequenzvergleich gegen vanilla.json), aber
# erst 2026-09-06 tatsächlich in die App verdrahtet statt nur als
# Scratchpad-Test zu existieren.
_AI_TABLE_AOB_PATTERN = (
    "? ? ? ? e8 ? ? ? ? 89 1d ? ? ? ? 83 3d ? ? ? ? 00 75 44 6a 08 b9 "
    "? ? ? ? e8 ? ? ? ? 85 c0 74 34 8b c5 2b 05"
)


def _parse_aob_pattern(hexstr):
    """Zerlegt ein Space-getrenntes Hex/Wildcard-Pattern ("? A1 ? FF ...")
    in (bytes_mit_platzhalter_0, mask) - mask[i]=False heißt "beliebiges
    Byte", wie in Cheat Engine/UCP3 üblich."""
    tokens = hexstr.split()
    pattern = bytearray()
    mask = []
    for t in tokens:
        if t == "?":
            pattern.append(0)
            mask.append(False)
        else:
            pattern.append(int(t, 16))
            mask.append(True)
    return bytes(pattern), mask


def _aob_scan(data, pattern, mask):
    """Simpler linearer Byte-Scan mit Wildcard-Maske - für ein einmaliges
    Pattern pro connect() (nicht pro Tick) reicht das aus, keine Boyer-
    Moore-artige Optimierung nötig."""
    plen = len(pattern)
    n = len(data)
    for i in range(n - plen + 1):
        ok = True
        for j in range(plen):
            if mask[j] and data[i + j] != pattern[j]:
                ok = False
                break
        if ok:
            return i
    return -1


def _scan_extreme_ai_table_base(pm):
    """Führt den AOB-Scan für Extreme aus und gibt aicArrayBaseAddr
    zurück, oder None bei Fehlschlag (Pattern nicht gefunden, Modul nicht
    lesbar o.ä. - wird abgefangen, damit ein fehlgeschlagener Scan nie
    connect() zum Absturz bringt, nur die Live-KI-Tabelle bleibt
    unverfügbar)."""
    try:
        module = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME_EXTREME)
        if module is None:
            return None
        data = pm.read_bytes(module.lpBaseOfDll, module.SizeOfImage)
        pattern, mask = _parse_aob_pattern(_AI_TABLE_AOB_PATTERN)
        idx = _aob_scan(data, pattern, mask)
        if idx == -1:
            return None
        match_addr = module.lpBaseOfDll + idx
        return pm.read_int(match_addr)
    except Exception:
        return None

# Feste Engine-Reihenfolge der 16 Personas (persona_index 1-16), bestätigt
# über den 16/16-Sequenzvergleich gegen vanilla.json. Aliases (Deutsch)
# nach demselben Muster wie LORD_STRENGTH_MULTIPLIER oben - bewusst NICHT
# mit diesem Dict zusammengelegt, auch wenn die Schlüssel identisch sind,
# weil die beiden Tabellen unterschiedliche Dinge kodieren (Index vs.
# Multiplikator) und unabhängig voneinander geändert werden könnten.
PERSONA_INDEX = {
    "rat": 1, "ratte": 1,
    "snake": 2, "schlange": 2,
    "pig": 3, "schwein": 3,
    "wolf": 4,
    "saladin": 5,
    "caliph": 6, "kalif": 6,
    "sultan": 7,
    "richard": 8,
    "frederick": 9, "friedrich": 9,
    "phillip": 10,
    "wazir": 11,
    "emir": 12,
    "nizar": 13,
    "sheriff": 14,
    "marshal": 15,
    "abbot": 16, "abt": 16,
}


def get_persona_index(display_name):
    """Wie get_lord_strength_multiplier() - Wortgrenzen-Match gegen den
    Roster-Anzeigenamen, damit z.B. "Wolfgang" nicht fälschlich als Wolf
    (Index 4) erkannt wird. Kein Treffer -> None (menschlicher Spieler
    oder unbekannter Custom-Lord-Name)."""
    if not display_name:
        return None
    lower = display_name.lower()
    for name, index in PERSONA_INDEX.items():
        if re.search(rf"\b{re.escape(name)}\b", lower):
            return index
    return None


# Live-Persona-Zuweisung pro Spieler (2026-09-06 gefunden, per
# Rückverfolgung des EAX-Parameters am Anfang der Eskalationsfunktion -
# siehe [[project_aic_memory_field_investigation]]): dieses Feld sagt
# DIREKT, welchen Tabellen-Index (1-16) ein Spieler-Slot benutzt -
# unabhängig vom angezeigten Namen, würde also das Custom-Mod-Problem
# von get_persona_index() lösen (Namens-Matching kann danebenliegen, wenn
# ein Mod Anzeigename und tatsächlich genutzte Tabellenzeile entkoppelt).
# Live bestätigt für EINEN Spieler per Execution-Breakpoint direkt am
# Funktionsanfang (EAX=4 bei ESI=3×PLAYER_STRIDE, Slot 3 = "Wolf,
# Herzog Volpe" -> persona_index 4, exakter Treffer). ABER: ein kalter
# Reihen-Test direkt danach zeigte, dass dieses Feld für die ANDEREN
# beiden KIs (Ratte, Schwein) in einem frisch gestarteten Match noch
# ungültige/falsche Werte hatte (0 bzw. 2 statt der erwarteten 1/3) -
# es wird offenbar wie attackNumber/addtroops erst befüllt, sobald diese
# Funktion für den jeweiligen Spieler mindestens einmal gelaufen ist
# (also nach dessen erstem echten Angriff), nicht schon beim Match-Start.
# Deshalb NUR als Fallback verwendet (siehe read_ai_personality_table
# unten), nicht als primäre Quelle - sonst würde es die bewährte
# Namens-Erkennung bei Vanilla-Matches gelegentlich durch einen noch
# nicht aktualisierten Wert überschreiben. Für HD nicht separat
# verifiziert, aber wie ATTACK_NUMBER_OFFSET/ATTACK_ADDTROOPS_OFFSET als
# version-unabhängiger Offset behandelt (Offsets innerhalb des geteilten
# Pro-Spieler-Structs haben sich in diesem Projekt bisher immer 1:1
# zwischen HD und Extreme übertragen).
LIVE_PERSONA_INDEX_OFFSET = -0x1BC4


def read_live_persona_index(pm, player_index):
    """Liest den tatsächlich zugewiesenen Tabellen-Index (1-16) direkt aus
    dem Spielspeicher, KEIN Namens-Raten. Gibt None zurück bei Lesefehler
    oder einem offensichtlich ungültigen Wert (außerhalb 1-16 - z.B. der
    menschliche Spieler, dessen Slot dieses Feld nie befüllt)."""
    addr = BASE_ADDR + player_index * PLAYER_STRIDE + LIVE_PERSONA_INDEX_OFFSET
    try:
        value = pm.read_int(addr)
    except Exception:
        return None
    if value is None or not (1 <= value <= 16):
        return None
    return value


def read_ai_personality_table(pm, player_index, display_name):
    """Liest AttForceBase/AttForceRandom LIVE aus der KI-Tabelle statt aus
    einer geladenen .aic-Datei. Ermittelt den Personality-Index zuerst
    über die bewährte Namens-Erkennung (get_persona_index() - zuverlässig
    bei Vanilla-Namen) und versucht read_live_persona_index() nur als
    Fallback, wenn der Name keiner bekannten Persona zugeordnet werden
    konnte (menschlicher Spieler, oder ein Custom-Mod-Lord mit
    entkoppeltem Anzeigenamen - siehe LIVE_PERSONA_INDEX_OFFSET oben für
    die Einschränkungen dieses Fallbacks). Gibt (None, None) zurück, wenn
    die aktuelle Version noch keine Tabellen-Adresse hat (Extreme ohne
    erfolgreichen AOB-Scan) oder gar kein Index ermittelt werden konnte."""
    table_base = AI_TABLE_BASE.get(GAME_VERSION)
    persona_index = get_persona_index(display_name)
    if persona_index is None:
        persona_index = read_live_persona_index(pm, player_index)
    if table_base is None or persona_index is None:
        return None, None
    try:
        row = table_base + persona_index * AI_TABLE_STRIDE
        base = pm.read_int(row + AI_TABLE_FIELD_ATTFORCEBASE * 4)
        rand = pm.read_int(row + AI_TABLE_FIELD_ATTFORCERANDOM * 4)
        return base, rand
    except Exception:
        return None, None


# Live-Angriffs-Eskalation (Lead 2) - dieselbe Spielfunktion (004CDEDC in
# der HD-1.41-Referenz), die bei jedem tatsächlich losgeschickten Angriff
# die nächste Mindest-Truppenzahl um 5 (oder 7 bei >10.000 Gold) erhöht.
# Live numerisch bestätigt 2026-09-06 (drei exakte Live-Treffer inkl.
# eines echten Truppen-Auszählens, siehe
# [[project_aic_memory_field_investigation]]). WICHTIG: diese beiden
# Felder werden NICHT wie RESOURCE_OFFSETS mit player_index*PLAYER_STRIDE
# adressiert, sondern mit (player_index+1)*PLAYER_STRIDE - die KI-
# Entscheidungsfunktion zählt ihre Spieler intern ab 1 statt ab 0 (per
# Cross-Check gegen die schon bekannte Gold-Adresse in derselben Funktion
# entdeckt und live bestätigt: ESI stand beim Schreibzugriff exakt auf
# 2*PLAYER_STRIDE für player_index=1). NICHT versehentlich an die
# RESOURCE_OFFSETS-Konvention angleichen.
ATTACK_NUMBER_OFFSET = -0x5A0
ATTACK_ADDTROOPS_OFFSET = -0x624
ATTACK_ESCALATION_GOLD_THRESHOLD = 10000
ATTACK_ESCALATION_FACTOR_LOW = 5   # Gold <= 10.000
ATTACK_ESCALATION_FACTOR_HIGH = 7  # Gold > 10.000
ATTACK_ESCALATION_CAP = 500  # Spiel deckelt addtroops hart auf 500


def read_attack_escalation(pm, player_index):
    """Liest attackNumber/addtroops für player_index (0-7, dieselbe
    Zählung wie überall sonst in diesem Modul - die interne +1-
    Verschiebung wird hier gekapselt, siehe ATTACK_NUMBER_OFFSET oben).
    attackNumber zeigt live bestätigt (2026-09-06) schon VOR dem ersten
    Angriff 1 statt 0 - das Spiel zählt die "nächste" Angriffsnummer, kein
    Bug. Gibt (None, None) bei Lesefehler zurück."""
    base = BASE_ADDR + (player_index + 1) * PLAYER_STRIDE
    try:
        attack_number = pm.read_int(base + ATTACK_NUMBER_OFFSET)
        addtroops = pm.read_int(base + ATTACK_ADDTROOPS_OFFSET)
        return attack_number, addtroops
    except Exception:
        return None, None


def predict_next_attack_range(ai_base, ai_rand, attack_number, gold):
    """Kombiniert Lead 1 (statische Personality-Werte) und Lead 2 (live
    Eskalationszähler) zu einer Mindest-/Höchstschätzung für die nächste
    Angriffsgröße. Live formel-validiert 2026-09-06: addtroops =
    Zufall(0..ai_rand) + attack_number * (5 oder 7), gedeckelt auf 500.
    `attack_number` ist der AKTUELL gespeicherte Wert - für die Vorhersage
    wird er hier um 1 erhöht, weil das Spiel ihn erst GENAU beim
    Losschicken des nächsten Angriffs selbst hochzählt (der gespeicherte
    Wert ist also immer "einen hinter" dem, was für den nächsten Angriff
    tatsächlich verwendet wird). Gibt (None, None) zurück, wenn eine
    Eingabe fehlt (z.B. Extreme ohne AI_TABLE_BASE, oder Lesefehler)."""
    if ai_base is None or attack_number is None or gold is None:
        return None, None
    ai_rand = ai_rand or 0
    factor = (
        ATTACK_ESCALATION_FACTOR_HIGH
        if gold > ATTACK_ESCALATION_GOLD_THRESHOLD
        else ATTACK_ESCALATION_FACTOR_LOW
    )
    escalation = (attack_number + 1) * factor
    addtroops_min = min(ATTACK_ESCALATION_CAP, escalation)
    addtroops_max = min(ATTACK_ESCALATION_CAP, escalation + ai_rand)
    return ai_base + addtroops_min, ai_base + addtroops_max


# Team-/Bündnis-Erkennung (gefunden 2026-09-02, siehe research/
# shc_overlay_status.md für die volle Herleitungs-/Kalibrierungs-Historie).
# Kein dediziertes Team-ID-Feld, aber zwei Live-Felder (vermutlich Pro-
# Team-Aggregatwerte irgendeiner Art, konkrete Bedeutung unbekannt) zeigen
# über die Dauer eines Matches denselben Wert für Team-Kollegen und nie für
# Nicht-Kollegen - live verifiziert über 5+ Matches (verschachtelte Teams,
# ungleiche Größen inkl. Solo-Team, leere Slots, 4v4-Grenzfall, und ein
# ECHTER Blindtest: Vorhersage "kein Team, Free-for-All" korrekt VOR der
# Auflösung getroffen). Kalibrierte Ko-Gleichheits-Rate: Team-Kollegen
# 0.64-0.94, Nicht-Kollegen exakt 0.00 (n=27 Paare, 480 Messungen über die
# zwei ungünstigsten getesteten Konstellationen) - riesiger Sicherheitsabstand.
TEAM_SIGNAL_PRIMARY_OFFSET = 0x1BB8
TEAM_SIGNAL_SECONDARY_OFFSET = 0x1C78

# (Historie zur alten, lange erfolglosen Team-Erkennungssuche inkl. des
# widerlegten +0x9218-Kandidaten: siehe research/shc_overlay_status.md -
# die eigentliche Lösung sind TEAM_SIGNAL_PRIMARY/SECONDARY_OFFSET oben.)

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
# Namenstabelle des Matches, Reihenfolge = Spieler-Slot-Reihenfolge. Bei
# Extreme gibt es stattdessen roster_base_direct (siehe VERSION_PROFILES
# weiter oben) - dort liegt die Tabelle ohne Zeiger-Indirektion direkt an
# einer festen Adresse. Sind BEIDE None (sollte nicht vorkommen, reine
# Absicherung), gibt read_roster_names() {} zurueck und das Overlay faellt
# auf slot_label()-Platzhalter zurueck, kein Absturz.
ROSTER_POINTER_ADDR = VERSION_PROFILES["hd"]["roster_pointer_addr"]
ROSTER_BASE_DIRECT = VERSION_PROFILES["hd"]["roster_base_direct"]
ROSTER_SLOT_SIZE = 90
ROSTER_NUM_SLOTS = 8

# Die 7 arabischen Einheitstypen: Live-Zensus über die 0x490-Objekttabelle
# statt Struct-Feld (siehe RESOURCE_OFFSETS-Kommentar oben). Typwerte über
# das ECHTE Typfeld (Datensatz-Offset 0x8E, absolut EVENT_PLAYER_NUM_ADDR-8,
# siehe unten) gelesen, jeweils einzeln per Bauen-und-Zensus live bestätigt
# (2026-09-04, echtes Match, troops_total-Delta jedes Mal exakt passend).
# Fallen praktischerweise auf einen durchlaufenden Block 70-76, in exakt
# der Reihenfolge, die zwei unabhängige UCP3-Quellen (gynt/ucp_startResources
# troops.lua, CIO61/rebalancer constants.lua) für diese Einheitengruppe
# nannten - nur der Startindex (dort 68 vermutet) war falsch geraten.
ARAB_UNIT_TYPE_VALUES = {
    "unit_arab_archer":    70,  # Arabischer Bogenschütze
    "unit_slave":          71,  # Sklave
    "unit_slinger":        72,  # Schleuderschütze
    "unit_assassin":       73,  # Assassine
    "unit_horse_archer":   74,  # Berittener Bogenschütze
    "unit_arab_swordsman": 75,  # Arabischer Schwertkämpfer
    "unit_fire_thrower":   76,  # Feuerwerfer
}
_ARAB_TYPE_VALUE_TO_KEY = {v: k for k, v in ARAB_UNIT_TYPE_VALUES.items()}

# Unit-Typen, für die es eine Live-Quelle gibt (für die "Truppen-
# Aufschlüsselung"-Anzeige im Overlay) - alle RESOURCE_OFFSETS-Keys, die
# mit "unit_" beginnen, plus die per Objekttabellen-Zensus gelesenen
# arabischen Typen.
UNIT_TYPE_KEYS = [k for k in RESOURCE_OFFSETS if k.startswith("unit_")] + list(ARAB_UNIT_TYPE_VALUES)

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


def _apply_version_profile(version: str, pm=None):
    """Schaltet die versionsabhängigen Modul-Globals (siehe VERSION_PROFILES
    ganz oben) auf die erkannte Spielversion um. Wird nur von connect() bei
    erfolgreichem Verbindungsaufbau aufgerufen. `pm` wird nur für Extreme
    gebraucht (AOB-Scan der KI-Tabellen-Basis, siehe
    _scan_extreme_ai_table_base) - bei HD ungenutzt, da dort eine
    statische Adresse reicht."""
    global GAME_VERSION, BASE_ADDR, EVENT_TYPE_ID_ADDR, EVENT_PLAYER_NUM_ADDR
    global DIPLOMATIC_GROUP_ARRAY_ADDR, ROSTER_POINTER_ADDR, ROSTER_BASE_DIRECT
    profile = VERSION_PROFILES[version]
    GAME_VERSION = version
    BASE_ADDR = profile["base_addr"]
    EVENT_TYPE_ID_ADDR = profile["event_type_id_addr"]
    EVENT_PLAYER_NUM_ADDR = profile["event_player_num_addr"]
    DIPLOMATIC_GROUP_ARRAY_ADDR = profile["diplomatic_group_array_addr"]
    ROSTER_POINTER_ADDR = profile["roster_pointer_addr"]
    ROSTER_BASE_DIRECT = profile["roster_base_direct"]

    if version == "extreme" and pm is not None and AI_TABLE_BASE.get("extreme") is None:
        # Nur EINMAL scannen, nicht bei jedem Reconnect erneut - die
        # Adresse liegt im statischen Programm-Abbild und ändert sich
        # innerhalb einer Spielversion nie (wie BASE_ADDR/Objekttabelle).
        AI_TABLE_BASE["extreme"] = _scan_extreme_ai_table_base(pm)


def connect(verbose: bool = True):
    """Verbindet mit dem laufenden Spielprozess - probiert HD und Extreme
    nacheinander (siehe PROCESS_NAME_HD/PROCESS_NAME_EXTREME), da beide
    Versionen unterstützt werden. Gibt (pm, error_message) zurück -
    error_message ist None bei Erfolg. Schaltet bei Erfolg automatisch die
    versionsabhängigen Adressen um (siehe _apply_version_profile)."""
    for version, process_name in (("hd", PROCESS_NAME_HD), ("extreme", PROCESS_NAME_EXTREME)):
        try:
            pm = pymem.Pymem(process_name)
            _apply_version_profile(version, pm)
            if verbose:
                print(f"[OK] Verbunden mit {process_name} ({version})")
            return pm, None
        except pymem.exception.ProcessNotFound:
            continue
        except pymem.exception.CouldNotOpenProcess:
            msg = "Zugriff verweigert - App muss als Administrator laufen."
            if verbose:
                print(f"[FEHLER] {msg}")
            return None, msg

    msg = (
        f"Weder '{PROCESS_NAME_HD}' noch '{PROCESS_NAME_EXTREME}' gefunden. "
        "Läuft das Spiel?"
    )
    if verbose:
        print(f"[FEHLER] {msg}")
    return None, msg


# Konsistenz-Check zweier redundanter Slot-Felder (Cross-Referenz) -
# session_ok wird von read_player() mit abgelegt und an mehreren Stellen
# ausgewertet.
_SESSION_CHECK_OFFSET_A = 0x3454
_SESSION_CHECK_OFFSET_B = 0x34B5


def _session_check_slot(pm, player_index):
    """True nur wenn beide Referenzfelder lesbar sind, übereinstimmen und
    den erwarteten Wert zeigen."""
    base = BASE_ADDR + player_index * PLAYER_STRIDE
    try:
        a = pm.read_bytes(base + _SESSION_CHECK_OFFSET_A, 1)[0]
        b = pm.read_bytes(base + _SESSION_CHECK_OFFSET_B, 1)[0]
    except Exception:
        return False
    return a == b == 1


def read_player(pm, player_index):
    """Liest alle bekannten Werte für einen Spieler-Slot aus."""
    base = BASE_ADDR + player_index * PLAYER_STRIDE
    values = {}
    values["session_ok"] = _session_check_slot(pm, player_index)
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

    try:
        values["team_signal_primary"] = pm.read_ushort(base + TEAM_SIGNAL_PRIMARY_OFFSET)
    except Exception:
        values["team_signal_primary"] = None
    try:
        values["team_signal_secondary"] = pm.read_ushort(base + TEAM_SIGNAL_SECONDARY_OFFSET)
    except Exception:
        values["team_signal_secondary"] = None

    # Live-Lord-HP über die 0x490-Objekttabelle (LORD_TYPE_ID etc., siehe
    # unten) statt der alten, widerlegten LORD_HP_BASE-Adresse. Setzt
    # voraus, dass poll_lord_hp(pm) bei jedem Tick VORHER aufgerufen wurde
    # (siehe worker.py) - hier wird nur der gecachte Wert gelesen, kein
    # eigener Speicherzugriff mehr nötig.
    values["lord_hp"], values["lord_hp_max_live"] = get_lord_hp(player_index)

    # Arabische Einheiten-Zählung per Objekttabellen-Zensus statt Struct-
    # Feld (siehe ARAB_UNIT_TYPE_VALUES oben). Setzt wie bei Lord-HP
    # voraus, dass poll_arab_units(pm) bei jedem Tick VORHER lief.
    values.update(get_arab_unit_counts(player_index))

    return values


# Objekttabelle für den Live-Zensus (Lord-HP, arabische Einheiten, Mönche
# - alle drei nutzen dieselbe 0x490-Byte-Datensatz-Tabelle, gefiltert auf
# das ECHTE Typfeld bei Datensatz-Offset 0x8E, siehe DURCHBRUCH-Abschnitt
# in research/shc_overlay_status.md).
# EVENT_STRIDE ist IDENTISCH bei HD und Extreme (live bestaetigt
# 2026-09-05). EVENT_PLAYER_NUM_ADDR/EVENT_TYPE_ID_ADDR sind reine HD-
# Defaults, siehe VERSION_PROFILES ganz oben - connect() schaltet sie auf
# die erkannte Version um. Diese beiden Adressen werden NIE direkt als
# Datenquelle gelesen, nur als virtual_query-Anker (siehe poll_monk_units/
# _scan_lord_table) - jede Adresse INNERHALB derselben Objekttabellen-
# Speicherregion funktioniert hier gleich gut.
EVENT_STRIDE = 0x490
EVENT_PLAYER_NUM_ADDR = VERSION_PROFILES["hd"]["event_player_num_addr"]
EVENT_TYPE_ID_ADDR = VERSION_PROFILES["hd"]["event_type_id_addr"]

# Mönch-Zählung per Zensus statt des früheren Ereignis-Log-Ansatzes
# (EVENT_COUNTER_ADDR-Ringpuffer, gefiltert auf das ALTE, kaputte Typfeld
# bei 0x2C0 mit Wert 101). Der alte Ansatz war live nachweislich
# unzuverlässig: der Ringpuffer-Zähler oszilliert unabhängig von echten
# Mönch-Ereignissen extrem schnell (Test 0, ~60.000 Reads/s im Leerlauf:
# 2257 Rückgänge in 60s - breites Engine-Allokations-Rauschen, kein torn
# read, siehe research/shc_overlay_status.md). War außerdem nur ein
# KUMULATIVER "seit Tool-Start trainiert"-Zähler, kein Live-Bestand.
#
# Live per Bauen-und-Zensus bestätigt (2026-09-04): Typ 37 = Mönch.
MONK_TYPE_VALUE = 37

_monk_counts_by_player_num = {}  # player_num (1-8) -> Anzahl


def reset_monk_tracking():
    """Wie reset_lord_hp_tracking() - bei jedem frischen Verbinden
    aufrufen, siehe worker.py::_ensure_connected."""
    _monk_counts_by_player_num.clear()


def poll_monk_units(pm):
    """Muss bei JEDEM Poll-Tick aufgerufen werden (analog zu
    poll_lord_hp/poll_arab_units), VOR read_all_players(). Anders als beim
    alten Ereignis-Log gibt es hier kein "Ereignisse verpassen" mehr - jeder
    Aufruf liefert den vollständigen aktuellen Bestand, kein zusätzliches
    Zwischen-Polling zwischen den regulären Ticks mehr nötig (siehe
    worker.py - der alte MONK_POLL_INTERVAL_S-Sonderweg entfällt)."""
    try:
        mbi = pymem.memory.virtual_query(pm.process_handle, EVENT_TYPE_ID_ADDR)
    except Exception:
        return
    region_base = mbi.BaseAddress
    n_rows = mbi.RegionSize // EVENT_STRIDE
    try:
        buf = pm.read_bytes(region_base, n_rows * EVENT_STRIDE)
    except Exception:
        return

    player_num_phase = (EVENT_PLAYER_NUM_ADDR - region_base) % EVENT_STRIDE
    # Echtes Typfeld: Datensatz-Offset 0x8E, absolut EVENT_PLAYER_NUM_ADDR-8
    # (siehe DURCHBRUCH-Abschnitt) - NICHT das alte type_id-Feld bei 0x2C0.
    real_type_phase = (player_num_phase - 8) % EVENT_STRIDE

    counts = {p: 0 for p in range(1, 9)}
    for row in range(n_rows):
        base = row * EVENT_STRIDE
        real_type = int.from_bytes(buf[base + real_type_phase: base + real_type_phase + 2], "little")
        if real_type != MONK_TYPE_VALUE:
            continue
        player_num = int.from_bytes(buf[base + player_num_phase: base + player_num_phase + 2], "little")
        if not (1 <= player_num <= 8):
            continue
        counts[player_num] += 1

    _monk_counts_by_player_num.clear()
    _monk_counts_by_player_num.update(counts)


def get_monks_trained(player_index):
    """player_index: 0-7. Gibt den aktuellen LIVE-Bestand an Mönchen für
    diesen Spieler zurück (0 falls keiner, None falls noch kein Vollscan
    lief). Name aus Kompatibilitätsgründen beibehalten (Payload-Key
    "monks_trained" in Overlay/Übersicht) - ist aber seit dem Umbau auf den
    Objekttabellen-Zensus KEIN kumulativer Wert mehr, sondern sinkt wie bei
    jeder anderen Einheit, wenn ein Mönch stirbt."""
    return _monk_counts_by_player_num.get(player_index + 1)


# --- Live Lord-HP über dieselbe 0x490-Objekttabelle ------------------------
#
# Live gefunden und ausführlich falsifikationsgetestet am 2026-09-04 (volle
# Herleitungs-/Test-Historie siehe research/shc_overlay_status.md,
# Sackgasse 7). Dieselbe 0x490-Byte-Datensatz-Tabelle wie das Mönch-
# Event-Log oben (EVENT_*), hier aber als echtes Objekt-Array genutzt:
# Datensätze mit type_id==LORD_TYPE_ID sind Lords (genau einer pro aktivem
# Spieler). LORD_HP_CURRENT_OFFSET/LORD_HP_MAX_OFFSET liegen relativ zum
# player_num-Feld desselben Datensatzes.
#
# Bestätigt über:
#  - Multi-Wert-Snapshot (kein Kampf nötig): 7 KI-Lords unterschiedlicher
#    Persönlichkeit, LORD_HP_MAX_OFFSET traf bei JEDEM exakt
#    150000*LORD_STRENGTH_MULTIPLIER (LORD_BASE_HP war bisher fälschlich
#    100000, siehe oben).
#  - Live-Schadenstest am eigenen Lord: LORD_HP_CURRENT_OFFSET fiel unter
#    echtem Beschuss sauber monoton, LORD_HP_MAX_OFFSET blieb exakt gleich,
#    player_num blieb über die ganze Messung stabil (feste Zeile).
#  - Pflicht-Duplikat-Gegentest (zwei identische Lord-Personas, nur einen
#    beschossen, 60s): nur der beschossene Datensatz fiel, der andere blieb
#    exakt unverändert - echtes Pro-Objekt-Feld, keine geteilte Tabelle
#    (genau der Test, der beim alten LORD_HP_BASE-Versuch gefehlt hat).
#
# WICHTIG: `type_id` selbst flackert nachweislich (auch bei Nicht-Lords
# beobachtet, z.B. Bogenschütze kurzzeitig als "Mönch" getaggt) - als
# alleiniger Identitäts-Anker ungeeignet. Deshalb: Zeilen-Index cachen,
# aber player_num bei jedem Tick nachprüfen (nicht type_id), und die
# komplette Tabelle nur alle LORD_ROW_REVALIDATE_TICKS neu absuchen statt
# bei jedem Tick (Vollscan ist der teure Teil).
LORD_TYPE_ID = 13
LORD_HP_CURRENT_OFFSET = 818
LORD_HP_MAX_OFFSET = 822
# Bei ~500ms Poll-Intervall alle ~2s neu suchen (2026-09-06 von 10/~5s
# gesenkt - Nutzer meldete gelegentlich falsche Lord-HP-Werte bei hoher
# Spielgeschwindigkeit: die Objekttabelle recycelt Zeilen dann in
# SIMULIERTER Zeit viel schneller, das Zeitfenster zwischen zwei
# Vollscans (in REALER Wall-Clock-Zeit gemessen) deckte bei hoher
# Geschwindigkeit dadurch mehr echte Recycling-Ereignisse ab, als der
# player_num-Gegencheck allein zuverlässig abfangen konnte, bevor der
# naechste Vollscan die verwaiste Zeile endlich verwirft. Kein Tausch
# gegen einen strengeren Pro-Tick-Check (z.B. type_id zusaetzlich
# pruefen) - type_id flackert nachweislich auch bei echten Lords (siehe
# Kommentar unten), ein strengerer Check wuerde also selbst zu neuen
# Fehlalarmen fuehren, statt das Problem zu loesen.
LORD_ROW_REVALIDATE_TICKS = 4

_lord_rows_by_player_num = {}  # player_num (1-8) -> Datensatz-Index
_lord_hp_by_player_num = {}    # player_num (1-8) -> (aktuelle_hp, max_hp)
_lord_row_tick_counter = 0


def reset_lord_hp_tracking():
    """Wie reset_monk_tracking() - bei jedem frischen Verbinden aufrufen,
    siehe worker.py::_ensure_connected."""
    global _lord_row_tick_counter
    _lord_rows_by_player_num.clear()
    _lord_hp_by_player_num.clear()
    _lord_row_tick_counter = 0


def _scan_lord_table(pm):
    """Voller Regions-Scan über die gesamte Objekttabelle. Findet für jede
    Spielernummer (1-8) die plausibelste Zeile mit type_id==LORD_TYPE_ID -
    bei mehreren Treffern für denselben Spieler (kurzzeitig falsch getaggte,
    frisch recycelte Zeilen kommen vor) wird die mit dem GRÖSSTEN
    LORD_HP_MAX_OFFSET-Wert genommen, da Störtreffer erfahrungsgemäß
    deutlich kleinere, unplausible Werte zeigen. Gibt (rows, hp) zurück:
    rows={player_num: row_index}, hp={player_num: (aktuell, max)}."""
    try:
        mbi = pymem.memory.virtual_query(pm.process_handle, EVENT_TYPE_ID_ADDR)
    except Exception:
        return {}, {}
    region_base = mbi.BaseAddress
    n_rows = mbi.RegionSize // EVENT_STRIDE
    try:
        buf = pm.read_bytes(region_base, n_rows * EVENT_STRIDE)
    except Exception:
        return {}, {}

    type_id_phase = (EVENT_TYPE_ID_ADDR - region_base) % EVENT_STRIDE
    player_num_phase = (EVENT_PLAYER_NUM_ADDR - region_base) % EVENT_STRIDE
    cur_phase = (player_num_phase + LORD_HP_CURRENT_OFFSET) % EVENT_STRIDE
    max_phase = (player_num_phase + LORD_HP_MAX_OFFSET) % EVENT_STRIDE

    best = {}  # player_num -> (row, aktuell, max)
    for row in range(n_rows):
        base = row * EVENT_STRIDE
        type_id = int.from_bytes(buf[base + type_id_phase: base + type_id_phase + 2], "little")
        if type_id != LORD_TYPE_ID:
            continue
        player_num = int.from_bytes(buf[base + player_num_phase: base + player_num_phase + 2], "little")
        if not (1 <= player_num <= 8):
            continue
        maximum = int.from_bytes(buf[base + max_phase: base + max_phase + 4], "little")
        current = int.from_bytes(buf[base + cur_phase: base + cur_phase + 4], "little")
        prev = best.get(player_num)
        if prev is None or maximum > prev[2]:
            best[player_num] = (row, current, maximum)

    rows = {p: v[0] for p, v in best.items()}
    hp = {p: (v[1], v[2]) for p, v in best.items()}
    return rows, hp


def poll_lord_hp(pm):
    """Muss bei JEDEM Poll-Tick aufgerufen werden (analog zu
    poll_monk_units/poll_arab_units). Sucht nur alle LORD_ROW_REVALIDATE_TICKS die
    komplette Tabelle neu ab (teurer Vollscan, ~wenige MB), liest
    dazwischen nur die gecachten Zeilen direkt (billig - ein paar gezielte
    Reads), prüft dabei aber jedes Mal player_num nach - falls eine Zeile
    recycelt wurde, wird sie verworfen statt einen falschen Wert zu liefern
    (bleibt bis zum nächsten Vollscan als 'unbekannt', kein Rateversuch)."""
    global _lord_row_tick_counter
    need_full_scan = (
        not _lord_rows_by_player_num
        or _lord_row_tick_counter % LORD_ROW_REVALIDATE_TICKS == 0
    )
    _lord_row_tick_counter += 1

    if need_full_scan:
        rows, hp = _scan_lord_table(pm)
        if rows:
            _lord_rows_by_player_num.clear()
            _lord_rows_by_player_num.update(rows)
            _lord_hp_by_player_num.clear()
            _lord_hp_by_player_num.update(hp)
        return

    try:
        mbi = pymem.memory.virtual_query(pm.process_handle, EVENT_TYPE_ID_ADDR)
    except Exception:
        return
    region_base = mbi.BaseAddress
    player_num_phase = (EVENT_PLAYER_NUM_ADDR - region_base) % EVENT_STRIDE

    stale = []
    for player_num, row in _lord_rows_by_player_num.items():
        record_start = region_base + row * EVENT_STRIDE
        try:
            actual_pnum = pm.read_short(record_start + player_num_phase)
            if actual_pnum != player_num:
                stale.append(player_num)
                continue
            current = pm.read_uint(record_start + player_num_phase + LORD_HP_CURRENT_OFFSET)
            maximum = pm.read_uint(record_start + player_num_phase + LORD_HP_MAX_OFFSET)
            _lord_hp_by_player_num[player_num] = (current, maximum)
        except Exception:
            stale.append(player_num)

    for player_num in stale:
        _lord_rows_by_player_num.pop(player_num, None)
        _lord_hp_by_player_num.pop(player_num, None)


def get_lord_hp(player_index):
    """player_index: 0-7. Gibt (aktuelle_hp, max_hp) zurück, oder
    (None, None) falls für diesen Spieler (noch) kein Lord-Datensatz
    gefunden wurde (z.B. kurz nach Match-Start, bevor der erste Vollscan
    lief, oder falls der Lord besiegt/der Spieler eliminiert wurde)."""
    return _lord_hp_by_player_num.get(player_index + 1, (None, None))


# --- Live-Zensus für die 7 arabischen Einheitstypen --------------------
# Ersetzt die früheren Pro-Spieler-Struct-Felder (siehe RESOURCE_OFFSETS-
# Kommentar oben) - anders als bei Lord-HP gibt es hier keine billige
# Einzelzeilen-Abkürzung, da JEDE passende Zeile pro Spieler gezählt werden
# muss, nicht nur eine bekannte wiedergefunden wird. Voller Regions-Scan
# bei jedem Tick, wie schon für die Belagerungstypen-Zensustests diese
# Session verwendet.

_arab_unit_counts_by_player_num = {}  # player_num (1-8) -> {key: count}


def reset_arab_unit_tracking():
    """Wie reset_lord_hp_tracking() - bei jedem frischen Verbinden
    aufrufen, siehe worker.py::_ensure_connected."""
    _arab_unit_counts_by_player_num.clear()


def poll_arab_units(pm):
    """Muss bei JEDEM Poll-Tick aufgerufen werden (analog zu
    poll_lord_hp/poll_monk_units), VOR read_all_players()."""
    try:
        mbi = pymem.memory.virtual_query(pm.process_handle, EVENT_TYPE_ID_ADDR)
    except Exception:
        return
    region_base = mbi.BaseAddress
    n_rows = mbi.RegionSize // EVENT_STRIDE
    try:
        buf = pm.read_bytes(region_base, n_rows * EVENT_STRIDE)
    except Exception:
        return

    player_num_phase = (EVENT_PLAYER_NUM_ADDR - region_base) % EVENT_STRIDE
    # Echtes Typfeld: Datensatz-Offset 0x8E, absolut EVENT_PLAYER_NUM_ADDR-8
    # (siehe DURCHBRUCH-Abschnitt) - NICHT das alte type_id-Feld bei 0x2C0.
    real_type_phase = (player_num_phase - 8) % EVENT_STRIDE

    counts = {p: {k: 0 for k in ARAB_UNIT_TYPE_VALUES} for p in range(1, 9)}
    for row in range(n_rows):
        base = row * EVENT_STRIDE
        real_type = int.from_bytes(buf[base + real_type_phase: base + real_type_phase + 2], "little")
        key = _ARAB_TYPE_VALUE_TO_KEY.get(real_type)
        if key is None:
            continue
        player_num = int.from_bytes(buf[base + player_num_phase: base + player_num_phase + 2], "little")
        if not (1 <= player_num <= 8):
            continue
        counts[player_num][key] += 1

    _arab_unit_counts_by_player_num.clear()
    _arab_unit_counts_by_player_num.update(counts)


def get_arab_unit_counts(player_index):
    """player_index: 0-7. Gibt {unit_arab_archer: n, ...} zurück, alle 0
    falls für diesen Spieler (noch) kein Vollscan lief."""
    return _arab_unit_counts_by_player_num.get(
        player_index + 1, {k: None for k in ARAB_UNIT_TYPE_VALUES}
    )


# --- Team-Erkennung (ALT/Fallback): rollierendes Ko-Gleichheits-Fenster ----
#
# Seit 2026-09-05 NICHT mehr die primäre Quelle (siehe
# get_diplomatic_teams() weiter unten) - bleibt aber im Code als
# Sicherheitsnetz, falls das neue Feld sich doch mal als unzuverlässig
# herausstellt. Läuft weiterhin bei jedem Tick mit (poll_team_detection),
# ihr Ergebnis (get_detected_teams()) wird aber aktuell nirgends mehr
# angezeigt/verwendet. Um zurückzuschalten: in worker.py wieder
# res.get_detected_teams() statt res.get_diplomatic_teams(...) verwenden.
#
# Team-Zugehörigkeit steht für die gesamte Dauer eines Matches fest - daher
# kein Live-Update pro Tick, sondern ein EINMALIGER Schätzer, der über einen
# Zeitraum Beweise sammelt und dann für den Rest des Matches einfriert
# ("latcht"), statt bei jedem Poll neu zu entscheiden (was bei den
# beobachteten kurzen Zufalls-Kollisionen sonst gelegentlich flackern würde).
#
# TEAM_RANGE_GATE hat ZWEI Zwecke, nicht nur eines: (1) blendet Felder mit zu
# wenig Wertespreizung aus (z.B. show_lord_hp-ähnliche Kollisions-Fallen),
# UND (2) schützt vor den ersten Sekunden nach Matchstart/Reconnect, in denen
# alle aktiven Spieler noch bei 0 oder sehr ähnlichen Werten stehen (live
# beobachtet: mehrfache Fehlstarts durch genau dieses Anlauf-Flackern) - bitte
# NICHT als reine Kollisions-Optimierung wegkürzen, ohne diesen zweiten Zweck
# zu bedenken.
TEAM_RANGE_GATE = 10
TEAM_COEQ_THRESHOLD = 0.35
TEAM_COEQ_GREY_LOW = 0.15
TEAM_COEQ_GREY_HIGH = 0.45
TEAM_LATCH_MIN_USABLE_SAMPLES = 120  # ~60s bei 500ms Poll-Intervall
TEAM_WARMUP_TICKS = 20  # erste Ticks nach Reset verwerfen (Anlauf-Flackern)


class _TeamDetector:
    def __init__(self):
        self.reset()

    def reset(self):
        self._counts = {}  # (slot_i, slot_j) -> Anzahl akzeptierter Ko-Gleichheits-Treffer
        self._usable_samples = 0
        self._raw_ticks = 0
        self._locked_partition = None  # Liste von Slot-Mengen, oder None

    def update(self, active_entries):
        """active_entries: Liste von dicts {'slot', 'team_signal_primary',
        'team_signal_secondary'} - NUR aktive Slots (is_active_slot() muss
        VORHER gefiltert haben, siehe Kommentar bei is_active_slot())."""
        if self._locked_partition is not None:
            return
        entries = [e for e in active_entries if e.get("team_signal_primary") is not None]
        if len(entries) < 2:
            return

        self._raw_ticks += 1
        if self._raw_ticks <= TEAM_WARMUP_TICKS:
            return

        def spread(field):
            vals = [e[field] for e in entries if e.get(field) is not None]
            if len(vals) < len(entries):
                return -1
            return max(vals) - min(vals)

        use_primary = spread("team_signal_primary") >= TEAM_RANGE_GATE
        use_secondary = spread("team_signal_secondary") >= TEAM_RANGE_GATE
        if not use_primary and not use_secondary:
            return  # Messung übersprungen - zählt NICHT in den Nenner

        self._usable_samples += 1
        for a in range(len(entries)):
            for b in range(a + 1, len(entries)):
                i, j = entries[a]["slot"], entries[b]["slot"]
                key = (i, j) if i < j else (j, i)
                agree = True
                if use_primary:
                    agree = agree and (entries[a]["team_signal_primary"] == entries[b]["team_signal_primary"])
                if use_secondary:
                    agree = agree and (entries[a]["team_signal_secondary"] == entries[b]["team_signal_secondary"])
                if agree:
                    self._counts[key] = self._counts.get(key, 0) + 1

        if self._usable_samples >= TEAM_LATCH_MIN_USABLE_SAMPLES:
            self._try_latch([e["slot"] for e in entries])

    def _try_latch(self, slots):
        rates = {}
        for a in range(len(slots)):
            for b in range(a + 1, len(slots)):
                i, j = slots[a], slots[b]
                key = (i, j) if i < j else (j, i)
                rates[key] = self._counts.get(key, 0) / self._usable_samples

        # Irgendein Paar in der Grauzone -> noch keine eindeutige Evidenz,
        # weiter sammeln statt vorschnell zu latchen.
        for rate in rates.values():
            if TEAM_COEQ_GREY_LOW <= rate <= TEAM_COEQ_GREY_HIGH:
                return

        # Gruppen per Union-Find bilden: Kante akzeptiert, wenn Rate klar
        # über der Schwelle liegt (nicht nur > TEAM_COEQ_GREY_HIGH, das ist
        # oben schon ausgeschlossen worden).
        parent = {s: s for s in slots}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        def union(x, y):
            parent[find(x)] = find(y)

        for (i, j), rate in rates.items():
            if rate > TEAM_COEQ_THRESHOLD:
                union(i, j)

        groups = {}
        for s in slots:
            groups.setdefault(find(s), set()).add(s)
        partition = list(groups.values())

        # Sicherheitsregel: ALLE Spieler in einer einzigen Gruppe ist immer
        # ein Fehler (kein Match wird komplett ohne Gegner gespielt) - schützt
        # vor dem befürchteten "beide Team-Werte kollidieren zufällig"-Fall
        # bei 2-Team-Matches.
        if len(partition) == 1 and len(slots) > 1:
            return

        # Near-Clique-Prüfung: JEDES interne Paar einer Gruppe muss über der
        # Schwelle liegen, nicht nur transitiv verbunden sein - eine einzelne
        # schwache Kante darf nicht zwei echte Gruppen zusammenziehen. Bewusst
        # strikt gehalten (keine Toleranz für einzelne schwache Kanten in
        # größeren Gruppen) - kann bei 5+-Personen-Teams zu häufigerem
        # Abstain statt Erkennung führen, ist aber die sicherere Wahl.
        for group in partition:
            g = list(group)
            for a in range(len(g)):
                for b in range(a + 1, len(g)):
                    i, j = g[a], g[b]
                    key = (i, j) if i < j else (j, i)
                    if rates.get(key, 0) <= TEAM_COEQ_THRESHOLD:
                        return

        self._locked_partition = partition

    def detected_teams(self):
        """dict slot(int)->team_number(int), oder None wenn noch nicht
        eingefroren oder wenn erkannt wurde, dass es gar keine echten Teams
        gibt (Free-for-All: jeder Spieler seine eigene Gruppe)."""
        if self._locked_partition is None:
            return None
        if all(len(g) == 1 for g in self._locked_partition):
            return None
        result = {}
        for team_num, group in enumerate(sorted(self._locked_partition, key=min), start=1):
            for slot in group:
                result[slot] = team_num
        return result


_team_detector = _TeamDetector()


def reset_team_detection():
    """Muss bei jedem frischen Verbinden UND bei jedem erkannten Match-
    Neustart aufgerufen werden (siehe worker.py) - sonst überlebt eine
    eingefrorene Team-Zuordnung einen Match-/Team-Wechsel."""
    _team_detector.reset()


def poll_team_detection(all_values):
    """Muss bei JEDEM Poll-Tick aufgerufen werden. all_values: wie von
    read_all_players() (Liste von (slot_index, values_dict)) - Filterung auf
    aktive Slots passiert hier intern über is_active_slot()."""
    entries = [
        {
            "slot": i,
            "team_signal_primary": v.get("team_signal_primary"),
            "team_signal_secondary": v.get("team_signal_secondary"),
        }
        for i, v in all_values
        if is_active_slot(v)
    ]
    _team_detector.update(entries)


def get_detected_teams():
    """dict slot(int)->team_number(int) der eingefrorenen Erkennung, oder
    None (noch nicht genug Daten, oder erkanntes Free-for-All ohne Teams).
    ALT/Fallback, siehe Kommentar oben - nicht mehr die primäre Quelle."""
    return _team_detector.detected_teams()


# --- Team-/Bündnis-Erkennung (NEU, primär): literales Gruppen-ID-Array ----
#
# Live gefunden und ausführlich falsifikationsgetestet 2026-09-04/05 (volle
# Herleitungs-/Test-Historie: 7 echte Matches, siehe
# research/shc_overlay_status.md, Abschnitt "DURCHBRUCH: literales
# Team-ID-Array live bestätigt"). Ersetzt die Ko-Gleichheits-Heuristik oben
# als primäre Quelle: eine feste, absolute Adresse im statischen .exe-Image
# (wie BASE_ADDR - kein ASLR-Risiko, per Modul-Grenzen-Check bestätigt),
# 8 aufeinanderfolgende u32-Werte (Slot 0-7), kein Struct-relativer Offset,
# kein Polling/Latch nötig - korrekt ab dem allerersten Tick nach
# Match-Start.
#
# Semantik bewusst als "diplomatische Gruppen-ID" verstanden, nicht als
# reine "Team-ID": jeder unallierte Solo-Spieler bekommt automatisch seine
# eigene, global eindeutige Nummer (fortlaufend nach den echten
# Team-Nummern), statt eines geteilten Sonderwerts - genau das war der
# Unterschied zu drei anderen, äußerlich fast identischen Kandidaten-
# Adressen, die bei zwei GLEICHZEITIGEN Solo-Spielern beide denselben Wert
# zeigten (für uns ein echter Fehler, da zwei unabhängige Solo-Spieler
# fälschlich als verbündet erkannt worden wären) - siehe Doku für die
# vollständige Abgrenzung.
#
# Bestätigt über: 4 unterschiedliche Team-Aufteilungen (inkl. Duplikat-
# Charakter-Test, auch mit 5 gleichzeitig SOLO spielenden identischen
# Personas), ein reines FFA-Match, einen kompletten Spiel-Prozess-Neustart,
# einen Lobby-Umsortier-Test (die FINALE, nicht eine zwischenzeitliche
# Aufteilung wird übernommen) und einen Elimination-Test (Wert eines
# besiegten Spielers bleibt stabil, kein Recycling).
# HD-Default, siehe VERSION_PROFILES ganz oben - connect() schaltet auf die
# erkannte Version um. Extreme-Wert (2026-09-05) per relativem Byte-
# Abstand zu BASE_ADDR extrapoliert und live plausibel bestaetigt (zwei
# aktive Solo-Spieler mit unterschiedlichen Gruppennummern, Rest 0) - aber
# NOCH KEIN echter Team-Match-Gegentest (siehe VERSION_PROFILES-Kommentar).
DIPLOMATIC_GROUP_ARRAY_ADDR = VERSION_PROFILES["hd"]["diplomatic_group_array_addr"]


def get_diplomatic_teams(pm, all_values):
    """all_values: wie von read_all_players() (Liste von (slot_index,
    values_dict)). Liest das Gruppen-ID-Array direkt (8 einzelne Reads,
    kein Vollscan/Cache nötig) und baut daraus dieselbe slot->team_number-
    Zuordnung wie die alte get_detected_teams() - inklusive derselben
    FFA-Sonderregel: None, wenn alle aktiven Spieler eine je eigene,
    einzigartige Nummer haben (kein einziges echtes Team im Match)."""
    active_slots = [i for i, v in all_values if is_active_slot(v)]
    if not active_slots:
        return None

    groups = {}
    for i in active_slots:
        try:
            groups[i] = pm.read_uint(DIPLOMATIC_GROUP_ARRAY_ADDR + i * 4)
        except Exception:
            return None

    if len(set(groups.values())) == len(groups):
        return None  # FFA: jeder Wert einzigartig, keine echten Teams

    return groups


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


def _untrusted_active_count(all_values):
    return sum(1 for _, v in all_values if is_active_slot(v) and not v.get("session_ok"))


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
    Zeiger (HD) bzw. die feste Roster-Adresse ohne Zeiger-Indirektion
    (Extreme, siehe ROSTER_BASE_DIRECT). Gibt dict slot->name zurück; leer
    bei Fehlern (z.B. noch keine Karte geladen) oder wenn für die laufende
    Version keine der beiden Adressen bekannt ist - dann fällt die Anzeige
    auf slot_label() zurück."""
    try:
        if ROSTER_BASE_DIRECT is not None:
            roster_base = ROSTER_BASE_DIRECT
        elif ROSTER_POINTER_ADDR is not None:
            roster_base = pm.read_int(ROSTER_POINTER_ADDR)
            if roster_base == 0:
                return {}
        else:
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
    if _untrusted_active_count(all_values) > 1:
        return {"players": [], "updated_at": time.time(), "blocked": True}

    attack_status = attack_status or {}
    players = []
    for i, values in all_values:
        if not is_active_slot(values):
            continue
        label = lord_labels.get(i, slot_label(i))
        # Live-Max-HP (aus derselben Objekttabelle wie lord_hp) ist
        # robuster als die namensbasierte Berechnung (die am bekannten
        # Substring-Matching-Bug in get_lord_strength_multiplier() hängt) -
        # bevorzugt verwenden, namensbasiert nur als Rückfallebene, solange
        # poll_lord_hp() den Datensatz für diesen Spieler noch nicht
        # gefunden hat (z.B. kurz nach Match-Start).
        lord_max_hp = values.get("lord_hp_max_live") or get_lord_max_hp(label)
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
            # Vom "besiegt, aber Karte bleibt sichtbar"-Filter in worker.py
            # gesetzt (siehe dortiger _debounce_active_slots-Kommentar) -
            # steuert im Frontend das Ausgrauen statt Entfernen der Karte.
            # Fehlt values der Schluessel (z.B. beim CLI-Debug-Gebrauch ohne
            # den Worker-Filter), ist False der sichere Standard.
            "is_defeated": bool(values.get("is_defeated")),
            "units": {k: values.get(k) for k in UNIT_TYPE_KEYS},
        }
        status_entry = attack_status.get(i)
        if status_entry is not None:
            entry["attack_status"] = status_entry.get("status")
            entry["attack_base"] = status_entry.get("base")
            # Neu (Lead 2, 2026-09-06): Bereich für die vorhergesagte
            # nächste Angriffsgröße, falls die Live-Eskalation lesbar war
            # (HD; bei Extreme/unbekannter Persona bleiben beide None,
            # overlay.html ignoriert fehlende Felder einfach). Noch nicht
            # in der UI dargestellt - hier nur mit ausgeliefert, damit
            # eine spätere Anzeige ohne Backend-Änderung möglich ist.
            entry["attack_predicted_min"] = status_entry.get("predicted_min")
            entry["attack_predicted_max"] = status_entry.get("predicted_max")
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
            # Muss VOR read_all_players() laufen - read_player() liest die
            # hier befüllten Caches nur noch aus (siehe worker.py::_tick).
            poll_lord_hp(pm)
            poll_arab_units(pm)
            poll_monk_units(pm)
            all_values = read_all_players(pm)
            lord_labels = read_roster_names(pm)
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
