"""Einstiegspunkt der App (sowohl im Dev-Betrieb als auch im gepackten
PyInstaller-Build).

Ablauf: Konfiguration laden -> Worker-Thread starten -> HTTP-Server
starten -> Tray-Icon starten -> Browser mit der Einstellungs-Seite
öffnen -> warten, bis der Idle-Watchdog oder "Beenden" im Tray-Menü
das Beenden anstößt -> sauber aufräumen.

Der Release-Build läuft ohne Konsole (--noconsole) - eine unbehandelte
Ausnahme wäre dort sonst völlig unsichtbar. Deshalb fängt main() alles
ab und zeigt Fehler notfalls per natives Tkinter-Meldungsfenster an.
"""

import sys
import threading
import webbrowser

import config as config_module
import hotkeys
from server import AppContext, start_server
from tray import TrayIcon
from worker import StateStore, Worker


def _show_fatal_error(message: str):
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("SHC Live Stats - Fehler", message)
        root.destroy()
    except Exception:
        print(f"[FATAL] {message}", file=sys.stderr)


def run():
    cfg = config_module.load_config()
    port = cfg.get("http_port", 8765)
    settings_url = f"http://127.0.0.1:{port}/"

    state = StateStore()
    worker = Worker(cfg, state)
    worker.start()

    ctx = AppContext(cfg, worker, state, tray_ref_holder={})

    stop_event = threading.Event()
    httpd_holder = {}

    def do_shutdown():
        tray = ctx.tray_ref_holder.get("tray")
        if tray is not None:
            tray.stop()
        httpd = httpd_holder.get("httpd")
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        worker.stop()
        hotkeys.unregister_all()
        stop_event.set()

    ctx.on_shutdown = do_shutdown

    httpd = start_server(ctx, port)
    httpd_holder["httpd"] = httpd

    tray = TrayIcon(settings_url, on_quit=ctx.request_shutdown)
    tray.start()
    ctx.tray_ref_holder["tray"] = tray

    webbrowser.open(settings_url)

    # Haupt-Thread blockiert, bis der Idle-Watchdog oder "Beenden" im
    # Tray-Menü das Beenden auslöst - alle anderen Threads sind Daemons,
    # daher reicht ein sauberes Rückkehren hier, um den Prozess zu beenden.
    stop_event.wait()


def main():
    try:
        run()
    except OSError as e:
        _show_fatal_error(
            f"Konnte nicht starten (evtl. Port bereits belegt?):\n{e}"
        )
    except Exception as e:
        _show_fatal_error(f"Unerwarteter Fehler:\n{e}")


if __name__ == "__main__":
    main()
