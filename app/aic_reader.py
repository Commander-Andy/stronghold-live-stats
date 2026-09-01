"""
Stronghold Crusader - AIC/Personality-Reader (Angriffs-Schwellwerte)
======================================================================
Portierte Version von shc_aic_reader.py fuer die App. Funktional
unveraendert - DEFAULT_AIC_FILE ist hier nur noch Dokumentation/Fallback
fuer den CLI-Gebrauch; die App selbst liest den Pfad immer aus der
gespeicherten Konfiguration (config.py), nie aus dieser Konstante.
"""

import json
import sys
from pathlib import Path

# Nur Doku/CLI-Fallback - die App nutzt config["aic_file_path"].
DEFAULT_AIC_FILE = (
    r"L:\SteamLibrary\steamapps\common\Stronghold Crusader Extreme"
    r"\resources\aic\vanilla.json"
)

# Eingebaute (deutsche) Standard-Anzeigenamen der Basis-Archetypen, wie sie
# LIVE im Speicher stehen - NICHT identisch mit dem "Name"-Feld in der
# .aic-Datei ("Wolf" vs. "Wolf, Herzog Volpe"). Nur bestaetigt fuer die
# deutsche Sprachversion.
DEFAULT_DISPLAY_NAMES = {
    "Rat":       "Ratte, Herzog de Puce",
    "Snake":     "Schlange, Herzog Beauregard",
    "Pig":       "Schwein, Herzog Truffe",
    "Wolf":      "Wolf, Herzog Volpe",
    "Saladin":   "Saladin \"Der Weise\"",
    "Caliph":    "Kalif \"Der Skorpion\"",
    "Sultan":    "Sultan Abdul",
    "Richard":   "Richard I Löwenherz",
    "Frederick": "Kaiser Friedrich I",
    "Phillip":   "König Philipp I",
    "Wazir":     "Wazir \"Der Schreckliche\"",
    "Emir":      "Emir Omar",
    "Nizar":     "Nizar \"Der Stille\"",
    "Sheriff":   "Sheriff von Nottingham",
    "Marshal":   "Marschall, Sir Longarm",
}

_DISPLAY_NAME_TO_ARCHETYPE = {v: k for k, v in DEFAULT_DISPLAY_NAMES.items()}

ATTACK_FIELDS = [
    "AttForceBase",
    "AttForceRandom",
    "AttForceRallyPercentage",
    "AttForceSupportAllyThreshold",
    "RecruitProbAttackDefault",
    "RecruitProbAttackWeak",
    "RecruitProbAttackStrong",
    "AttMaxDefault",
    "AttMainGroupsCount",
    "TargetChoice",
]


def load_aic(path):
    """Laedt eine .aic-JSON-Datei. strict=False, weil die Beschreibungsfelder
    echte Zeilenumbrueche in Strings enthalten (kein gueltiges Standard-JSON)."""
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f, strict=False)


def extract_lords(data):
    """Gibt eine Liste von (name, custom_name, personality_dict) zurueck."""
    result = []
    for char in data.get("AICharacters", []):
        name = char.get("Name", "?")
        custom_name = char.get("CustomName", "")
        personality = char.get("Personality", {})
        result.append((name, custom_name, personality))
    return result


def find_personality(data, identifier):
    """Sucht einen Lord anhand seines Namens ODER CustomNames (case-insensitive)."""
    identifier = identifier.strip().lower()
    for name, custom_name, personality in extract_lords(data):
        if name.strip().lower() == identifier or (custom_name or "").strip().lower() == identifier:
            return name, custom_name, personality
    return None


def resolve_personality(data, live_name):
    """Wie find_personality(), faellt aber zusaetzlich auf die eingebauten
    Standard-Anzeigenamen zurueck, falls der Live-Name nicht direkt als
    Name/CustomName in der .aic-Datei steht (typisch bei Vanilla-Lords)."""
    found = find_personality(data, live_name)
    if found is not None:
        return found
    archetype = _DISPLAY_NAME_TO_ARCHETYPE.get(live_name)
    if archetype is not None:
        return find_personality(data, archetype)
    return None


def attack_summary(personality):
    """Extrahiert nur die angriffsrelevanten Felder aus einer Personality."""
    return {field: personality.get(field) for field in ATTACK_FIELDS}


def describe(name, custom_name, summary):
    label = f"{name} ({custom_name})" if custom_name else name
    base = summary["AttForceBase"]
    rand = summary["AttForceRandom"]
    rally = summary["AttForceRallyPercentage"]
    probs = (
        summary["RecruitProbAttackDefault"],
        summary["RecruitProbAttackWeak"],
        summary["RecruitProbAttackStrong"],
    )
    target = summary["TargetChoice"]
    max_army = summary["AttMaxDefault"]

    lines = [f"{label}:"]
    if base is not None:
        lines.append(f"  Angriffs-Schwelle (Truppenzahl): {base} +/- {rand}")
    if rally is not None:
        lines.append(f"  Sammelpunkt-Quote vor Losschlagen: {rally}%")
    if any(p is not None for p in probs):
        lines.append(f"  Chance auf Angriffsarmee (Normal/Schwach/Stark): {probs[0]}/{probs[1]}/{probs[2]}")
    if max_army is not None:
        lines.append(f"  Max. Armeegroesse: {max_army}")
    if target is not None:
        lines.append(f"  Angriffsziel-Praeferenz: {target}")
    return "\n".join(lines)


def process_file(path):
    try:
        data = load_aic(path)
    except Exception as e:
        print(f"[FEHLER] {path}: {e}")
        return
    lords = extract_lords(data)
    if not lords:
        return
    print(f"\n=== {path.name} ===")
    for name, custom_name, personality in lords:
        summary = attack_summary(personality)
        print(describe(name, custom_name, summary))


def main():
    args = sys.argv[1:]

    if args and args[0] == "--dir":
        if len(args) < 2:
            print("Nutzung: python aic_reader.py --dir <ordner>")
            return
        search_dir = Path(args[1])
        files = sorted(search_dir.rglob("*.json"))
    elif args:
        files = [Path(a) for a in args]
    else:
        default_file = Path(DEFAULT_AIC_FILE)
        if not default_file.exists():
            print(f"[FEHLER] Standarddatei nicht gefunden: {default_file}")
            print("Nutzung: python aic_reader.py <datei.json> | --dir <ordner>")
            return
        files = [default_file]

    if not files:
        print("Keine .json-Dateien gefunden.")
        return

    for f in files:
        process_file(f)


if __name__ == "__main__":
    main()
