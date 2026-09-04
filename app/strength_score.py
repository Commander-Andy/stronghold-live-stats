"""
Live-Stärke-Score & Gewinnwahrscheinlichkeit (EXPERIMENTELL, erste Version)
============================================================================
Setzt den Entwurf aus research/shc_win_probability_design.md um. Berechnet
pro Spieler(-Slot) einen heuristischen "Stärke"-Wert aus Militär + Wirtschaft
+ deren Wachstumstrend über die letzten Minuten, und daraus eine grobe
Gewinnwahrscheinlichkeit gegen den Rest des Feldes (bei genau 2 Seiten -
Spielern oder Teams - mathematisch identisch zu einer echten 1v1-
Gewinnwahrscheinlichkeit, bei mehr Seiten ein "diese Seite gegen den Rest
kombiniert"-Wert statt einer strengen N-Wege-Verteilung).

WICHTIG: fast jede Konstante hier ist ein PLATZHALTER ohne echte
Kalibrierung (siehe Kommentare unten und das Design-Dokument) - Ausnahme
sind die Einheiten-Kampfwerte, die aus einem echten Liga-Regelwerk stammen
(als reine Kampfwert-SKALA, nicht als deren Budget-System - Standard-KIs
halten sich an dieses Budget-System nicht). Nichts hier ist gegen echte
Matchausgänge kalibriert worden.
"""

import math
import time
from collections import deque

import reader as res

# --- Einheiten-Gewichte (Militär-Score) ---
# Quelle: 1-KI-Liga-Regelwerk (Discord, Custom-AI-Turnier) als relative
# Kampfwert-Skala.
UNIT_WEIGHTS = {
    "unit_archer": 1.0,
    "unit_crossbowman": 2.25,
    "unit_arab_archer": 1.5,
    "unit_slinger": 0.75,
    "unit_fire_thrower": 4.0,
    "unit_spearman": 0.5,
    "unit_maceman": 1.0,
    "unit_pikeman": 1.25,
    "unit_swordsman": 1.5,
    "unit_knight": 2.0,
    "unit_slave": 0.25,
    "unit_assassin": 1.0,
    "unit_arab_swordsman": 1.25,
    # Platzhalter, Liga-Quelle nennt widersprüchlich 0,75 UND 1,5 in
    # unterschiedlichen Kontexten - grober Mittelwert, ungeklärt.
    "unit_horse_archer": 1.1,
    # Platzhalter, keine Liga-Angabe (kein Kampf-Einheitstyp).
    "unit_laddercarrier": 0.1,
    "unit_tunneler": 0.15,
    "unit_builder": 0.0,  # rein wirtschaftlich, keine eigene Kampfkraft
}

# Platzhalter: siege_movable_total ist ein Sammelwert über 6 sehr
# unterschiedlich starke Belagerungstypen (Liga-Werte: Katapult/Tribock/
# Feuerballiste/Balliste je 5, Rammbock/Schild/Turm eher ~0) - ohne
# Live-Aufschlüsselung nur ein grob geblendeter Mittelwert. Schwächste Zahl
# in diesem ganzen Modell, siehe research/shc_opus_review_2026-09-02.md
# Abschnitt D.1 für den Plan, das durch eine echte Aufschlüsselung zu ersetzen.
SIEGE_WEIGHT_PER_UNIT = 3.5

MONK_WEIGHT = 0.75
# monks_trained war früher kumulativ seit Tool-Start (daher ein Abwertungs-
# faktor gegen zunehmende Überbewertung) - seit dem Umbau auf den Objekt-
# tabellen-Zensus (2026-09-04) ist es ein echter Live-Bestand wie jede
# andere Einheit, braucht also keine Sonderbehandlung mehr.

# Waffen-/Rüstungslager sind noch keine Truppen (kein freier Bauer, keine
# Trainingszeit) - stark abgewertete "potenzielle Truppen".
STOCKPILE_DISCOUNT = 0.3
ARMOR_DISCOUNT = 0.15

STOCKPILE_UNIT_WEIGHTS = {
    "bows": UNIT_WEIGHTS["unit_archer"],
    "crossbows": UNIT_WEIGHTS["unit_crossbowman"],
    "spears": UNIT_WEIGHTS["unit_spearman"],
    "pikes": UNIT_WEIGHTS["unit_pikeman"],
    "maces": UNIT_WEIGHTS["unit_maceman"],
    "swords": UNIT_WEIGHTS["unit_swordsman"],
}
_MELEE_KEYS = ["unit_spearman", "unit_maceman", "unit_pikeman", "unit_swordsman", "unit_knight"]
AVG_MELEE_WEIGHT = sum(UNIT_WEIGHTS[k] for k in _MELEE_KEYS) / len(_MELEE_KEYS)

