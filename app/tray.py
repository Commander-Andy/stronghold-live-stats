"""Tray-Icon: laeuft ueber die gesamte Lebensdauer der App. Bietet
"Einstellungen oeffnen" (fuer den Fall, dass der Nutzer den Einstellungs-Tab
geschlossen hat, waehrend OBS die Overlay-Daten noch abruft) und "Beenden"
(manuelles Beenden ueber denselben sauberen Shutdown-Pfad wie der
Idle-Watchdog in server.py). Verschwindet automatisch, wenn die App beendet
wird - egal ob durch Inaktivitaet oder ueber "Beenden".
"""

import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw

from paths import resource_path


def _make_icon_image():
    """Laedt das App-Logo als Tray-Icon; faellt auf ein einfaches generisches
    Icon zurueck, falls die Asset-Datei aus irgendeinem Grund fehlt."""
    try:
        img = Image.open(resource_path("assets", "icons", "app_logo.png")).convert("RGBA")
        return img.resize((64, 64), Image.LANCZOS)
    except Exception:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, size - 4, size - 4), fill=(198, 161, 91, 255))
        draw.ellipse((4, 4, size - 4, size - 4), outline=(36, 26, 16, 255), width=3)
        draw.text((size / 2 - 10, size / 2 - 12), "S", fill=(36, 26, 16, 255))
        return img


class TrayIcon:
    def __init__(self, settings_url: str, on_quit):
        self._settings_url = settings_url
        self._on_quit = on_quit
        self._icon = pystray.Icon(
            "shc_live_stats",
            icon=_make_icon_image(),
            title="SHC Live Stats",
            menu=pystray.Menu(
                pystray.MenuItem("Einstellungen oeffnen", self._open_settings),
                pystray.MenuItem("Beenden", self._quit),
            ),
        )
        self._thread = None

    def _open_settings(self, icon=None, item=None):
        webbrowser.open(self._settings_url)

    def _quit(self, icon=None, item=None):
        self._on_quit()

    def start(self):
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._icon.stop()
        except Exception:
            pass
