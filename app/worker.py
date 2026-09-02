"""Hintergrund-Thread: liest laufend Spieldaten aus und hält die aktuelle
Overlay-Payload in einem Thread-sicheren StateStore bereit, den server.py
für /overlay.json ausliest.

Verbindungsfehler (Spiel nicht offen, Spiel währenddessen geschlossen)
führen NIE zu einem Absturz des Threads - stattdessen wird der Status auf
"waiting_for_game"/"error" gesetzt und der nächste Tick versucht es erneut.
Das ist wichtig, weil dieses Tool an gewöhnliche Endanwender verteilt wird,
bei denen "Spiel noch nicht gestartet" der Normalfall beim App-Start ist.
"""

import threading
import time

import aic_reader as aic
import attack_monitor
import reader as res
import strength_score


class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._payload = {"players": [], "updated_at": 0}
        self._status = "waiting_for_game"
        self._last_error = None
        self._aic_loaded = False

    def snapshot(self):
        with self._lock:
            return dict(self._payload)

    def status(self):
        with self._lock:
            return {
                "connected": self._status == "ok",
                "process_found": self._status != "waiting_for_game",
                "aic_loaded": self._aic_loaded,
                "last_error": self._last_error,
                "player_count": len(self._payload.get("players", [])),
            }

    def set_payload(self, payload):
        with self._lock:
            self._payload = payload
            self._status = "ok"
            self._last_error = None

    def set_waiting(self, message=None):
        with self._lock:
            self._status = "waiting_for_game"
            self._last_error = message

    def set_error(self, message):
        with self._lock:
            self._status = "error"
            self._last_error = message

    def set_aic_loaded(self, loaded: bool):
        with self._lock:
            self._aic_loaded = loaded


class Worker:
    def __init__(self, config: dict, state: StateStore):
        self._config = config
        self.state = state
        self._aic_data = None
        self._stop_event = threading.Event()
        self._thread = None
        self._pm = None
        self._strength_tracker = strength_score.StrengthTracker()
        # Für die (unvollständige, siehe _tick) Match-Neustart-Erkennung:
        # ob im letzten Tick mindestens ein aktiver Spieler-Slot da war.
        self._had_active_players = False
        self._reload_aic()

    def _reload_aic(self):
        path = self._config.get("aic_file_path")
        if not path:
            self._aic_data = None
            self.state.set_aic_loaded(False)
            return
        try:
            self._aic_data = aic.load_aic(path)
            self.state.set_aic_loaded(True)
        except Exception:
            self._aic_data = None
            self.state.set_aic_loaded(False)

    def update_config(self, new_config: dict):
        """Wird vom Server nach einer Settings-Änderung aufgerufen - lädt
        die aic-Datei neu, falls sich der Pfad geändert hat."""
        old_aic_path = self._config.get("aic_file_path")
        self._config = new_config
        if new_config.get("aic_file_path") != old_aic_path:
            self._reload_aic()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _ensure_connected(self):
        if self._pm is not None:
            return True
        pm, err = res.connect(verbose=False)
        if pm is None:
            self.state.set_waiting(err)
            return False
        self._pm = pm
        res.reset_monk_tracking()
        self._strength_tracker.reset()
        self._had_active_players = False
        return True

    def _tick(self):
        if not self._ensure_connected():
            return

        try:
            all_values = res.read_all_players(self._pm)
            lord_labels = res.read_roster_names(self._pm)
        except Exception as e:
            # Spiel vermutlich geschlossen - Verbindung verwerfen, nächster
            # Tick versucht einen frischen connect().
            self._pm = None
            self.state.set_waiting(f"Verbindung verloren: {e}")
            return

        attack_status = {}
        if self._aic_data is not None:
            for i, values in all_values:
                if i == 0 or not res.is_active_slot(values):
                    continue
                name = lord_labels.get(i)
                troops = values.get("troops_total")
                attack_status[i] = attack_monitor.compute_player_attack_status(
                    self._aic_data, name, troops
                )

        payload = res.build_overlay_payload(all_values, lord_labels, attack_status)

        # Heuristik für "neues Match im selben, weiterlaufenden Spielprozess
        # gestartet": alle Slots waren zuletzt inaktiv (Kartenübergang/
        # Hauptmenü) und jetzt ist mindestens einer wieder aktiv. Anders als
        # der Reconnect-Reset oben (sicher, da an einen echten Verbindungs-
        # verlust gekoppelt) ist das nur eine Annahme - siehe
        # research/shc_overlay_status.md für die offene Verifikationsfrage,
        # ob population_capacity zwischen Matches tatsächlich für alle
        # Slots kurz auf 0 fällt.
        has_active = bool(payload["players"])
        if has_active and not self._had_active_players:
            self._strength_tracker.reset()
        self._had_active_players = has_active

        team_assignment = self._config.get("team_assignment", {})
        strengths, _ = strength_score.update_and_get_strengths(payload["players"], self._strength_tracker)
        strength_score.apply_win_probabilities(payload["players"], team_assignment, strengths)
        payload["sides"] = strength_score.compute_side_summary(payload["players"], team_assignment, strengths)
        self.state.set_payload(payload)

    def _poll_monks_only(self):
        """Nur das Mönche-Ereignis-Log abfragen, ohne den vollen Tick
        (kein read_all_players/roster/payload-Aufbau). Wird deutlich
        öfter aufgerufen als der volle Tick, weil das Log ein kleiner,
        sich schnell überschreibender Zähl-Stapel ist (siehe
        reader.poll_monk_events Docstring) - je seltener wir schauen,
        desto eher geht ein Mönch-Ereignis unbemerkt verloren, wenn
        zwischendurch viele andere Einheiten gebaut/verloren werden."""
        if self._pm is None:
            return
        try:
            res.poll_monk_events(self._pm)
        except Exception:
            pass  # nächster _tick() erkennt einen echten Verbindungsverlust

    MONK_POLL_INTERVAL_S = 0.15

    def _run(self):
        last_full_tick = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            interval = self._config.get("poll_interval_ms", 500) / 1000.0
            if now - last_full_tick >= interval:
                try:
                    self._tick()
                except Exception as e:
                    # Absicherung: der Thread darf unter keinen Umständen
                    # sterben, auch nicht bei einem unerwarteten Fehler.
                    self.state.set_error(str(e))
                    self._pm = None
                last_full_tick = now
            else:
                self._poll_monks_only()
            self._stop_event.wait(min(self.MONK_POLL_INTERVAL_S, interval))
