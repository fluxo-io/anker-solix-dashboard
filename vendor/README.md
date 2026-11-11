# Gemeinsame Anmeldung

`fluxo_auth-1.1.0-py3-none-any.whl` ist die unveränderte, versionierte Bibliothek aus
`CODEX/packages/auth`. Der Build installiert diese lokale Datei; zur Laufzeit
besteht keine Verbindung zur zentralen Bibliothek. Der zugehörige SHA256 steht
in `SHA256SUMS`. Änderungen erfolgen ausschließlich in der gemeinsamen Quelle.

Lokale Python-Abhängigkeiten vom Projektwurzelverzeichnis aus installieren,
damit der feste Pfad `./vendor/` gleich wie im Docker-Build aufgelöst wird.
