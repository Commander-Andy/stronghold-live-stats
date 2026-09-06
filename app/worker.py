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
        # Für die (unvollständige, siehe _tick) Match-Neustart-Erkennung:
        # ob im letzten Tick mindestens ein aktiver Spieler-Slot da war.
        self._had_active_players = False
        # Fuer den troops_total-Einbruch-Filter (siehe _debounce_troop_drops):
        # slot -> zuletzt akzeptierter Wert bzw. Anzahl aufeinanderfolgender
        # verdaechtiger Polls.
        self._last_confirmed_troops = {}
        self._pending_drop_counts = {}
        # Fuer den Verschwinden-Filter (siehe _debounce_active_slots): slot
        # -> zuletzt bestaetigte values-dict bzw. Anzahl aufeinanderfolgender
        # Polls, in denen der Slot faelschlich als inaktiv erkannt wurde.
        self._last_confirmed_active_values = {}
        self._pending_inactive_counts = {}
        # Fuer den Truppentyp-Aufschluesselung-Filter (siehe
        # _debounce_unit_breakdown): slot -> zuletzt bestaetigtes
        # units-dict bzw. Anzahl aufeinanderfolgender verdaechtiger Polls.
        self._last_confirmed_units = {}
        self._pending_unit_drop_counts = {}
        # Fuer die reaktive "Greift an"-Anzeige (siehe _tick): slot ->
        # zuletzt gesehener attackNumber-Wert (Lead 2, siehe
        # [[project_aic_memory_field_investigation]]) bzw. ob "GREIFT AN"
        # gerade aktiv ist. Kein fester Timer mehr (siehe _tick-Kommentar) -
        # bleibt aktiv, bis troops_total spuerbar unter die Truppenzahl beim
        # Angriff selbst faellt (Nutzer-Idee 2026-09-06, loest das Problem
        # eines zu kurzen/zu langen festen Zeitfensters).
        self._last_attack_number = {}
        self._attacking_active = {}
        # slot -> troops_total-Wert im Moment des zuletzt erkannten
        # Angriffs (siehe _tick) - Ausstiegs-Referenz fuer "GREIFT AN",
        # bewusst UNABHAENGIG von einer aufgeloesten Personality.
        self._attack_dispatch_troops = {}
        # Fuer _debounce_scalar_field() - je Feld ein eigenes Paar
        # "letzter guter Wert"/"Verdachtszaehler" pro Slot. lord_hp war das
        # erste (2026-09-06), population/population_capacity/
        # siege_movable_total/monks_trained kamen am selben Tag dazu,
        # nachdem der Nutzer meldete, dass auch diese Felder kurzzeitig
        # verschwinden konnten.
        self._last_confirmed_lord_hp = {}
        self._pending_lord_hp_drop_counts = {}
        self._last_confirmed_population = {}
        self._pending_population_drop_counts = {}
        self._last_confirmed_population_capacity = {}
        self._pending_population_capacity_drop_counts = {}
        self._last_confirmed_siege = {}
        self._pending_siege_drop_counts = {}
        self._last_confirmed_monks = {}
        self._pending_monks_drop_counts = {}
        # Slots, deren Burgherr als besiegt bestaetigt gilt (siehe
        # _debounce_active_slots) - Karte bleibt dafuer bestehen (Nutzer-
        # wunsch 2026-09-06) statt zu verschwinden, wird im Frontend
        # stattdessen ausgegraut (payload-Feld "is_defeated").
        self._confirmed_defeated = set()
        # Fuer das Gewinnwahrscheinlichkeits-Throttling (siehe
        # WIN_PROB_REFRESH_S/_tick): Zeitpunkt der letzten echten Neu-
        # berechnung, plus die dabei zuletzt berechneten Werte zum
        # Zwischen-Ticks-Wiederverwenden.
        self._last_win_prob_update_ts = 0.0
        self._cached_win_probs = {}  # slot -> (strength_score, win_probability_percent)
        self._cached_sides = []
        # Sprung-Sicherung fuer die Gewinnwahrscheinlichkeit (Nutzeridee
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
        self._had_active_players = False
        self._last_confirmed_troops = {}
        self._pending_drop_counts = {}
        self._last_confirmed_active_values = {}
        self._pending_inactive_counts = {}
        self._last_confirmed_units = {}
        self._pending_unit_drop_counts = {}
        self._last_attack_number = {}
        self._attacking_active = {}
        self._attack_dispatch_troops = {}
        self._last_confirmed_lord_hp = {}
        self._pending_lord_hp_drop_counts = {}
        self._last_confirmed_population = {}
        self._pending_population_drop_counts = {}
        self._last_confirmed_population_capacity = {}
        self._pending_population_capacity_drop_counts = {}
        self._last_confirmed_siege = {}
        self._pending_siege_drop_counts = {}
        self._last_confirmed_monks = {}
        self._pending_monks_drop_counts = {}
        self._confirmed_defeated = set()
        self._last_win_prob_update_ts = 0.0
        self._cached_win_probs = {}
        self._cached_sides = []
        self._win_prob_displayed = {}
        self._win_prob_jump_pending = {}
        self._side_win_prob_displayed = {}
        self._side_win_prob_jump_pending = {}
        return True

    # So viele aufeinanderfolgende Polls muss ein ploetzlicher
    # troops_total-Einbruch bzw. ein Verschwinden anhalten, bevor es als
    # echt akzeptiert wird.
    GLITCH_CONFIRM_TICKS = 3
    ACTIVE_CONFIRM_TICKS = 3
    # Die Truppentyp-Aufschluesselung (_debounce_unit_breakdown) glitcht
    # etwas hartnaeckiger als der reine troops_total-Wert - der Nutzer
    # meldete 2026-09-06 noch gelegentlich verschwindende Einheiten bei den
    # 3 Ticks/1,5s von GLITCH_CONFIRM_TICKS. Eigene, laengere Schwelle statt
    # den gemeinsamen Wert fuer alle Debounces hochzusetzen - 5 Ticks bei
    # Standard-poll_interval_ms=500 sind 2,5s, im vom Nutzer gewuenschten
    # 2-2,5s-Fenster.
    UNIT_GLITCH_CONFIRM_TICKS = 5

    # Die Gewinnwahrscheinlichkeit wirkte trotz der schon vorhandenen
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

    def _debounce_scalar_field(self, all_values, field, last_good_by_slot, pending_by_slot, healthy_min, glitch_max):
        """Generischer Glitch-Filter fuer EIN values-Feld: ein Sprung von
        einem "gesunden" Wert (> healthy_min) auf einen verdaechtig
        niedrigen (<= glitch_max) wird erst nach GLITCH_CONFIRM_TICKS
        aufeinanderfolgenden Polls akzeptiert, davor bleibt der letzte gute
        Wert eingefroren. Ersetzt seit 2026-09-06 die vorher fast
        identischen, separat kopierten _debounce_troop_drops/_debounce_
        lord_hp-Methoden - der Nutzer meldete, dass auch andere, spaeter
        hinzugefuegte Felder (Bevoelkerung, Belagerung) denselben
        kurzzeitigen Aussetzer zeigten, den troops_total/lord_hp schon
        hatten - alle Betroffenen teilen sich jetzt diese eine Funktion
        statt dass jedes Feld seine eigene Kopie braucht.

        Muss VOR der Angriffsstatus-Berechnung und build_overlay_payload()
        laufen (mutiert die values-dicts in all_values direkt in place,
        dieselben Objekte werden dort weiterverwendet) - sonst wuerde z.B.
        attack_status waehrend eines noch unbestaetigten troops_total-
        Einbruchs kurz faelschlich auf "ruhig" fallen, obwohl der
        angezeigte Truppenwert selbst schon korrigiert waere."""
        for i, values in all_values:
            if not res.is_active_slot(values):
                continue
            new = values.get(field)
            last_good = last_good_by_slot.get(i)
            looks_like_glitch = (
                last_good is not None and last_good > healthy_min
                and new is not None and new <= glitch_max
            )
            if looks_like_glitch:
                count = pending_by_slot.get(i, 0) + 1
                pending_by_slot[i] = count
                if count < self.GLITCH_CONFIRM_TICKS:
                    values[field] = last_good
                    continue
            else:
                pending_by_slot[i] = 0
            last_good_by_slot[i] = new

    def _debounce_unit_breakdown(self, all_values):
        """Wie _debounce_troop_drops, aber fuer die Truppentyp-Werte
        (UNIT_TYPE_KEYS in reader.py, z.B. unit_archer/unit_knight/...).

        PRO EINZELNEM TYP geprueft (nicht nur an der Summe aller Typen, wie
        bis 2026-09-06) - der urspruengliche Lesefehler kann ALLE Typen
        gleichzeitig auf 0 werfen (dafuer war die Summen-Pruefung gedacht),
        aber der Nutzer meldete auch danach noch gelegentlich einzelne
        verschwindende Einheiten - ein Glitch, der nur EINEN Typ betrifft
        waehrend der Rest der Aufschluesselung stabil bleibt, faellt bei
        einer reinen Summen-Pruefung durchs Raster (die Summe aendert sich
        ja kaum, wenn nur ein kleiner Posten kurz auf 0 faellt). Haelt pro
        Typ eine eigene "letzter guter Wert"/Verdachtszaehler-Historie."""
        for i, values in all_values:
            if not res.is_active_slot(values):
                continue
            last_good = self._last_confirmed_units.setdefault(i, {})
            pending = self._pending_unit_drop_counts.setdefault(i, {})
            for k in res.UNIT_TYPE_KEYS:
                new = values.get(k)
                prev_good = last_good.get(k)
                looks_like_glitch = (
                    prev_good is not None and prev_good > 2
                    and (new is None or new <= 0)
                )
                if looks_like_glitch:
                    count = pending.get(k, 0) + 1
                    pending[k] = count
                    if count < self.UNIT_GLITCH_CONFIRM_TICKS:
                        values[k] = prev_good
                        continue
                else:
                    pending[k] = 0
                last_good[k] = new

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

        all_values = self._debounce_active_slots(all_values)
        # Reine Struktur-Felder, die praktisch nie spontan auf (nahe) 0
        # fallen koennen, solange der Slot noch echt aktiv ist - ein
        # kurzzeitiger Sprung dorthin ist so gut wie immer derselbe
        # Objekttabellen-/Struct-Lesefehler wie beim urspruenglich
        # gefundenen troops_total-Glitch, nicht der echte Wert. Bewusst
        # NICHT auf Gold/Holz/Ressourcen/Popularity ausgeweitet - die
        # koennen ganz legitim auf 0 fallen (z.B. Gold komplett in Truppen
        # investiert), ein Freeze dort wuerde echte Aenderungen verschlucken.
        self._debounce_scalar_field(
            all_values, "troops_total", self._last_confirmed_troops, self._pending_drop_counts, 5, 1
        )
        self._debounce_scalar_field(
            all_values, "lord_hp", self._last_confirmed_lord_hp, self._pending_lord_hp_drop_counts, 0, 0
        )
        self._debounce_scalar_field(
            all_values, "population", self._last_confirmed_population, self._pending_population_drop_counts, 3, 0
        )
        self._debounce_scalar_field(
            all_values, "population_capacity", self._last_confirmed_population_capacity,
            self._pending_population_capacity_drop_counts, 5, 0
        )
        self._debounce_scalar_field(
            all_values, "siege_movable_total", self._last_confirmed_siege, self._pending_siege_drop_counts, 0, 0
        )
        self._debounce_scalar_field(
            all_values, "monks_trained", self._last_confirmed_monks, self._pending_monks_drop_counts, 0, 0
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
            # Alternative zur vorhersagebasierten "ANGRIFF BEVORSTEHEND"-
            # Schwelle, die laut Nutzer trotz korrekter Berechnung zu früh
            # anspringt): attackNumber (Lead 2, live bestätigt) steigt NUR
            # genau dann, wenn die KI tatsächlich einen Angriff losschickt.
            #
            # KEIN festes Zeitfenster mehr (erste Version nutzte 10s hart -
            # Nutzer meldete noch am selben Tag, dass ein laenger laufender
            # Angriff/Belagerung dann faelschlich zurueck auf "ANGRIFF
            # BEVORSTEHEND" faellt, waehrend der Kampf noch laeuft). Bleibt
            # jetzt stattdessen aktiv, bis troops_total spuerbar (10%) UNTER
            # die Schwelle faellt.
            #
            # Diese Schwelle ist die fuer den NAECHSTEN Angriff (result[
            # "predicted_min"]) - NICHT die des letzten (zweite Version,
            # selber Tag, kurz ausprobiert): die Schwelle des letzten
            # Angriffs waechst nur um den festen Eskalationsschritt (+5/+7)
            # pro Angriff, waehrend troops_total durch die normale
            # Wirtschaft im Spielverlauf viel schneller waechst - irgendwann
            # liegt troops_total dauerhaft ueber dieser laengst ueberholten
            # alten Schwelle und "GREIFT AN" haengt permanent fest (vom
            # Nutzer selbst noch am selben Tag entdeckt, zuerst bei Slot 0
            # gesehen). Die naechste Schwelle waechst zusammen mit dem
            # Eskalationszaehler UND wird bei jedem Angriff neu ausgerechnet,
            # bleibt also relativ zur aktuellen Truppenzahl sinnvoll.
            # `just_triggered` verhindert ein sofortiges Wieder-Ausschalten
            # im selben Tick, in dem der Anstieg erkannt wird, falls
            # troops_total zu diesem Zeitpunkt schon unter der Schwelle
            # liegt (Truppen koennen schneller abgezogen sein, als wir
            # pollen) - "GREIFT AN" soll dann trotzdem mindestens einmal
            # sichtbar werden.
            # Ausstiegs-Referenz ist NICHT mehr die berechnete Schwelle
            # (base/predicted_min - braucht eine aufgeloeste Personality),
            # sondern schlicht troops_total im Moment DIESES Angriffs
            # selbst - loest gleich zwei Probleme, beide am selben Tag live
            # gefunden: (1) fuer Spieler mit unaufloesbarem Namen (weder
            # Namens-Match noch Live-Feld-Fallback, z.B. "Templer, Der
            # Fromme") waren base/predicted_min IMMER None, wodurch die
            # Ausstiegs-Pruefung nie True werden konnte - "GREIFT AN" blieb
            # fuer diesen Spieler fuer immer an ("permanentes GREIFT AN nur
            # bei Spieler 1/ID0" - kein Adressierungsproblem, sondern genau
            # das). (2) selbst MIT aufgeloester Personality wurde die
            # Schwelle aus einem frueheren Versuch schnell von der normal
            # wachsenden Wirtschaft ueberholt und blieb dann dauerhaft
            # unterschritten. Die hier verwendete Referenz (Truppenzahl bei
            # Angriffs-Erkennung) ist dagegen IMMER aktuell und braucht gar
            # keine Personality-Aufloesung - funktioniert dadurch jetzt auch
            # bei Custom-KIs mit unbekanntem Namen.
            attack_number = result.get("attack_number")
            just_triggered = False
            if attack_number is not None:
                last = self._last_attack_number.get(i)
                if last is not None and attack_number > last:
                    self._attacking_active[i] = True
                    self._attack_dispatch_troops[i] = troops
                    just_triggered = True
                self._last_attack_number[i] = attack_number

            if self._attacking_active.get(i):
                dispatch_troops = self._attack_dispatch_troops.get(i)
                if dispatch_troops is None:
                    # Sollte praktisch nie vorkommen (wird ja im selben
                    # Tick gesetzt, in dem "aktiv" auf True geht) - nur zur
                    # Absicherung, damit es im Zweifel sauber ausschaltet
                    # statt haengen zu bleiben.
                    self._attacking_active[i] = False
                else:
                    should_exit = (
                        not just_triggered
                        and troops is not None
                        and troops < dispatch_troops * 0.9
                    )
                    if should_exit:
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
        if has_active and not self._had_active_players:
            self._strength_tracker.reset()
            res.reset_team_detection()
            # sonst wuerden die niedrigen Start-Truppenzahlen des neuen
            # Matches faelschlich gegen die hohen Endstand-Werte des
            # vorherigen Matches als "Einbruch" gewertet und fuer die ersten
            # GLITCH_CONFIRM_TICKS Polls unterdrueckt (siehe
            # _debounce_troop_drops).
            self._last_confirmed_troops = {}
            self._pending_drop_counts = {}
            self._last_confirmed_units = {}
            self._pending_unit_drop_counts = {}
            # sonst koennte der hohe attackNumber-Endstand des alten
            # Matches faelschlich als "gerade gesunken" gegen den neuen
            # Match-Startwert (1) gewertet werden - kein echter Fehler
            # (ein SINKEN loest ohnehin nie "GREIFT AN" aus), aber sauberer
            # als auf den impliziten Selbstkorrektur-Effekt zu vertrauen.
            self._last_attack_number = {}
            self._attacking_active = {}
            self._attack_dispatch_troops = {}
            self._last_confirmed_lord_hp = {}
            self._pending_lord_hp_drop_counts = {}
            self._last_confirmed_population = {}
            self._pending_population_drop_counts = {}
            self._last_confirmed_population_capacity = {}
            self._pending_population_capacity_drop_counts = {}
            self._last_confirmed_siege = {}
            self._pending_siege_drop_counts = {}
            self._last_confirmed_monks = {}
            self._pending_monks_drop_counts = {}
            # Eingefrorene "besiegt"-Karten des VORHERIGEN Matches muessen
            # jetzt weg, sonst wuerden sie als Geister-Spieler im neuen
            # Match weiter mitgeschleppt (deren echter Slot koennte im
            # neuen Match sogar mit einem anderen Spieler belegt sein).
            self._last_confirmed_active_values = {}
            self._pending_inactive_counts = {}
            self._confirmed_defeated = set()
            # sonst wuerden fuer bis zu WIN_PROB_REFRESH_S Sekunden die
            # gecachten Gewinnwahrscheinlichkeiten des VORHERIGEN Matches
            # weiterangezeigt.
            self._last_win_prob_update_ts = 0.0
            self._cached_win_probs = {}
            self._cached_sides = []
            self._win_prob_displayed = {}
            self._win_prob_jump_pending = {}
            self._side_win_prob_displayed = {}
            self._side_win_prob_jump_pending = {}
        self._had_active_players = has_active

        res.poll_team_detection(all_values)

        # Effektive Team-Zuordnung: ENTWEDER/ODER je nach
        # config["team_assignment_mode"], keine Vermischung pro Slot (siehe
        # config.py-Kommentar - ein Agenten-Review deckte auf, dass die
        # frühere "manuell gewinnt, sobald irgendein Slot gesetzt ist"-Logik
        # bei nur teilweise ausgefüllter manueller Liste ALLE anderen Slots
        # fälschlich aus der automatischen Erkennung rausfallen ließ). Wird
        # sowohl fuer die Gewinnwahrscheinlichkeit unten als auch (ueber
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
