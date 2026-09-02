"""Lokaler HTTP-Server (nur 127.0.0.1 - loopback, damit nie eine Windows-
Firewall-Abfrage ausgelöst wird) für die Einstellungs-Seite, das
OBS-Overlay und die kleine JSON-API dazwischen.

Enthält zusätzlich den Idle-Shutdown-Watchdog: die App soll sich beenden,
sobald WEDER die Einstellungs-Seite NOCH OBS (das laufend /overlay.json
abruft) noch aktiv ist - damit ein versehentlich geschlossener
Einstellungs-Tab niemals ein laufendes Overlay mitten im Stream absterben
lässt.
"""

import json
import mimetypes
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import config as config_module
import filedialog_bridge
from paths import app_data_dir, resource_path

IDLE_CHECK_INTERVAL_S = 15
DEFAULT_IDLE_GRACE_PERIOD_S = 60


class AppContext:
    """Gemeinsamer Zustand, den Handler-Instanzen (die der HTTPServer pro
    Anfrage neu erzeugt) über Klassenattribute erreichen können."""

    def __init__(self, config: dict, worker, state, tray_ref_holder):
        self.config = config
        self.worker = worker
        self.state = state
        self.tray_ref_holder = tray_ref_holder  # dict mit "tray" Key, später befüllt
        self.last_settings_heartbeat = time.time()
        self.last_overlay_poll = time.time()
        self._lock = threading.Lock()
        self._shutdown_requested = False
        self.on_shutdown = None  # von main.py gesetzt

    def touch_settings(self):
        with self._lock:
            self.last_settings_heartbeat = time.time()

    def touch_overlay(self):
        with self._lock:
            self.last_overlay_poll = time.time()

    def idle_seconds(self):
        with self._lock:
            newest = max(self.last_settings_heartbeat, self.last_overlay_poll)
        return time.time() - newest

    def request_shutdown(self):
        with self._lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
        if self.on_shutdown:
            self.on_shutdown()


def _idle_watchdog(ctx: AppContext):
    while True:
        time.sleep(IDLE_CHECK_INTERVAL_S)
        grace_period = ctx.config.get("idle_shutdown_seconds", DEFAULT_IDLE_GRACE_PERIOD_S)
        if grace_period <= 0:
            continue  # 0 = Auto-Beenden deaktiviert, nur noch über Tray-Menü
        if ctx.idle_seconds() >= grace_period:
            ctx.request_shutdown()
            return


ICON_MIME = "image/png"


