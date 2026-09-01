"""Hintergrund-Thread: liest laufend Spieldaten aus und haelt die aktuelle
Overlay-Payload in einem Thread-sicheren StateStore bereit, den server.py
fuer /overlay.json ausliest.

Verbindungsfehler (Spiel nicht offen, Spiel waehrenddessen geschlossen)
fuehren NIE zu einem Absturz des Threads - stattdessen wird der Status auf
"waiting_for_game"/"error" gesetzt und der naechste Tick versucht es erneut.
Das ist wichtig, weil dieses Tool an gewoehnliche Endanwender verteilt wird,
bei denen "Spiel noch nicht gestartet" der Normalfall beim App-Start ist.
"""

import threading
import time

import aic_reader as aic
import attack_monitor
import reader as res


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
        """Wird vom Server nach einer Settings-Aenderung aufgerufen - laedt
        die aic-Datei neu, falls sich der Pfad geaendert hat."""
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
        return True

    def _tick(self):
        if not self._ensure_connected():
            return

        try:
            all_values = res.read_all_players(self._pm)
            lord_labels = res.read_roster_names(self._pm)
        except Exception as e:
            # Spiel vermutlich geschlossen - Verbindung verwerfen, naechster
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
        self.state.set_payload(payload)

    def _poll_monks_only(self):
        """Nur das Moenche-Ereignis-Log abfragen, ohne den vollen Tick
        (kein read_all_players/roster/payload-Aufbau). Wird deutlich
        oefter aufgerufen als der volle Tick, weil das Log ein kleiner,
        sich schnell ueberschreibender Zaehl-Stapel ist (siehe
        reader.poll_monk_events Docstring) - je seltener wir schauen,
        desto eher geht ein Moench-Ereignis unbemerkt verloren, wenn
        zwischendurch viele andere Einheiten gebaut/verloren werden."""
        if self._pm is None:
            return
        try:
            res.poll_monk_events(self._pm)
        except Exception:
            pass  # naechster _tick() erkennt einen echten Verbindungsverlust

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
                    # Absicherung: der Thread darf unter keinen Umstaenden
                    # sterben, auch nicht bei einem unerwarteten Fehler.
                    self.state.set_error(str(e))
                    self._pm = None
                last_full_tick = now
            else:
                self._poll_monks_only()
            self._stop_event.wait(min(self.MONK_POLL_INTERVAL_S, interval))
