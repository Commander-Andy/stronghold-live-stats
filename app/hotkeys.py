"""Globale Tastenkürzel zum Laden eines Speicherplatzes (siehe config.py
DEFAULT_CONFIG["saved_layouts"]) - funktionieren auch während Stronghold
Crusader den Fokus hat, damit man zum Umschalten nicht extra aus dem Spiel
raus-tabben muss (das pausiert das Spiel). Nutzt die "keyboard"-Bibliothek
für den system-weiten Hook - braucht dafür Administrator-Rechte, die diese
App wegen pymem ohnehin schon hat.

Absichtlich NUR zum Laden da (kein Speichern per Hotkey), siehe Nutzer-
Entscheidung 2026-09-08.
"""

import threading

try:
    import keyboard
except Exception:
    keyboard = None

_lock = threading.Lock()
_registered_hotkeys = []  # Liste der von keyboard.add_hotkey() gelieferten Handler


def available() -> bool:
    """False z.B. wenn die Bibliothek fehlt oder der Prozess nicht die
    nötigen Rechte für einen globalen Tastatur-Hook hat."""
    return keyboard is not None


def unregister_all():
    if keyboard is None:
        return
    with _lock:
        for handler in _registered_hotkeys:
            try:
                keyboard.remove_hotkey(handler)
            except (KeyError, ValueError):
                pass
        _registered_hotkeys.clear()


def register_from_config(cfg: dict, on_load):
    """Registriert alle in cfg["saved_layouts"] gesetzten Hotkeys neu
    (ersetzt komplett die vorherige Registrierung - einfacher und robuster
    als ein Diff, und wird nur bei tatsächlichen Config-Änderungen
    aufgerufen, also nicht performance-kritisch).

    on_load(slot_index) wird im Kontext des globalen Hooks aufgerufen,
    sobald die zugehörige Tastenkombination gedrückt wird."""
    if keyboard is None:
        return
    unregister_all()
    slots = cfg.get("saved_layouts") or []
    with _lock:
        for i, slot in enumerate(slots):
            if not slot:
                continue
            hotkey = slot.get("hotkey")
            if not hotkey:
                continue
            try:
                handler = keyboard.add_hotkey(hotkey, lambda idx=i: on_load(idx))
                _registered_hotkeys.append(handler)
            except Exception:
                # Ungueltige/nicht registrierbare Kombination - lieber
                # stillschweigend ueberspringen als die ganze App zum
                # Absturz zu bringen (z.B. wenn eine andere Anwendung
                # dieselbe Kombination bereits exklusiv belegt hat).
                pass
