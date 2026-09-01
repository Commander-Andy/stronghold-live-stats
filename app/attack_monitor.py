"""
Stronghold Crusader HD - Angriffs-Monitor
=============================================
Portierte Version von shc_attack_monitor.py. status_for() ist unveraendert.
Die frueher inline in main()'s Schleife lebende Pro-Spieler-Logik wurde in
compute_player_attack_status() extrahiert, damit worker.py sie fuer jeden
aktiven Nicht-Ich-Slot aufrufen und das Ergebnis in dieselbe Overlay-Payload
mergen kann, die auch die Ressourcen/Truppen liefert.
"""

import sys
import time

import aic_reader as aic
import reader as res


def status_for(troops, base, rand):
    if base is None:
        return "?"
    low = base - rand
    high = base + rand
    if troops >= high:
        return "ANGRIFF MOEGLICH"
    if troops >= low:
        return "naehert sich"
    if base > 0 and troops >= 0.7 * low:
        return "baut auf"
    return "ruhig"


def compute_player_attack_status(aic_data, name, troops):
    """Loest den Live-Namen zu einer .aic-Personality auf und berechnet den
    Angriffsstatus. Gibt immer ein dict zurueck (auch ohne Treffer)."""
    if aic_data is None or not name or troops is None:
        return {"status": "?", "base": None, "rand": 0, "personality_found": False}

    found = aic.resolve_personality(aic_data, name)
    if found is None:
        return {"status": "?", "base": None, "rand": 0, "personality_found": False}

    _, _, personality = found
    summary = aic.attack_summary(personality)
    base = summary.get("AttForceBase")
    rand = summary.get("AttForceRandom") or 0
    status = status_for(troops, base, rand)
    return {"status": status, "base": base, "rand": rand, "personality_found": True}


# --- Nur fuer CLI-Debug (python app/attack_monitor.py), nicht von der App genutzt ---

def main():
    aic_path = sys.argv[1] if len(sys.argv) > 1 else aic.DEFAULT_AIC_FILE
    try:
        aic_data = aic.load_aic(aic_path)
    except Exception as e:
        print(f"[FEHLER] Konnte aic-Datei nicht laden ({aic_path}): {e}")
        return
    print(f"Angriffs-Schwellwerte werden aus '{aic_path}' nachgeschlagen.\n")

    pm, err = res.connect()
    if pm is None:
        print(f"[FEHLER] {err}")
        return

    print("Starte Live-Ueberwachung (Strg+C zum Beenden)...\n")
    try:
        while True:
            all_values = res.read_all_players(pm)
            active = [(i, v) for i, v in all_values if res.is_active_slot(v)]
            names = res.read_roster_names(pm)

            parts = []
            for i, values in active:
                if i == 0:
                    continue  # du selbst hast keine Angriffs-Schwelle
                troops = values.get("troops_total")
                name = names.get(i)
                result = compute_player_attack_status(aic_data, name, troops)
                if not result["personality_found"]:
                    parts.append(f"[{name}] Truppen:{troops:>3} (kein Personality-Eintrag gefunden)")
                    continue
                parts.append(f"[{name}] Truppen:{troops:>3}/{result['base']} ({result['status']})")

            print(" | ".join(parts))
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
