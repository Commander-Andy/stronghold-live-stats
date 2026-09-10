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
        # Effektive Team-Zuordnung (manuell falls gesetzt, sonst automatisch
        # erkannt falls eingefroren, sonst leer) - server.py serviert das
        # statt der rohen Config, wenn Overlay/Übersicht danach fragen. Siehe
        # Worker._tick() für die Merge-Logik.
        self._effective_team_assignment = {}

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
                # Solange die Vorhersage-Tiers global deaktiviert sind
                # (attack_monitor.PREDICTIVE_TIERS_ENABLED), ist die
                # .aic-Datei nirgends wirksam (GREIFT AN braucht sie nicht,
                # ist Persona-unabhängig) - die Einstellungsseite blendet
                # den Datei-Auswahl-Bereich dann aus, siehe settings.html.
                "predictive_tiers_enabled": attack_monitor.PREDICTIVE_TIERS_ENABLED,
            }

    def set_payload(self, payload):
        with self._lock:
            self._payload = payload
            self._status = "ok"
            self._last_error = None

    def set_blocked(self):
        with self._lock:
            self._payload = {"players": [], "updated_at": time.time(), "blocked": True}
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

    def set_effective_team_assignment(self, assignment: dict):
        with self._lock:
            self._effective_team_assignment = assignment

    def get_effective_team_assignment(self):
        with self._lock:
            return dict(self._effective_team_assignment)


