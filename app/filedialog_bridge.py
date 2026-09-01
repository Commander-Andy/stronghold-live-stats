"""Brücke zu einem nativen Windows-Datei-Dialog, aufrufbar aus einem HTTP-
Request-Handler heraus. Die Einstellungs-UI ist eine Webseite, aber ein
normales <input type="file"> in einem Browser-Tab liefert aus
Sicherheitsgründen keinen echten Dateisystem-Pfad zurück - deshalb wird
hier stattdessen ein echter tkinter.filedialog (unter Windows der native
Win32-Dialog) serverseitig geöffnet, wenn der Nutzer auf einen Button in
der Einstellungs-Seite klickt.

Läuft synchron im aufrufenden Request-Thread - blockiert also nur diese
eine HTTP-Anfrage, während der Dialog offen ist. Der ThreadingHTTPServer
sorgt dafür, dass gleichzeitiges /overlay.json-Polling durch OBS davon
unberührt bleibt. Ein Lock verhindert, dass ein Doppelklick zwei Dialoge
gleichzeitig öffnet.
"""

import threading

_dialog_lock = threading.Lock()


def pick_file(title: str, filetypes, initialdir=None):
    """Öffnet einen nativen 'Datei öffnen'-Dialog. Gibt den gewählten
    Pfad zurück, oder None bei Abbruch."""
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
        "Wähle die .aic-Personality-Datei (z.B. vanilla.json)",
        [("AIC/JSON-Dateien", "*.json"), ("Alle Dateien", "*.*")],
    )


def pick_logo_file():
    return pick_file(
        "Wähle ein Logo-Bild",
        [("Bilder", "*.png *.jpg *.jpeg *.gif *.bmp"), ("Alle Dateien", "*.*")],
    )
