# CLAUDE.md

Stronghold Live Stats — Live-Stats-Overlay für Stronghold-Crusader-Streams.

## Geräte-Sync (PC ↔ Notebook) — verbindlich

Dieses Projekt wird von mehreren Geräten aus bearbeitet und ausschließlich über GitHub synchronisiert.

- **Session-Start:** Zuerst `git fetch` und prüfen, ob `origin/master` neuer ist als der lokale Stand. Falls ja: **erst `git pull`**, bevor an etwas Neuem gearbeitet wird.
- **Session-Ende:** Alle Änderungen committen und `git push` — falls eine Projekt-Memory-Datei (z. B. `MEMORY.md`) existiert oder angelegt wird, gehört sie mit ins Repo (nicht gitignored) und wird mitgepusht, damit der Stand sofort auf dem anderen Gerät verfügbar ist.
- Bei Konflikten (lokal und `origin` beide neu) nie überschreiben (`reset --hard`, `push --force`) — den Nutzer fragen.