def make_handler(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler druckt Zugriffe standardmäßig auf stderr -
        # im fensterlosen Build gibt es davon niemanden, der es sieht, und es
        # kostet nur Leistung. Abschalten.
        def log_message(self, fmt, *args):
            pass

        # ---- Hilfsfunktionen ----

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, content_type=None):
            if not path.exists():
                self.send_error(404, "Not Found")
                return
            data = path.read_bytes()
            if content_type is None:
                content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if path.suffix == ".html":
                # overlay.html/win_bar.html/etc. ändern sich bei App-Updates -
                # OBS-Browser-Quellen (CEF) UND normale Browser-Tabs cachen die
                # Seite selbst sonst und holen bei einem simplen Reload nicht
                # zwingend eine neue Version, nur die per JS gepollten
                # /overlay.json-Werte aktualisieren sich (das hat schon einmal
                # zu Verwirrung geführt: ein CSS-Fix "kam nicht an", obwohl der
                # Server längst die neue Datei auslieferte - die offene Seite
                # hatte einfach nur nie neu nachgefragt).
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def _read_raw_body(self):
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return b""
            return self.rfile.read(length)

        def _query_slot(self):
            """Liest ?slot=N aus der Anfrage, validiert 0-7. Gibt None bei
            fehlendem/ungültigem Wert zurück."""
            qs = parse_qs(urlsplit(self.path).query)
            try:
                slot = int(qs.get("slot", [""])[0])
            except (ValueError, IndexError):
                return None
            return slot if 0 <= slot <= 7 else None

        # ---- GET ----

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path == "/" or path == "/settings.html":
                ctx.touch_settings()
                self._send_file(resource_path("assets", "settings.html"), "text/html; charset=utf-8")
            elif path == "/overlay.html":
                self._send_file(resource_path("assets", "overlay.html"), "text/html; charset=utf-8")
            elif path == "/overview.html":
                self._send_file(resource_path("assets", "overview.html"), "text/html; charset=utf-8")
            elif path == "/win_bar.html":
                self._send_file(resource_path("assets", "win_bar.html"), "text/html; charset=utf-8")
            elif path == "/overview-settings.html":
                ctx.touch_settings()
                self._send_file(resource_path("assets", "overview_settings.html"), "text/html; charset=utf-8")
            elif path == "/overlay.json":
                ctx.touch_overlay()
                self._send_json(ctx.state.snapshot())
            elif path == "/overlay-config.json":
                self._send_json({
                    "display": ctx.config.get("display", {}),
                    "layout": ctx.config.get("layout", {}),
                    "logo": ctx.config.get("logo", {}),
                    "player_colors": ctx.config.get("player_colors", {}),
                    # Manuelle Zuordnung gewinnt immer, automatische
                    # Erkennung (siehe worker.py) fuellt nur auf, wenn
                    # manuell nichts gesetzt ist - die Vermischung passiert
                    # schon in Worker._tick(), hier nur auslesen.
                    "team_assignment": ctx.state.get_effective_team_assignment(),
                    "team_colors": ctx.config.get("team_colors", {}),
                    "win_bar": ctx.config.get("win_bar", {}),
                })
            elif path == "/api/config":
                self._send_json(ctx.config)
            elif path == "/api/overview-config":
                self._send_json({
                    **ctx.config.get("overview", {}),
                    "team_assignment": ctx.state.get_effective_team_assignment(),
                    "player_colors": ctx.config.get("player_colors", {}),
                    "team_colors": ctx.config.get("team_colors", {}),
                    "win_bar": ctx.config.get("win_bar", {}),
                })
            elif path == "/api/status":
                self._send_json(ctx.state.status())
            elif path == "/logo.png":
                logo_path = app_data_dir() / "logo.png"
                self._send_file(logo_path, ICON_MIME)
            elif path.startswith("/icons/") and path.endswith(".png"):
                name = path[len("/icons/"):]
                if "/" in name or ".." in name:
                    self.send_error(400, "Bad Request")
                    return
                self._send_file(resource_path("assets", "icons", name), ICON_MIME)
            elif path == "/bg/program.png":
                self._send_file(resource_path("assets", "backgrounds", "program_bg.png"), ICON_MIME)
            elif path == "/bg/preview.png":
                self._send_file(resource_path("assets", "backgrounds", "preview_bg.png"), ICON_MIME)
            elif path == "/bg/frame-parchment.png":
                self._send_file(resource_path("assets", "backgrounds", "frame_parchment.png"), ICON_MIME)
            elif path.startswith("/player-bg/") and path.endswith(".png"):
                slot_str = path[len("/player-bg/"):-len(".png")]
                if not slot_str.isdigit() or not (0 <= int(slot_str) <= 7):
                    self.send_error(400, "Bad Request")
                    return
                self._send_file(app_data_dir() / f"player_bg_{slot_str}.png", ICON_MIME)
            else:
                self.send_error(404, "Not Found")

        # ---- POST ----

        def do_POST(self):
            path = self.path.split("?", 1)[0]

            if path == "/api/config":
                patch = self._read_json_body()
                ctx.config = config_module.merge_and_save(ctx.config, patch)
                ctx.worker.update_config(ctx.config)
                self._send_json(ctx.config)
            elif path == "/api/overview-config":
                patch = self._read_json_body()
                # team_assignment/win_bar sind TOP-LEVEL geteilt (Overlay +
                # Übersicht nutzen dieselbe Zuordnung/Balken-Config, kein
                # "overview"-Duplikat) - deshalb hier rausgezogen und separat
                # gemerged statt unter "overview" verschachtelt zu werden.
                team_patch = patch.pop("team_assignment", None)
                win_bar_patch = patch.pop("win_bar", None)
                full_patch = {"overview": patch}
                if team_patch is not None:
                    full_patch["team_assignment"] = team_patch
                if win_bar_patch is not None:
                    full_patch["win_bar"] = win_bar_patch
                ctx.config = config_module.merge_and_save(ctx.config, full_patch)
                self._send_json({
                    **ctx.config.get("overview", {}),
                    "team_assignment": ctx.config.get("team_assignment", {}),
                    "player_colors": ctx.config.get("player_colors", {}),
                    "team_colors": ctx.config.get("team_colors", {}),
                    "win_bar": ctx.config.get("win_bar", {}),
                })
            elif path == "/api/heartbeat":
                ctx.touch_settings()
                self._send_json({"ok": True})
            elif path == "/api/pick-aic-file":
                chosen = filedialog_bridge.pick_aic_file()
                self._send_json({"path": chosen})
            elif path == "/api/pick-logo-file":
                chosen = filedialog_bridge.pick_logo_file()
                saved_path = None
                if chosen:
                    dest = app_data_dir() / "logo.png"
                    try:
                        shutil.copyfile(chosen, dest)
                        saved_path = str(dest)
                    except Exception:
                        saved_path = None
                self._send_json({"path": saved_path})
            elif path == "/api/upload-aic":
                # Für Drag&Drop: der Browser gibt bei Dateien keinen echten
                # Dateisystempfad heraus (Sicherheitsbeschränkung), also wird
                # hier stattdessen der Dateiinhalt hochgeladen und lokal
                # gespeichert - die App nutzt danach diesen gespeicherten Pfad.
                raw = self._read_raw_body()
                dest = app_data_dir() / "uploaded_aic.json"
                try:
                    dest.write_bytes(raw)
                    ctx.config = config_module.merge_and_save(ctx.config, {"aic_file_path": str(dest)})
                    ctx.worker.update_config(ctx.config)
                    self._send_json({"path": str(dest)})
                except Exception as e:
                    self._send_json({"path": None, "error": str(e)}, status=500)
            elif path == "/api/upload-logo":
                raw = self._read_raw_body()
                dest = app_data_dir() / "logo.png"
                try:
                    dest.write_bytes(raw)
                    ctx.config = config_module.merge_and_save(
                        ctx.config, {"display": {"logo_path": str(dest), "show_logo": True}}
                    )
                    ctx.worker.update_config(ctx.config)
                    self._send_json({"path": str(dest)})
                except Exception as e:
                    self._send_json({"path": None, "error": str(e)}, status=500)
            elif path == "/api/upload-player-bg":
                slot = self._query_slot()
                if slot is None:
                    self.send_error(400, "Bad Request")
                    return
                raw = self._read_raw_body()
                dest = app_data_dir() / f"player_bg_{slot}.png"
                try:
                    dest.write_bytes(raw)
                    self._send_json({"ok": True})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, status=500)
            elif path == "/api/clear-player-bg":
                slot = self._query_slot()
                if slot is None:
                    self.send_error(400, "Bad Request")
                    return
                dest = app_data_dir() / f"player_bg_{slot}.png"
                dest.unlink(missing_ok=True)
                self._send_json({"ok": True})
            else:
                self.send_error(404, "Not Found")

    return Handler


def start_server(ctx: AppContext, port: int) -> ThreadingHTTPServer:
    handler_cls = make_handler(ctx)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    watchdog_thread = threading.Thread(target=_idle_watchdog, args=(ctx,), daemon=True)
    watchdog_thread.start()

    return httpd
