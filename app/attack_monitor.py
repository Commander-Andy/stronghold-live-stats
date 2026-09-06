"""
Stronghold Crusader HD/Extreme - Angriffs-Monitor
=============================================
Live-first seit 2026-09-06: compute_player_attack_status() liest
AttForceBase/AttForceRandom jetzt primär direkt aus der KI-Tabelle im
Spielspeicher (reader.read_ai_personality_table(), Lead 1) statt aus
einer vom Nutzer geladenen .aic-Datei - reflektiert damit automatisch den
tatsächlich laufenden AI-Mod, ohne dass eine passende Datei geladen sein
muss. Die alte datei-basierte Lookup (aic_reader.py) bleibt als Fallback
bestehen, falls die Live-Tabelle nicht verfügbar ist (aktuell: Extreme,
oder ein Custom-Lord-Name ohne bekannte Persona).

Zusätzlich neu: die Schwelle für "ANGRIFF BEVORSTEHEND" ist jetzt DYNAMISCH
statt starr, wenn die Live-Angriffs-Eskalation lesbar ist (Lead 2, siehe
reader.predict_next_attack_range()) - die KI braucht nach jedem
tatsächlichen Angriff spürbar mehr Truppen für den nächsten, das war vor
2026-09-06 nicht abgebildet. Ohne lesbare Eskalation (Extreme) fällt die
Schwelle automatisch auf die alte statische base±rand-Logik zurück.

Noch mal neu, selber Tag: der Status hieß bis eben "ANGRIFF MÖGLICH" -
umbenannt zu "ANGRIFF BEVORSTEHEND", weil das eine reine VORHERSAGE ist
(Truppenzahl hat die berechnete Schwelle erreicht, kein Beweis, dass
wirklich gleich angegriffen wird - der Nutzer empfand "MÖGLICH" als zu
früh feuernd trotz an sich korrekter Berechnung). Ergänzend dazu gibt es
seit worker.py::_tick() einen zweiten, rein REAKTIVEN Status "GREIFT AN"
(nicht hier, sondern dort gesetzt) - der überschreibt den Status für 10s,
sobald der Live-Eskalationszähler (attackNumber) tatsächlich hochzählt,
also der Angriff bereits nachweislich läuft statt nur vorhergesagt zu
sein. Beide zusammen: "ANGRIFF BEVORSTEHEND" als Frühwarnung, "GREIFT AN"
als Bestätigung.
"""

import sys
import time

import aic_reader as aic
import reader as res

# TEMPORÄR DEAKTIVIERT (Nutzerwunsch 2026-09-06): die vorhersagebasierten
# Status-Stufen ("baut auf"/"nähert sich"/"ANGRIFF BEVORSTEHEND") laufen
# nur für Spieler mit aufgelöster Personality - bei echten Custom-KIs
# ohne bekannten Namen (siehe [[project_aic_memory_field_investigation]],
# "Templer"-Fall) bleiben sie für immer bei "?" hängen, während "GREIFT
# AN" (komplett personality-unabhängig, siehe worker.py) für ALLE
# funktioniert. Der Nutzer wollte dieses uneinheitliche Verhalten für
# eine Runde ausblenden, statt manche Spieler mit mehr Infos zu zeigen als
# andere - bis die Personality-Auflösung auch für Custom-KIs nachgezogen
# ist. Auf True setzen, um die Vorhersage-Stufen wieder einzuschalten -
# die Logik selbst (status_for() etc.) ist unverändert, nur dieser
# Schalter ist neu. "GREIFT AN" selbst ist von diesem Schalter NICHT
# betroffen (wird in worker.py unabhängig davon gesetzt/überschrieben).
PREDICTIVE_TIERS_ENABLED = False


def status_for(troops, base, low, high):
    """low/high sind normalerweise base∓rand (altes Verhalten), können
    aber von einer dynamischen Vorhersage überschrieben werden (siehe
    compute_player_attack_status) - die Tier-Logik selbst ist unverändert
    gegenüber der ursprünglichen Version."""
    if base is None:
        return "?"
    if troops >= high:
        return "ANGRIFF BEVORSTEHEND"
    if troops >= low:
        return "nähert sich"
    if base > 0 and troops >= 0.7 * low:
        return "baut auf"
    return "ruhig"