# --- Wirtschafts-Score ---
# ALLE Gewichte hier sind reine Größenordnungs-Platzhalter (damit Ressourcen
# im hunderter/tausender-Bereich zahlenmäßig zum Militär-Score passen), KEINE
# strategische Bewertung. Doppelzählungs-Risiko gegenüber dem Lagerwert oben
# (z.B. Holz für Belagerungsgeräte wird hier UND dort erfasst) ist bekannt
# und ungelöst, siehe Design-Dokument.
ECONOMY_WEIGHTS = {
    "gold": 0.001,
    "wood": 0.001,
    "stone": 0.001,
    "iron": 0.002,
    "food_total": 0.01,
    "population_capacity": 0.1,
}
POPULARITY_WEIGHT = 0.02  # popularity: 0..100
TAX_POPULARITY_WEIGHT = 0.02  # tax_popularity_effect: etwa -24..+7

K_ECON = 0.3  # Wirtschaft->Militär-Äquivalenz, Platzhalter ohne Herleitung

# --- Trend/Momentum ---
T_WINDOW_S = 180.0  # Rückblick-Fenster für die Trend-Berechnung
T_LOOKAHEAD_S = 300.0  # Vorausprojektion
MIN_HISTORY_SPAN_S = 5.0  # unter dieser Spanne wird Trend als 0 behandelt

# --- Lord-Stärke-Prior (schwach, klingt mit der Zeit ab) ---
T_DECAY_S = 600.0
PRIOR_WEIGHT = 0.15

# Sigmoid-Steilheit für Score-Verhältnis -> Gewinnwahrscheinlichkeit.
SIGMOID_K = 8.0


def military_score(p):
    """p: ein Eintrag aus build_overlay_payload()["players"]."""
    units = p.get("units") or {}
    score = 0.0
    for key, weight in UNIT_WEIGHTS.items():
        count = units.get(key)
        if count:
            score += count * weight

    siege = p.get("siege_movable_total")
    if siege:
        score += siege * SIEGE_WEIGHT_PER_UNIT

    monks = p.get("monks_trained")
    if monks:
        score += monks * MONK_WEIGHT

    score += _stockpile_value(p)
    return score


def _stockpile_value(p):
    value = 0.0
    for key, weight in STOCKPILE_UNIT_WEIGHTS.items():
        count = p.get(key)
        if count:
            value += count * weight * STOCKPILE_DISCOUNT
    armor = (p.get("leather_armor") or 0) + (p.get("metal_armor") or 0)
    if armor:
        value += armor * AVG_MELEE_WEIGHT * ARMOR_DISCOUNT
    return value


def economy_score(p):
    score = 0.0
    for key, weight in ECONOMY_WEIGHTS.items():
        v = p.get(key)
        if v:
            score += v * weight
    pop = p.get("popularity")
    if pop:
        score += pop * POPULARITY_WEIGHT
    tax_effect = p.get("tax_popularity_effect")
    if tax_effect:
        score += tax_effect * TAX_POPULARITY_WEIGHT
    return score


class StrengthTracker:
    """Hält pro Spieler-Slot eine kurze Zeitreihe des Gesamt-Scores, um
    daraus Trend/Projektion zu berechnen. MUSS bei einem neuen Match
    zurückgesetzt werden (reset()) - sonst würde Historie aus einem
    vorherigen Match den Trend der ersten T_WINDOW_S eines neuen Matches
    verfälschen (siehe Aufrufer in worker.py für die - unvollständige,
    siehe dortige Kommentare - Erkennung eines Match-Neustarts)."""

    def __init__(self):
        self._history = {}  # slot -> deque[(timestamp, total_score)]
        self._first_seen = {}  # slot -> Zeitpunkt der ersten Beobachtung

    def reset(self):
        self._history.clear()
        self._first_seen.clear()

    def update(self, slot, total_score, now):
        hist = self._history.setdefault(slot, deque())
        hist.append((now, total_score))
        while len(hist) > 1 and now - hist[0][0] > T_WINDOW_S:
            hist.popleft()
        self._first_seen.setdefault(slot, now)

    def trend(self, slot):
        hist = self._history.get(slot)
        if not hist or len(hist) < 2:
            return 0.0
        oldest_t, oldest_v = hist[0]
        newest_t, newest_v = hist[-1]
        dt = newest_t - oldest_t
        if dt < MIN_HISTORY_SPAN_S:
            return 0.0
        return (newest_v - oldest_v) / dt

    def elapsed(self, slot, now):
        start = self._first_seen.get(slot)
        return 0.0 if start is None else now - start

    def latest_total(self, slot):
        hist = self._history.get(slot)
        return hist[-1][1] if hist else 0.0


def _lord_prior(label, elapsed_s):
    multiplier = res.get_lord_strength_multiplier(label)
    decay = max(0.0, 1.0 - elapsed_s / T_DECAY_S)
    return (multiplier - 1.0) * PRIOR_WEIGHT * decay


def _strength_for_slot(tracker, slot, label, now):
    total = tracker.latest_total(slot)
    trend = tracker.trend(slot)
    projected = total + trend * T_LOOKAHEAD_S
    prior = _lord_prior(label, tracker.elapsed(slot, now))
    return projected * (1.0 + prior)