class Worker:
    def __init__(self, config: dict, state: StateStore):
        self._config = config
        self.state = state
        self._aic_data = None
        self._stop_event = threading.Event()
        self._thread = None
        self._pm = None
        self._strength_tracker = strength_score.StrengthTracker()
        # Match-Neustart-Erkennung (siehe _tick): Anzahl Ticks IN FOLGE
        # ohne einen echt aktiven Spieler. Erst wenn danach wieder Spieler
        # aktiv werden UND die Pause lang genug war (NEW_MATCH_INACTIVE_TICKS)
        # gilt das als neues Match und die Tracker/besiegten Karten werden
        # geleert. Ein kurzer Aussetzer (alle Slots glitchen 1-2 Ticks
        # gleichzeitig auf "inaktiv") loest so KEINEN Reset mehr aus, der
        # sonst legitim besiegte Karten mitten im Spiel wegraeumte
        # (Nutzer 2026-09-10). Startwert hoch, damit der erste aktive Tick
        # die Tracker sauber initialisiert.
        self._inactive_streak = 999
        # Stall-Erkennung: aendert sich bei KEINEM aktiven, nicht-besiegten
        # Spieler laenger als STALL_SECONDS irgendein Kernwert, ist das Spiel
        # vermutlich vorbei oder pausiert -> payload["stalled"] = True, das
        # Frontend graut dann alles aus (Karten bleiben aber sichtbar).
        self._last_activity_ts = 0.0
        self._last_activity_sig = None
        self._raw_activity_sig = None
        # Fuer den Verschwinden-Filter (siehe _debounce_active_slots): slot
        # -> zuletzt bestaetigte values-dict bzw. Anzahl aufeinanderfolgender
        # Polls, in denen der Slot faelschlich als inaktiv erkannt wurde.
        self._last_confirmed_active_values = {}
        self._pending_inactive_counts = {}
        # Fuer den Truppentyp-Aufschluesselung-Filter (siehe
        # _debounce_unit_breakdown): slot -> {unit_key -> Halte-Zustand}
        # (siehe _hold_on_drop).
        self._unit_hold_state = {}
        # Fuer die reaktive "GREIFT AN"-Anzeige (siehe _tick): slot ->
        # zuletzt gesehener attackNumber-Wert (Lead 2, siehe
        # [[project_aic_memory_field_investigation]]) bzw. ob es gerade
        # aktiv ist, plus der Zeitpunkt der Erkennung - "GREIFT AN" bleibt
        # danach fuer GREIFT_AN_SECONDS sichtbar, dann aus.
        self._last_attack_number = {}
        self._attacking_active = {}
        self._attack_trigger_ts = {}
        # Fuer _debounce_scalar_field() (siehe _DEBOUNCED_SCALAR_FIELDS):
        # Feldname -> {slot -> Halte-Zustand} (siehe _hold_on_drop:
        # {"disp": zuletzt angezeigter guter Wert, "since": monotonic-Start
        # des aktuellen Einbruchs oder None}).
        self._scalar_hold_state = {}
        # Slots, deren Burgherr als besiegt bestaetigt gilt (siehe
        # _debounce_active_slots) - Karte bleibt dafuer bestehen (Nutzer-
        # wunsch 2026-09-06) statt zu verschwinden, wird im Frontend
        # stattdessen ausgegraut (payload-Feld "is_defeated").
        self._confirmed_defeated = set()
        # Fuer das Staerkeindex-Throttling (siehe
        # WIN_PROB_REFRESH_S/_tick): Zeitpunkt der letzten echten Neu-
        # berechnung, plus die dabei zuletzt berechneten Werte zum
        # Zwischen-Ticks-Wiederverwenden.
        self._last_win_prob_update_ts = 0.0
        self._cached_win_probs = {}  # slot -> (strength_score, win_probability_percent)
        self._cached_sides = []
        # Sprung-Sicherung fuer den Staerkeindex (Nutzeridee
        # 2026-09-06): key -> zuletzt tatsaechlich ANGEZEIGTER Wert bzw.
        # Anzahl aufeinanderfolgender bestaetigter Spruenge. Getrennte
        # Dicts fuer Pro-Spieler (key=slot) und Pro-Seite/Team (key=side-
        # "key" aus compute_side_summary), siehe _stabilize_percent.
        self._win_prob_displayed = {}
        self._win_prob_jump_pending = {}
        self._side_win_prob_displayed = {}
        self._side_win_prob_jump_pending = {}
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
        res.reset_lord_hp_tracking()
        res.reset_arab_unit_tracking()
        res.reset_team_detection()
        self._strength_tracker.reset()
        self._inactive_streak = 999
        self._last_activity_ts = 0.0
        self._last_activity_sig = None
        self._raw_activity_sig = None
        self._last_confirmed_active_values = {}
        self._pending_inactive_counts = {}
        self._unit_hold_state = {}
        self._last_attack_number = {}
        self._attacking_active = {}
        self._attack_trigger_ts = {}
        self._scalar_hold_state = {}
        self._confirmed_defeated = set()
        self._last_win_prob_update_ts = 0.0
        self._cached_win_probs = {}
        self._cached_sides = []
        self._win_prob_displayed = {}
        self._win_prob_jump_pending = {}
        self._side_win_prob_displayed = {}
        self._side_win_prob_jump_pending = {}
        return True

    ACTIVE_CONFIRM_TICKS = 3

    # Glitch-Filter fuer die angezeigten Zahlenfelder: ein Rueckgang wird
    # bis zu SCALAR_HOLD_SECONDS lang auf dem letzten guten Wert GEHALTEN.
    # Erholt sich der Wert (steigt wieder auf >= den gehaltenen Stand)
    # vorher, war es ein Aussetzer/Zwischenwert und wurde nie angezeigt;
    # haelt der Rueckgang laenger an, gilt er als echt und kommt durch
    # (dann eben mit dieser Verzoegerung). Ein Anstieg kommt immer sofort.
    #
    # Wall-Clock statt Tick-Zaehlung (Umstellung 2026-09-10): die alte
    # Tick-Logik (GLITCH_CONFIRM_TICKS/UNIT_GLITCH_CONFIRM_TICKS) und der
    # kurzzeitig versuchte Doppel-Lese-Ansatz haben die vom Nutzer
    # gemeldeten Truppen-/Einheitentyp-Aussetzer NICHT zuverlaessig
    # abgefangen: der Fehl-Read haelt hier teils >1 Frame an (also auch
    # ueber zwei schnelle Reads hinweg gleich), und die Tick-Schwellen
    # verschoben sich mit poll_interval_ms/Zensus-Entkopplung. Ein festes
    # Zeitfenster ist unabhaengig davon.
    #
    # 2,0s (2026-09-10 von 1,5 erhoeht): der troops_total-Neu-Aufsummier-
    # Sweep (siehe _GUARD_ALL_DROPS_FIELDS) dauert bei langsamer
    # Spielgeschwindigkeit laenger - ein zu kurzes Fenster liesse dann kurz
    # vor Ablauf einen Zwischenwert durch, der sofort danach wieder hoch
    # springt (genau das Flackern, das wir killen wollen). Eine echte
    # Aenderung ist dafuer bis zu 2s zu spaet sichtbar; der Nutzer fand
    # 2026-09-10 fuer alle diese Felder ausdruecklich auch ~1s Verzoegerung
    # ok ("da reicht auch eine Sekunde Refresh in der Anzeige").
    SCALAR_HOLD_SECONDS = 2.0

    # Felder, bei denen JEDER Rueckgang (nicht nur ein scharfer) bis zu
    # SCALAR_HOLD_SECONDS gehalten wird. Grund (Nutzer-Beobachtung
    # 2026-09-10, bei sehr langsamer Spielgeschwindigkeit im CE-Adressfeld
    # sichtbar): troops_total wird spielintern NICHT atomar gesetzt,
    # sondern die Einheiten werden neu aufsummiert - der Wert laeuft dabei
    # sichtbar runter und wieder hoch und nimmt JEDEN Zwischenwert an, auch
    # flache. Ein Schwellen-Check auf die Einbruchstiefe wuerde die flachen
    # Zwischenwerte des Sweeps durchlassen. Da der Wert nur nach oben
    # springen kann, wenn er echt ist, kostet "jeden Rueckgang halten"
    # nichts ausser der ohnehin akzeptierten ~1,5s-Verzoegerung bei echten
    # Verlusten. Die Einheitentyp-Aufschluesselung wird genauso behandelt
    # (_debounce_unit_breakdown ruft mit guard_all_drops=True).
    _GUARD_ALL_DROPS_FIELDS = {"troops_total"}

    # Alle angezeigten Zahlenfelder, die vor kurzzeitigen Lesefehler-
    # Aussetzern geschuetzt werden - (Feldname in values, healthy_min,
    # glitch_max[, drop_fraction]), siehe _debounce_scalar_field().
    #
    # Seit 2026-09-10 deckt das ALLE Zahlenfelder ab, auch Gold/Holz/Stein/
    # Eisen/Popularity/Steuersatz/Nahrung/Waffenlager, die frueher bewusst
    # ausgespart waren ("koennen legitim auf 0 fallen"). Der Filter haelt
    # den alten Wert ja nur SCALAR_HOLD_SECONDS lang fest - ein echter
    # Absturz auf 0 kommt danach ganz normal durch, nur eben ~1,5s spaeter.
    # drop_fraction optional (Standard None).
    _DEBOUNCED_SCALAR_FIELDS = [
        # troops_total: steht in _GUARD_ALL_DROPS_FIELDS - jeder Rueckgang
        # wird gehalten (Neu-Aufsummier-Sweep, siehe dort). drop_fraction
        # hier daher irrelevant.
        ("troops_total", 3, 3),
        # lord_hp: hoehere Schwelle (0.6), weil der Lord unter schwerem
        # Beschuss legitim schneller HP verliert - >60% in einem Schritt
        # schafft praktisch nur ein Fehl-Read (falsche/keine Zeile in der
        # Objekttabelle). Ein echter Tod (HP -> 0) laeuft ueber den
        # glitch_max=0-Fall und kommt nach ~1,5s durch. War laut Nutzer
        # 2026-09-10 schon stabil - hier nichts verschaerft.
        ("lord_hp", 0, 0, 0.6),
        ("population", 3, 0),
        ("population_capacity", 5, 0),
        ("siege_movable_total", 0, 0),
        ("monks_trained", 0, 0),
        ("gold", 0, 0),
        ("wood", 0, 0),
        ("stone", 0, 0),
        ("iron", 0, 0),
        ("popularity_percent", 0, 0),
        ("tax_rate", 0, 0),
        ("bread", 0, 0), ("cheese", 0, 0), ("meat", 0, 0), ("apples", 0, 0),
        ("ale", 0, 0), ("pitch", 0, 0), ("hops", 0, 0), ("wheat", 0, 0), ("flour", 0, 0),
        ("bows", 0, 0), ("crossbows", 0, 0), ("spears", 0, 0), ("pikes", 0, 0),
        ("maces", 0, 0), ("swords", 0, 0), ("leather_armor", 0, 0), ("metal_armor", 0, 0),
    ]

    # Der Staerkeindex wirkte trotz der schon vorhandenen
    # sanften Balken-Animation noch "zu schnell"/unruhig, weil ein neues
    # Ziel bei jedem Poll (Standard alle 500ms) nachgeliefert wurde - die
    # Animation lief also quasi ununterbrochen (Nutzer-Feedback 2026-09-06).
    # Die Score-HISTORIE (strength_score.StrengthTracker) wird weiterhin
    # JEDEN Tick aktualisiert (billig, wichtig fuer einen akkuraten Trend),
    # aber apply_win_probabilities()/compute_side_summary() - und damit ein
    # neues sichtbares Ziel - laufen nur noch alle WIN_PROB_REFRESH_S
    # Sekunden, siehe _tick(). Erster Test-Wert, nicht weiter kalibriert.
    WIN_PROB_REFRESH_S = 2.0

    # Zusaetzliche Sprung-Sicherung (Nutzeridee 2026-09-06, oben drauf auf
    # das Refresh-Throttling): manchmal schwankt der frisch berechnete Wert
    # um 10 Prozentpunkte oder mehr und springt danach wieder zurueck -
    # eine einzelne so grosse Abweichung vom zuletzt ANGEZEIGTEN (nicht
    # dem zuletzt berechneten!) Wert wird deshalb noch nicht uebernommen,
    # erst wenn sie 2 Refresh-Zyklen HINTEREINANDER besteht. Siehe
    # _stabilize_percent().
    WIN_PROB_JUMP_THRESHOLD = 10  # Prozentpunkte
    WIN_PROB_JUMP_CONFIRM = 2  # aufeinanderfolgende Refresh-Zyklen

    # "GREIFT AN" bleibt nach der Angriffs-Erkennung (attackNumber steigt)
    # fuer diese feste Dauer sichtbar, dann aus. Zurueck zum einfachen
    # Zeitfenster (Nutzer 2026-09-10): der zwischenzeitliche Truppen-
    # Rueckgang-Check schaltete es oft schon nach ~1 Poll wieder ab (viel
    # zu kurz zum Ablesen), und die urspruengliche 10s-Version fiel damals
    # nur deshalb unangenehm auf, weil sie auf die inzwischen deaktivierte
    # "ANGRIFF BEVORSTEHEND"-Vorstufe zurueckfiel - jetzt faellt sie
    # einfach auf "kein Badge".
    GREIFT_AN_SECONDS = 10.0

    # So viele Ticks IN FOLGE muss "kein Spieler aktiv" angehalten haben,
    # bevor ein spaeteres Wieder-Aktivwerden als NEUES Match gilt (Tracker/
    # besiegte Karten leeren). ~3s bei Standard-poll_interval_ms=500 - ein
    # echter Kartenuebergang dauert deutlich laenger, ein Lesefehler-
    # Aussetzer nur 1-2 Ticks.
    NEW_MATCH_INACTIVE_TICKS = 6
    # Aendert sich bei keinem aktiven Spieler laenger als STALL_SECONDS
    # IRGENDein Zahlenwert aus _STALL_SIG_FIELDS, gilt das Spiel als vorbei
    # oder pausiert -> payload["stalled"]=True, das Frontend graut dann
    # alles aus (Karten bleiben sichtbar). Die Signatur wird bewusst aus
    # den ROHWERTEN gebildet (vor _hold_on_drop), sonst wuerde ein laufender
    # Halte-Zeitraum die eingefrorenen Truppen-/Ressourcenwerte
    # faelschlich als "nichts passiert" durchgehen lassen (Nutzer
    # 2026-09-10: Overlay grau trotz laufendem Spiel). 8s statt 5s und ein
    # breites Feld-Set, damit ein ruhiger Moment im Spiel (kein Kampf) das
    # nicht faelschlich ausloest. 6,0s (2026-09-10 von 8 gesenkt, im Zuge
    # der insgesamt kuerzeren Puffer) - das breite Roh-Feld-Set macht einen
    # Fehlalarm bei laufendem Spiel trotzdem unwahrscheinlich.
    STALL_SECONDS = 6.0
    _STALL_SIG_FIELDS = (
        "gold", "wood", "stone", "iron", "bread", "cheese", "meat",
        "troops_total", "population", "population_capacity",
        "popularity_percent", "lord_hp", "siege_movable_total",
        "unit_archer", "unit_spearman", "unit_pikeman", "unit_swordsman",
        "unit_knight", "unit_arab_archer",
    )

    # Halte-Fenster fuer die Truppentyp-Aufschluesselung
    # (_debounce_unit_breakdown). Etwas laenger als SCALAR_HOLD_SECONDS,
    # weil die Pro-Typ-Zaehler (Struct 0x1BE8..0x1C0C) beim Fehl-Read teils
    # laenger zu niedrig stehen als der reine troops_total-Wert. 3,0s
    # (2026-09-10 von 5,0 gesenkt - der Nutzer fand die 5s Nachlauf zu
    # traege; 3s fangen den beobachteten "kurz weg"-Fall noch ab). Ein
    # wirklich ausgestorbener Typ verschwindet erst nach dieser Zeit.
    UNIT_HOLD_SECONDS = 3.0

    def _stabilize_percent(self, key, raw, displayed_by_key, pending_by_key):
        """Gibt den Wert zurueck, der tatsaechlich angezeigt werden soll -
        entweder `raw` (direkt uebernommen) oder der weiterhin eingefrorene
        alte Anzeigewert, falls `raw` zu weit (siehe WIN_PROB_JUMP_
        THRESHOLD) vom bisherigen Anzeigewert abweicht und das noch nicht
        WIN_PROB_JUMP_CONFIRM Mal hintereinander der Fall war. `key` ist
        slot (Pro-Spieler) oder side["key"] (Pro-Seite/Team) - beide nutzen
        dieselbe Funktion mit getrennten State-Dicts."""
        if raw is None:
            return displayed_by_key.get(key)
        displayed = displayed_by_key.get(key)
        if displayed is None:
            # Erster Wert ueberhaupt fuer diesen Slot/diese Seite - sofort
            # uebernehmen, nichts zum Vergleichen da.
            displayed_by_key[key] = raw
            pending_by_key[key] = 0
            return raw
        if abs(raw - displayed) >= self.WIN_PROB_JUMP_THRESHOLD:
            count = pending_by_key.get(key, 0) + 1
            pending_by_key[key] = count
            if count >= self.WIN_PROB_JUMP_CONFIRM:
                displayed_by_key[key] = raw
                pending_by_key[key] = 0
            return displayed_by_key[key]
        pending_by_key[key] = 0
        displayed_by_key[key] = raw
        return raw

    def _debounce_active_slots(self, all_values):
        """Ein Spieler, der ploetzlich als 'inaktiv' erkannt wird
        (is_active_slot() prueft population_capacity>=10), obwohl er eben
        noch aktiv war, ist oft derselbe Lesefehler wie bei troops_total -
        population_capacity glitcht genauso kurz. Haelt 'inaktiv' ueber
        mehrere Polls IN FOLGE an, gilt der Spieler als wirklich besiegt.

        Seit 2026-09-06 (Nutzerwunsch): eine bestaetigt besiegte Karte
        VERSCHWINDET NICHT mehr, sondern bleibt eingefroren auf dem letzten
        bekannten Stand VOR der Niederlage bestehen (markiert mit
        is_defeated=True, siehe build_overlay_payload/overlay.html - dort
        wird sie ausgegraut statt entfernt). Vorher wurde nach Ablauf der
        Gnadenfrist der jetzt leere Rohwert durchgereicht, den
        is_active_slot()/build_overlay_payload dann herausfilterten - die
        Karte verschwand komplett. Gibt eine NEUE all_values-Liste zurueck
        (Tupel sind unveraenderlich), jeder values-Eintrag hat jetzt IMMER
        einen expliziten "is_defeated"-Schluessel (False bei echt aktiven
        Slots, fehlt nur bei einem Slot, der in diesem Match noch nie aktiv
        war)."""
        result = []
        for i, values in all_values:
            if res.is_active_slot(values):
                self._pending_inactive_counts[i] = 0
                self._last_confirmed_active_values[i] = values
                self._confirmed_defeated.discard(i)
                v = dict(values)
                v["is_defeated"] = False
                result.append((i, v))
                continue
            last_active = self._last_confirmed_active_values.get(i)
            if last_active is None:
                # Slot war in diesem Match noch nie aktiv - echt leer,
                # unveraendert durchreichen (wird von is_active_slot()
                # weiterhin korrekt herausgefiltert).
                result.append((i, values))
                continue
            count = self._pending_inactive_counts.get(i, 0) + 1
            self._pending_inactive_counts[i] = count
            if count < self.ACTIVE_CONFIRM_TICKS:
                v = dict(last_active)
                v["is_defeated"] = False
                result.append((i, v))
                continue
            # Bestaetigt besiegt - Cache bewusst NICHT mehr loeschen (frueher
            # hier: pop()), sonst wuerde is_active_slot() beim naechsten Tick
            # wieder False liefern und die Karte doch verschwinden. Bleibt
            # bis zum naechsten echten Match-Start eingefroren (siehe
            # _tick()'s Match-Neustart-Heuristik fuer das Aufraeumen).
            self._confirmed_defeated.add(i)
            v = dict(last_active)
            v["is_defeated"] = True
            result.append((i, v))
        return result

    @staticmethod
    def _hold_on_drop(new, st, healthy_min, glitch_max, drop_fraction, hold_seconds,
                      guard_all_drops=False):
        """Kern des Glitch-Filters fuer EINEN Wert. `st` ist ein
        veraenderliches dict pro (Slot, Feld):
          "disp"  - zuletzt als gut angezeigter Wert
          "since" - monotonic-Zeitpunkt, seit dem der aktuelle Einbruch
                    ununterbrochen anhaelt (None = kein Einbruch)
        Rueckgabe: der anzuzeigende Wert.

        Ein Wert, der steigt oder gleich bleibt, wird IMMER sofort
        uebernommen (ein echter Wert kann nur nach oben springen; nach
        oben "glitcht" hier nichts).

        Ein Rueckgang wird bis zu hold_seconds lang unterdrueckt, "disp"
        bleibt sichtbar. Erholt sich der Wert (>= disp) innerhalb des
        Fensters, war es ein Aussetzer/Zwischenwert und wurde nie gezeigt.
        Haelt der Rueckgang laenger an, gilt er als echt und "disp" zieht
        nach.

        Welche Rueckgaenge ueberhaupt gehalten werden:
          guard_all_drops=True  -> jeder (troops_total & Einheitentypen -
              der spielinterne Neu-Aufsummier-Sweep nimmt auch flache
              Zwischenwerte an, siehe _GUARD_ALL_DROPS_FIELDS).
          guard_all_drops=False -> nur "scharfe": auf <= glitch_max ODER,
              falls drop_fraction gesetzt, um >= diesen Anteil von disp."""
        disp = st.get("disp")
        if new is None:
            return disp  # kein frischer Messwert -> letzten guten Stand halten
        if disp is None:
            st["disp"] = new
            st["since"] = None
            return new
        if new >= disp:
            # Anstieg/Erholung -> immer sofort uebernehmen, ein evtl.
            # laufender Sweep ist damit vorbei.
            st["disp"] = new
            st["since"] = None
            return new
        if guard_all_drops:
            suspicious = disp > healthy_min
        else:
            suspicious = disp > healthy_min and (
                new <= glitch_max
                or (drop_fraction is not None and new <= disp * (1.0 - drop_fraction))
            )
        if not suspicious:
            st["since"] = None
            st["disp"] = new
            return new
        now = time.monotonic()
        if st.get("since") is None:
            st["since"] = now
        if now - st["since"] >= hold_seconds:
            st["disp"] = new  # Rueckgang haelt lange genug an -> als echt uebernehmen
            st["since"] = None
            return new
        return disp

    def _debounce_scalar_field(self, all_values, field, hold_state_by_slot,
                               healthy_min, glitch_max, drop_fraction=None):
        """Generischer Glitch-Filter fuer EIN values-Feld ueber alle aktiven
        Slots - haelt einen scharfen Einbruch bis zu SCALAR_HOLD_SECONDS
        lang auf dem letzten guten Wert (Details siehe _hold_on_drop und
        den Kommentar bei SCALAR_HOLD_SECONDS).

        Muss VOR der Angriffsstatus-Berechnung und build_overlay_payload()
        laufen (mutiert die values-dicts in all_values in place, dieselben
        Objekte werden dort weiterverwendet) - sonst wuerde z.B.
        attack_status waehrend eines noch unbestaetigten troops_total-
        Einbruchs kurz faelschlich auf "ruhig" fallen."""
        guard_all_drops = field in self._GUARD_ALL_DROPS_FIELDS
        for i, values in all_values:
            if not res.is_active_slot(values):
                continue
            st = hold_state_by_slot.setdefault(i, {})
            values[field] = self._hold_on_drop(
                values.get(field), st, healthy_min, glitch_max,
                drop_fraction, self.SCALAR_HOLD_SECONDS, guard_all_drops,
            )

    def _debounce_unit_breakdown(self, all_values):
        """Wie _debounce_scalar_field, aber pro Truppentyp-Wert
        (UNIT_TYPE_KEYS in reader.py, z.B. unit_archer/unit_knight/...).

        Pro EINZELNEM Typ geprueft: der Lesefehler kann alle Typen
        gleichzeitig einbrechen lassen, aber auch nur einen - das faellt
        bei einer reinen Summen-Pruefung durchs Raster. guard_all_drops=True
        wie bei troops_total (derselbe Neu-Aufsummier-Sweep, siehe
        _GUARD_ALL_DROPS_FIELDS): jeder Rueckgang eines Typs wird bis zu
        UNIT_HOLD_SECONDS gehalten, nur ein Anstieg kommt sofort durch."""
        for i, values in all_values:
            if not res.is_active_slot(values):
                continue
            slot_state = self._unit_hold_state.setdefault(i, {})
            for k in res.UNIT_TYPE_KEYS:
                st = slot_state.setdefault(k, {})
                values[k] = self._hold_on_drop(
                    values.get(k), st, 0, 0, None,
                    self.UNIT_HOLD_SECONDS, True,
                )

    def _tick(self):
        if not self._ensure_connected():
            return

        try:
            # Muss VOR read_all_players() laufen - read_player() liest die
            # hier befüllten Caches nur noch aus (siehe reader.py::get_lord_hp,
            # ::get_arab_unit_counts und ::get_monks_trained).
            res.poll_lord_hp(self._pm)
            res.poll_arab_units(self._pm)
            res.poll_monk_units(self._pm)
            all_values = res.read_all_players(self._pm)
            lord_labels = res.read_roster_names(self._pm)
        except Exception as e:
            # Spiel vermutlich geschlossen - Verbindung verwerfen, nächster
            # Tick versucht einen frischen connect().
            self._pm = None
            self.state.set_waiting(f"Verbindung verloren: {e}")
            return

        if sum(1 for _, v in all_values if res.is_active_slot(v) and not v.get("session_ok")) > 1:
            self.state.set_blocked()
            return

        # Stall-Signatur JETZT festhalten - aus den Rohwerten, bevor
        # _debounce_active_slots/_hold_on_drop irgendetwas einfrieren.
        self._raw_activity_sig = tuple(
            (i,) + tuple(v.get(k) for k in self._STALL_SIG_FIELDS)
            for i, v in all_values
            if res.is_active_slot(v)
        )

        all_values = self._debounce_active_slots(all_values)
        # Alle angezeigten Zahlenfelder gegen kurzzeitige Lesefehler-
        # Aussetzer (Sprung auf ~0, im naechsten Poll wieder da) abfedern -
        # siehe _DEBOUNCED_SCALAR_FIELDS fuer die Liste und die Begruendung,
        # warum inzwischen auch Gold/Ressourcen/Popularity/Waffen dabei sind.
        for entry in self._DEBOUNCED_SCALAR_FIELDS:
            field, healthy_min, glitch_max = entry[0], entry[1], entry[2]
            drop_fraction = entry[3] if len(entry) > 3 else None
            self._debounce_scalar_field(
                all_values, field,
                self._scalar_hold_state.setdefault(field, {}),
                healthy_min, glitch_max, drop_fraction,
            )
        self._debounce_unit_breakdown(all_values)

        # Seit 2026-09-06 live-first (siehe attack_monitor.py-Moduldoku) -
        # braucht KEINE geladene .aic-Datei mehr (self._aic_data ist nur
        # noch optionaler Fallback), deshalb hier nicht mehr auf sie
        # gated.
        #
        # Slot 0 NICHT mehr pauschal uebersprungen (frueher: "i == 0 or
        # ..." - Annahme "Slot 0 ist immer der Mensch"). In reinen KI-vs-KI-
        # Zuschauer-Matches (UCP3 erlaubt das) kann Slot 0 genauso gut eine
        # KI sein - der Nutzer meldete 2026-09-06 fehlende Angriffs-
        # meldungen fuer Spieler 1 in genau so einem Match. Fuer einen
        # ECHTEN menschlichen Slot 0 aendert sich dadurch nichts: der Name
        # matcht ohnehin keine bekannte Persona, compute_player_attack_
        # status() liefert dann ganz normal status="?" zurueck, was das
        # Frontend schon immer als "kein Badge" behandelt (siehe
        # attackBadgeHtml/statusHtml) - kein Sonderfall noetig.
        attack_status = {}
        for i, values in all_values:
            if not res.is_active_slot(values):
                continue
            name = lord_labels.get(i)
            troops = values.get("troops_total")
            gold = values.get("gold")
            result = attack_monitor.compute_player_attack_status(
                self._pm, i, name, troops, gold, self._aic_data
            )
            # Reaktive "GREIFT AN"-Anzeige (Nutzerwunsch 2026-09-06, als
            # Alternative zur vorhersagebasierten Schwelle, die laut Nutzer
            # zu früh ansprang): attackNumber (Lead 2, live bestätigt)
            # steigt NUR genau dann, wenn die KI tatsächlich einen Angriff
            # losschickt. Bei so einem Anstieg wird "GREIFT AN" fuer
            # GREIFT_AN_SECONDS eingeblendet und danach wieder aus.
            #
            # Bewusst ein einfaches festes Zeitfenster (mehrere andere
            # Ausstiegs-Bedingungen wurden 2026-09-06/-10 durchprobiert und
            # wieder verworfen: eine Schwelle relativ zu einer berechneten
            # Angriffsgroesse braucht eine aufgeloeste Personality und blieb
            # bei Custom-KIs fuer immer haengen; ein troops_total-Rueckgang-
            # Check schaltete oft schon nach ~1 Poll wieder ab). Die
            # urspruengliche 10s-Version fiel damals nur deshalb negativ
            # auf, weil sie auf die inzwischen deaktivierte "ANGRIFF
            # BEVORSTEHEND"-Vorstufe zurueckfiel - jetzt faellt sie auf
            # "kein Badge". `just_triggered` haelt "GREIFT AN" fuer den
            # Erkennungs-Tick selbst sicher an (der elapsed-Vergleich koennte
            # sonst bei einem langsamen Tick theoretisch sofort True sein).
            attack_number = result.get("attack_number")
            just_triggered = False
            if attack_number is not None:
                last = self._last_attack_number.get(i)
                if last is not None and attack_number > last:
                    self._attacking_active[i] = True
                    self._attack_trigger_ts[i] = time.monotonic()
                    just_triggered = True
                self._last_attack_number[i] = attack_number

            if self._attacking_active.get(i):
                trigger_ts = self._attack_trigger_ts.get(i)
                if trigger_ts is None:
                    # Sollte praktisch nie vorkommen (wird im selben Tick
                    # gesetzt, in dem "aktiv" auf True geht) - nur zur
                    # Absicherung, damit es im Zweifel sauber ausschaltet
                    # statt haengen zu bleiben.
                    self._attacking_active[i] = False
                elif not just_triggered and (time.monotonic() - trigger_ts) >= self.GREIFT_AN_SECONDS:
                    self._attacking_active[i] = False
                else:
                    result["status"] = "GREIFT AN"
            attack_status[i] = result

        payload = res.build_overlay_payload(all_values, lord_labels, attack_status)

        # Heuristik für "neues Match im selben, weiterlaufenden Spielprozess
        # gestartet": alle Slots waren zuletzt inaktiv (Kartenübergang/
        # Hauptmenü) und jetzt ist mindestens einer wieder aktiv. Anders als
        # der Reconnect-Reset oben (sicher, da an einen echten Verbindungs-
        # verlust gekoppelt) ist das nur eine Annahme - siehe
        # research/shc_overlay_status.md für die offene Verifikationsfrage,
        # ob population_capacity zwischen Matches tatsächlich für alle
        # Slots kurz auf 0 fällt.
        # NICHT mehr ueber payload["players"] pruefen (seit der "besiegte
        # Karte bleibt sichtbar"-Aenderung, siehe _debounce_active_slots,
        # enthaelt das auch eingefrorene besiegte Spieler, die is_active_slot
        # weiterhin bestehen - has_active wuerde sonst NIE wieder False,
        # sobald einmal jemand besiegt wurde, und dieser Reset faende nie
        # wieder statt) - stattdessen direkt aus all_values, nur echt
        # GERADE aktive (nicht eingefrorene) Slots zaehlen.
        has_active = any(
            res.is_active_slot(v) and not v.get("is_defeated") for _, v in all_values
        )
        # Nur ein Wieder-Aktivwerden NACH einer ausreichend langen Pause
        # gilt als neues Match - ein kurzer gemeinsamer Aussetzer aller
        # Slots (1-2 Ticks) darf die besiegten Karten NICHT wegraeumen.
        is_new_match = has_active and self._inactive_streak >= self.NEW_MATCH_INACTIVE_TICKS
        self._inactive_streak = 0 if has_active else self._inactive_streak + 1
        if is_new_match:
            self._strength_tracker.reset()
            res.reset_team_detection()
            # sonst wuerden die niedrigen Start-Werte des neuen Matches
            # faelschlich gegen die hohen Endstand-Werte des vorherigen als
            # "Einbruch" gewertet und fuer SCALAR_HOLD_SECONDS unterdrueckt
            # (siehe _debounce_scalar_field/_debounce_unit_breakdown).
            self._scalar_hold_state = {}
            self._unit_hold_state = {}
            # sonst koennte der hohe attackNumber-Endstand des alten
            # Matches faelschlich als "gerade gesunken" gegen den neuen
            # Match-Startwert (1) gewertet werden - kein echter Fehler
            # (ein SINKEN loest ohnehin nie "GREIFT AN" aus), aber sauberer
            # als auf den impliziten Selbstkorrektur-Effekt zu vertrauen.
            self._last_attack_number = {}
            self._attacking_active = {}
            self._attack_trigger_ts = {}
            # Eingefrorene "besiegt"-Karten des VORHERIGEN Matches muessen
            # jetzt weg, sonst wuerden sie als Geister-Spieler im neuen
            # Match weiter mitgeschleppt (deren echter Slot koennte im
            # neuen Match sogar mit einem anderen Spieler belegt sein).
            self._last_confirmed_active_values = {}
            self._pending_inactive_counts = {}
            self._confirmed_defeated = set()
            # sonst wuerden fuer bis zu WIN_PROB_REFRESH_S Sekunden die
            # gecachten Staerkeindex-Werte des VORHERIGEN Matches
            # weiterangezeigt.
            self._last_win_prob_update_ts = 0.0
            self._cached_win_probs = {}
            self._cached_sides = []
            self._win_prob_displayed = {}
            self._win_prob_jump_pending = {}
            self._side_win_prob_displayed = {}
            self._side_win_prob_jump_pending = {}
            self._last_activity_ts = 0.0
            self._last_activity_sig = None
            self._raw_activity_sig = None

        # Stall-Erkennung (siehe STALL_SECONDS/_STALL_SIG_FIELDS): die
        # Signatur wurde oben AUS DEN ROHWERTEN gebildet (vor _hold_on_drop),
        # damit ein laufender Halte-Zeitraum die eingefrorenen Werte nicht
        # faelschlich als "nichts aendert sich" durchgehen laesst.
        now = time.monotonic()
        sig = self._raw_activity_sig
        if sig != self._last_activity_sig or not sig:
            self._last_activity_sig = sig
            self._last_activity_ts = now
        stalled = bool(sig) and (now - self._last_activity_ts) > self.STALL_SECONDS
        payload["stalled"] = stalled

        res.poll_team_detection(all_values)

        # Effektive Team-Zuordnung: ENTWEDER/ODER je nach
        # config["team_assignment_mode"], keine Vermischung pro Slot (siehe
        # config.py-Kommentar - ein Agenten-Review deckte auf, dass die
        # frühere "manuell gewinnt, sobald irgendein Slot gesetzt ist"-Logik
        # bei nur teilweise ausgefüllter manueller Liste ALLE anderen Slots
        # fälschlich aus der automatischen Erkennung rausfallen ließ). Wird
        # sowohl fuer den Staerkeindex unten als auch (ueber
        # state.set_effective_team_assignment) fuer Overlay/Uebersicht per
        # server.py verwendet.
        if self._config.get("team_assignment_mode", "auto") == "manual":
            team_assignment = self._config.get("team_assignment", {})
        else:
            # get_diplomatic_teams() (literales Gruppen-ID-Array,
            # 0x0117D54C) statt der alten Ko-Gleichheits-Heuristik
            # (get_detected_teams()) - siehe reader.py-Kommentar. Die
            # alte Erkennung läuft über poll_team_detection() weiter
            # im Hintergrund mit, nur als Sicherheitsnetz, aktuell ohne
            # Auswirkung auf die Anzeige.
            detected = res.get_diplomatic_teams(self._pm, all_values)
            team_assignment = (
                {str(slot): team for slot, team in detected.items()}
                if detected
                else {}
            )
        self.state.set_effective_team_assignment(team_assignment)

        # Score-Historie IMMER aktualisieren (billig, wichtig fuer einen
        # akkuraten Trend) - aber apply_win_probabilities()/
        # compute_side_summary() (und damit ein neues sichtbares Ziel fuer
        # die Balken-Animation) nur alle WIN_PROB_REFRESH_S neu berechnen,
        # dazwischen die zuletzt berechneten Werte weiterverwenden (siehe
        # Konstanten-Kommentar oben) - reduziert die gefuehlte Update-
        # Frequenz, ohne den Trend selbst zu verzoegern.
        now = time.time()
        strengths, _ = strength_score.update_and_get_strengths(payload["players"], self._strength_tracker)
        due_for_refresh = (now - self._last_win_prob_update_ts) >= self.WIN_PROB_REFRESH_S
        if due_for_refresh or not self._cached_win_probs:
            strength_score.apply_win_probabilities(payload["players"], team_assignment, strengths)
            sides = strength_score.compute_side_summary(
                payload["players"], team_assignment, strengths,
                team_names=self._config.get("team_names", {}),
            )
            # 10%-Sprung-Sicherung (siehe _stabilize_percent/Konstanten
            # oben) - ersetzt den frisch berechneten Wert durch den
            # stabilisierten, BEVOR er gecacht/angezeigt wird.
            for p in payload["players"]:
                p["win_probability_percent"] = self._stabilize_percent(
                    p["slot"], p.get("win_probability_percent"),
                    self._win_prob_displayed, self._win_prob_jump_pending,
                )
            for s in sides:
                s["win_probability_percent"] = self._stabilize_percent(
                    s["key"], s.get("win_probability_percent"),
                    self._side_win_prob_displayed, self._side_win_prob_jump_pending,
                )
            self._cached_sides = sides
            self._cached_win_probs = {
                p["slot"]: (p.get("strength_score"), p.get("win_probability_percent"))
                for p in payload["players"]
            }
            self._last_win_prob_update_ts = now
        else:
            for p in payload["players"]:
                cached = self._cached_win_probs.get(p["slot"])
                if cached is not None:
                    p["strength_score"], p["win_probability_percent"] = cached
        payload["sides"] = self._cached_sides
        self.state.set_payload(payload)

    def _run(self):
        # Früher gab es hier einen Zwei-Takt-Poll (schnelles Zwischen-Poll
        # nur fürs Mönche-Ereignis-Log, um dessen Ringpuffer nicht zu
        # verpassen) - mit dem Umbau auf den Objekttabellen-Zensus
        # (2026-09-04) liefert jeder Tick einen vollständigen aktuellen
        # Bestand, ein Zwischen-Poll ist nicht mehr nötig.
        while not self._stop_event.is_set():
            interval = self._config.get("poll_interval_ms", 500) / 1000.0
            try:
                self._tick()
            except Exception as e:
                # Absicherung: der Thread darf unter keinen Umständen
                # sterben, auch nicht bei einem unerwarteten Fehler.
                self.state.set_error(str(e))
                self._pm = None
            self._stop_event.wait(interval)
