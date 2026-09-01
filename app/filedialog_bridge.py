"""Bruecke zu einem nativen Windows-Datei-Dialog, aufrufbar aus einem HTTP-
Request-Handler heraus. Die Einstellungs-UI ist eine Webseite, aber ein
normales <input type="file"> in einem Browser-Tab liefert aus
Sicherheitsgruenden keinen echten Dateisystem-Pfad zurueck - deshalb wird
hier stattdessen ein echter tkinter.filedialog (unter Windows der native
Win32-Dialog) serverseitig geoeffnet, wenn der Nutzer auf einen Button in
der Einstellungs-Seite klickt.

Laeuft synchron im aufrufenden Request-Thread - blockiert also nur diese
eine HTTP-Anfrage, waehrend der Dialog offen ist. Der ThreadingHTTPServer
sorgt dafuer, dass gleichzeitiges /overlay.json-Polling durch OBS davon
unberuehrt bleibt. Ein Lock verhindert, dass ein Doppelklick zwei Dialoge
gleichzeitig oeffnet.
"""

import threading

_dialog_lock = threading.Lock()


def pick_file(title: str, filetypes, initialdir=None):
    """Oeffnet einen nativen 'Datei oeffnen'-Dialog. Gibt den gewaehlten
    Pfad zurueck, oder None bei Abbruch."""
    with _dialog_lock:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askopenfilename(
                title=title, filetypes=filetypes, initialdir=initialdir, parent=root
            )
            return path or None
        finally:
            root.destroy()


def pick_aic_file():
    return pick_file(
        "Waehle die .aic-Personality-Datei (z.B. vanilla.json)",
        [("AIC/JSON-Dateien", "*.json"), ("Alle Dateien", "*.*")],
    )


def pick_logo_file():
    return pick_file(
        "Waehle ein Logo-Bild",
        [("Bilder", "*.png *.jpg *.jpeg *.gif *.bmp"), ("Alle Dateien", "*.*")],
    )