def _sides(players, team_assignment):
    """Gruppiert Spieler zu 'Seiten': gleiche Team-Nummer = eine Seite,
    nicht zugeordnete Spieler sind je eine eigene Seite. Gibt dict
    side_key -> [slot, ...] zurück."""
    sides = {}
    for p in players:
        slot = p["slot"]
        team = team_assignment.get(str(slot))
        key = f"team:{team}" if team is not None else f"solo:{slot}"
        sides.setdefault(key, []).append(slot)
    return sides


def _sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def update_and_get_strengths(players, tracker, now=None):
    """Aktualisiert die Score-Historie im tracker (GENAU EINMAL pro Tick
    aufrufen - ruft tracker.update() pro Spieler auf, ein zweiter Aufruf im
    selben Tick würde doppelte Zeitpunkte in die Historie schreiben) und
    gibt (dict slot->Stärke-Wert, benutzter Zeitpunkt) zurück. Sowohl
    apply_win_probabilities() als auch compute_side_summary() konsumieren
    dasselbe Ergebnis, statt selbst zu aktualisieren."""
    if now is None:
        now = time.monotonic()
    if not players:
        return {}, now
    for p in players:
        total = military_score(p) + K_ECON * economy_score(p)
        tracker.update(p["slot"], total, now)
    strengths = {
        p["slot"]: _strength_for_slot(tracker, p["slot"], p.get("label"), now)
        for p in players
    }
    return strengths, now


def apply_win_probabilities(players, team_assignment, strengths):
    """Setzt pro Spieler "strength_score" (roher Stärke-Wert, v.a. zum
    Debuggen/als Tooltip) und "win_probability_percent" (0-100, diese Seite
    gegen den Rest des Feldes kombiniert - bei genau 2 Seiten eine echte
    1v1-Wahrscheinlichkeit, bei 3+ Seiten NICHT auf 100% aufsummierend,
    siehe Modul-Docstring). Mutiert die Einträge in players in place und
    gibt players zurück. EXPERIMENTELL - siehe Modul-Docstring."""
    if not players:
        return players

    sides = _sides(players, team_assignment)
    side_strength = {key: sum(strengths[slot] for slot in slots) for key, slots in sides.items()}
    slot_to_side = {slot: key for key, slots in sides.items() for slot in slots}

    for p in players:
        own = side_strength[slot_to_side[p["slot"]]]
        rest = sum(v for k, v in side_strength.items() if k != slot_to_side[p["slot"]])
        if own + rest <= 0:
            prob = 0.5
        else:
            diff_ratio = (own - rest) / (own + rest)
            prob = _sigmoid(SIGMOID_K * diff_ratio)
        p["strength_score"] = round(strengths[p["slot"]], 1)
        p["win_probability_percent"] = round(prob * 100)

    return players


def compute_side_summary(players, team_assignment, strengths):
    """Seiten-Zusammenfassung fürs Balken-Overlay (win_bar.html): pro Seite
    ein Anteil ("share_percent", summiert IMMER auf ~100% - anders als
    win_probability_percent oben, das bei 3+ Seiten nicht aufsummiert) plus
    zusätzlich dieselbe "gegen den Rest"-Wahrscheinlichkeit wie im
    Spieler-Rahmen (für den Einzel-Balken-pro-Fraktion-Modus). Reihenfolge:
    nach Team-Nummer aufsteigend, WENN jeder aktive Spieler einem Team
    zugeordnet ist (damit der Balken nicht bei jedem Vorsprungswechsel die
    Seiten tauscht) - sonst nach Stärke absteigend (FFA ohne Teams hat kein
    natürliches Links/Rechts). "representative_slot" (niedrigste Slot-Nummer
    der Seite) ist als stabiler Farb-Anker gedacht, siehe win_bar.html."""
    if not players:
        return []

    labels = {p["slot"]: p.get("label") for p in players}
    sides = _sides(players, team_assignment)
    side_strength = {key: sum(strengths[slot] for slot in slots) for key, slots in sides.items()}
    total = sum(side_strength.values())
    all_teamed = all(key.startswith("team:") for key in sides)

    entries = []
    for key, slots in sides.items():
        own = side_strength[key]
        rest = total - own
        if own + rest <= 0:
            win_prob = 50.0
        else:
            win_prob = _sigmoid(SIGMOID_K * ((own - rest) / (own + rest))) * 100
        share = (own / total * 100) if total > 0 else (100.0 / len(sides))
        rep_slot = min(slots)
        team_number = int(key.split(":", 1)[1]) if key.startswith("team:") else None
        entries.append({
            "key": key,
            "label": f"Team {team_number}" if team_number is not None else labels.get(rep_slot),
            "slots": sorted(slots),
            "representative_slot": rep_slot,
            "team_number": team_number,
            "share_percent": round(share, 1),
            "win_probability_percent": round(win_prob),
        })

    if all_teamed:
        entries.sort(key=lambda e: e["team_number"])
    else:
        entries.sort(key=lambda e: side_strength[e["key"]], reverse=True)

    return entries
