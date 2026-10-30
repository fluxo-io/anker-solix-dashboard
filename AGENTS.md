# Projektvertrag

Dieses Projekt verwendet `fluxo-webapp/1.0` und Web → API → Datenhaltung. Das maschinenlesbare Profil steht in `webapp-profile.json`.

- Die API besitzt Fachbestände, Geschäftsregeln, maßgebliche Rechte, Geschäftstransaktionen und Pflichtaudit. Web und Worker verwenden den API-Client; kein direkter Fachstore oder Datenbank-Fallback im Web.
- Anker-Dienstgrenze: `API_TOKEN` ist nur für Web-Lese-/Authoperationen; `COLLECTOR_API_TOKEN` nur für Messwert-/Statuspublikation und Collectorbereitschaft. Neue API-Operationen müssen in der expliziten Dienstmatrix klassifiziert und negativ getestet werden. Niemals beide Schlüssel gleichsetzen oder an den jeweils anderen Dienst weiterreichen.
- Allgemeine Aktionsmeldungen verwenden das feste Paket `@fluxo/web-ui`; Feldfehler, Datenfrische und dauerhafte Zustände bleiben am fachlichen Ort. Kurze Texte aus Nutzersicht.
- Frontend-Manifeste, Lockdatei, Quellen und feste Paketarchive liegen in `frontend/`. Kein Laufzeitzugriff auf die zentrale Bibliothek.
- `compose.yaml` ist neutral. Lokal `compose.local.yaml`, für den Server `compose.webserver.yaml` ausdrücklich wählen. Compose-Namen, Datenpfade, Besitzrechte und Recovery-Verträge erhalten.
- Prüfungen stehen in `scripts/check`; sie verwenden synthetische Konfiguration und temporäre Daten. Keine private `.env` in Prüfausgaben, keine Produktdaten oder Bestandsdienste für Tests.
- Keine Commits oder Änderungen bestehender Commits ohne ausdrückliche Freigabe eines konkreten Conventional-Commit-Vorschlags. Push benötigt danach eine eigene Freigabe.
- Dateisicherungen: alle persistenten Runtime-Mounts in `file-backup.json` erfassen; `scripts/file_backup.py` bleibt bytegleich zur gemeinsamen Vorlage. UID/GID, bestehende Hostpfade und Collector-Stopfrist bewahren. Nur private kalte Snapshots, Quellen nie mit SQLite öffnen; isoliert wiederherstellen. Neue Dateibestände benötigen Backup- und Restoreprüfung. Server-Backupjobs werden separat konfiguriert.