def compute_player_attack_status(pm, player_index, name, troops, gold, aic_data=None):
    """Live-first (siehe Moduldoku oben). `pm` und `player_index` werden
    für die Live-Tabellen-/Eskalations-Reads gebraucht; `aic_data` ist
    optional und nur noch der Datei-Fallback (kann None sein, wenn der
    Nutzer keine .aic-Datei geladen hat - live funktioniert unabhängig
    davon). Gibt immer ein dict zurück (auch ohne Treffer).

    WICHTIG (Bug behoben 2026-09-06, siehe
    [[project_aic_memory_field_investigation]]): `attack_number` wird
    IMMER gelesen, auch wenn `personality_found` False bleibt (Name passt
    zu keiner bekannten Persona) - vorher gab es hier einen fruehen
    Rücksprung VOR dem Eskalations-Read, was bedeutete, dass "GREIFT AN"
    fuer JEDEN Spieler mit unauflösbarem Namen niemals auslösen konnte,
    obwohl attack_number rein pro Spieler-Slot funktioniert und mit der
    Personality-Auflösung gar nichts zu tun hat (Nutzer vermutete
    faelschlich ein Timing-/Polling-Problem - es war dieser Gating-Bug)."""
    attack_number, addtroops = (None, None)
    if troops is not None:
        attack_number, addtroops = res.read_attack_escalation(pm, player_index)

    if not name or troops is None:
        return {
            "status": "?", "base": None, "rand": 0, "personality_found": False,
            "attack_number": attack_number, "predicted_min": None, "predicted_max": None,
        }

    base, rand = res.read_ai_personality_table(pm, player_index, name)
    personality_found = base is not None

    if not personality_found and aic_data is not None:
        found = aic.resolve_personality(aic_data, name)
        if found is not None:
            _, _, personality = found
            summary = aic.attack_summary(personality)
            base = summary.get("AttForceBase")
            rand = summary.get("AttForceRandom") or 0
            personality_found = base is not None

    if not personality_found:
        return {
            "status": "?", "base": None, "rand": 0, "personality_found": False,
            "attack_number": attack_number, "predicted_min": None, "predicted_max": None,
        }

    rand = rand or 0
    low, high = base - rand, base + rand

    predicted_min, predicted_max = res.predict_next_attack_range(base, rand, attack_number, gold)
    if predicted_min is not None:
        low, high = predicted_min, predicted_max

    status = status_for(troops, base, low, high) if PREDICTIVE_TIERS_ENABLED else "?"
    return {
        "status": status,
        "base": base,
        "rand": rand,
        "personality_found": True,
        "attack_number": attack_number,
        "predicted_min": predicted_min,
        "predicted_max": predicted_max,
    }


# --- Nur für CLI-Debug (python app/attack_monitor.py), nicht von der App genutzt ---

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

    print("Starte Live-Überwachung (Strg+C zum Beenden)...\n")
    try:
        while True:
            all_values = res.read_all_players(pm)
            active = [(i, v) for i, v in all_values if res.is_active_slot(v)]
            names = res.read_roster_names(pm)

            parts = []
            for i, values in active:
                # Slot 0 NICHT mehr pauschal uebersprungen - siehe
                # worker.py::_tick() fuer die Begruendung (KI-vs-KI-Matches
                # koennen Slot 0 als KI haben, ein echter Mensch bekommt
                # ohnehin automatisch "?" zurueck).
                troops = values.get("troops_total")
                gold = values.get("gold")
                name = names.get(i)
                result = compute_player_attack_status(pm, i, name, troops, gold, aic_data)
                if not result["personality_found"]:
                    parts.append(f"[{name}] Truppen:{troops:>3} (kein Personality-Eintrag gefunden)")
                    continue
                pred = ""
                if result["predicted_min"] is not None:
                    pred = f", nächster ~{result['predicted_min']}-{result['predicted_max']}"
                parts.append(f"[{name}] Truppen:{troops:>3}/{result['base']} ({result['status']}{pred})")

            print(" | ".join(parts))
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
