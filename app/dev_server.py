"""Dev-only Einstiegspunkt zum lokalen Testen der Einstellungsseiten im
Browser - startet denselben HTTP-Server wie main.py, aber OHNE Tray-Icon
und OHNE automatisches Öffnen eines System-Browserfensters (praktisch für
headless/Browser-Tool-gesteuertes Testen). Nicht Teil des gepackten
PyInstaller-Builds, siehe main.py für den echten App-Einstiegspunkt.
"""

import threading

import config as config_module
from server import AppContext, start_server
from worker import StateStore, Worker


def run():
    cfg = config_module.load_config()
    port = cfg.get("http_port", 8765)

    state = StateStore()
    worker = Worker(cfg, state)
    worker.start()

    ctx = AppContext(cfg, worker, state, tray_ref_holder={})
    start_server(ctx, port)

    print(f"Dev-Server laeuft auf http://127.0.0.1:{port}/ (Strg+C zum Beenden)")
    threading.Event().wait()


if __name__ == "__main__":
    run()
