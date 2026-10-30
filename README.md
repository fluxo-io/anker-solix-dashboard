# Private Solardatenbank

Fluxo-Demoprojekt für private Anker-SOLIX-Messwerte. Das Dashboard zeigt
Leistung, Ladezustand, Energie und Geräteinformationen.

## Aufbau

```text
web/                     Plotly Dash / Flask, ausschließlich API-Zugriff
api/                     Flask-API und SQLite
collector/               Anker-Cloud abrufen und per API speichern
tests/{web,api,collector} isolierte Tests
docker/entrypoint.py      Berechtigungen für Datenordner
Dockerfile               Laufzeit- und Testziele
compose.yaml             gemeinsame Dienste
compose.local.yaml       lokaler HTTP-Betrieb
compose.webserver.yaml   HTTPS über Reverse Proxy
frontend/                feste Design-/UI-Pakete und Asset-Build
webapp-profile.json      Web/API-Vertrag
.env.example             Konfigurationsvorlage
data/db/app.sqlite3      Messwerte, Benutzer und Anmeldeversuche
data/collector/authcache/ Anker-Anmeldecache
```

Dieser Aufbau, die Dienste `web`/`api`, die gemeinsamen ENV-Namen und die
Docker-Konfiguration entsprechen `www.fluxo.io`; hier kommt der Collector hinzu.
Die API ist nur im internen Compose-Netz erreichbar. Web und Collector greifen
über `API_URL=http://api:8001` mit getrennten Schlüsseln auf sie zu: `API_TOKEN`
für Web, `COLLECTOR_API_TOKEN` für den Collector. Nur die API öffnet SQLite.

`data/` wird vom Docker-Host direkt eingebunden. Host und Container arbeiten auf
denselben Dateien, Änderungen bleiben auch nach Neubau und Neustart erhalten.
Es gibt keine automatische Datenübernahme. Ein leerer Datenordner wird
beim ersten Start initialisiert; neue Messwerte kommen ab dann vom Collector.
Alte PostgreSQL-Volumes und vorhandene Sicherungen werden nicht verändert.

## Einrichtung

```bash
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
```

Die drei unabhängigen Zufallswerte als `SECRET_KEY`, `API_TOKEN` und
`COLLECTOR_API_TOKEN` eintragen.
`INIT_ADMIN_USERNAME`, ein mindestens zwölf Zeichen langes
`INIT_ADMIN_PASSWORD` sowie `ANKER_USERNAME` und `ANKER_PASSWORD` setzen.
Alternativ kann `INIT_ADMIN_PASSWORD_HASH` einen vorhandenen sicheren
Scrypt-/PBKDF2-Hash für den ersten Benutzer enthalten. Ein gesetztes
Klartextpasswort hat Vorrang. Init-Werte ändern einen vorhandenen Benutzer nicht.

`DATA_DIR` wählt den Host-Datenordner, standardmäßig `./data`.

Die Container bereiten ausschließlich ihren eigenen Daten-Unterordner vor und
starten danach die Anwendung mit UID/GID 1000. Dadurch funktionieren auch beim
Start neu angelegte Bind-Mounts. `data/` darf keine symbolischen Links enthalten.
`.env`, `data/` und `backups/` sind von Git und Docker-Builds ausgeschlossen.

## Start (Webserver)

```bash
docker compose -f compose.yaml -f compose.webserver.yaml config --quiet
docker compose -f compose.yaml -f compose.webserver.yaml up --build -d
```

Dashboard: <https://kraftwerk.fluxo.io>. Das Overlay wird ausdrücklich gewählt;
`compose.yaml` allein veröffentlicht keinen Hostport. Ein automatisches Override gibt es nicht mehr.

Das externe Netz `REVERSE_PROXY_NETWORK` muss bestehen. Der Proxy erreicht den
Webdienst über `kraftwerk-web:8000`. `CANONICAL_ORIGIN` ist standardmäßig
`https://kraftwerk.fluxo.io`; das Overlay aktiviert sichere Cookies und vertraut
genau einem vorgeschalteten Proxy. API und SQLite erhalten keinen Host-Port.

## Lokal starten

```bash
docker compose -f compose.yaml -f compose.local.yaml config --quiet
docker compose -f compose.yaml -f compose.local.yaml up --build -d
```

Dashboard: <http://127.0.0.1:8501>. `PORT` ändert den lokalen Port.

## Konfiguration

Die gemeinsamen Variablen sind `SECRET_KEY`, `API_TOKEN`, `API_URL`,
`API_TIMEOUT_SECONDS`, `INIT_ADMIN_USERNAME`, `INIT_ADMIN_PASSWORD`,
`INIT_ADMIN_PASSWORD_HASH`, `PORT`, `DATA_DIR`, `SESSION_COOKIE_SECURE`,
`SESSION_HOURS`, `CANONICAL_ORIGIN`, `TRUST_PROXY` und `REVERSE_PROXY_NETWORK`.
Der getrennte `COLLECTOR_API_TOKEN` ist ausschließlich für API und Collector bestimmt.
Die vollständige Vorlage steht in `.env.example`. Compose setzt `API_URL` auf
die interne Adresse; für direkten Python-Betrieb kann eine andere Adresse dienen.

Anker-spezifisch sind die Kontodaten, Land, optionale Gerätenummer,
`POLL_INTERVAL_SECONDS` (mindestens 60, standardmäßig 300), Cloud-Zeitlimit,
Wiederholungen, Log-Level und die Anzeigezeitzonen. `ANKER_SOURCE_TIMEZONE` bleibt
leer, wenn die Zeitzone der Cloud-Messzeit unbekannt ist. `AUTH_ENABLED=false`
erlaubt lokal eine offene Ansicht; das Webserver-Overlay erzwingt die Anmeldung.

SQLite nutzt Transaktionen, ein Wartezeitlimit bei Schreibsperren und das
DELETE-Journal. Zeitpunkte werden in UTC gespeichert. Wiederholte Messungen
ersetzen vorhandene Zeitblöcke kontrolliert, ohne gültige durch ungültige Werte
zu überschreiben. Diagramme und Energieberechnung unterscheiden fehlende,
veraltete und gültige Werte.

## Hostdateien und Backups

Die vollständige Datenablage liegt auf dem Webserver-Dateisystem und ist für
berechtigte SSH-/SFTP-Konten sowie Dateibackups erreichbar. Der vorhandene
`DATA_DIR` und die beiden Unterordner bleiben unverändert. Es wird kein
HTTP-Dateizugriff auf Datenbanken oder Anmeldecaches eingerichtet.

| Bestand | Hostpfad | Snapshotpfad |
| --- | --- | --- |
| SQLite einschließlich möglicher Journale | `${DATA_DIR}/db` (Standard `./data/db`) | `sources/database/` |
| Vollständiger Collectorbestand einschließlich Anker-Anmeldecache und weiterer Dateien | `${DATA_DIR}/collector` (Standard `./data/collector`) | `sources/collector/` |

Der Collector verwendet `/data` auch für seine persistente temporäre Ablage;
deshalb wird der gesamte Ordner gesichert. Sein Heartbeat in `/tmp` ist flüchtig
und wird beim Start neu erzeugt. Die Browser-Sitzung liegt im signierten Cookie.

API und Collector verwenden UID/GID 1000:1000, private Dateien sind 0600.
Der Betreiber benötigt passende Eigentümer-/Gruppenrechte, gezielte ACLs oder
administrative Rechte für SSH/SFTP und Backups. Keine pauschale Freigabe für
alle Benutzer. Die Stoppreihenfolge lautet `collector` → `web` → `api`; die
vorhandene dreiminütige Collector-Stopfrist bleibt wirksam.


### Konsistente Dateisicherung

`file-backup.json` erfasst die vollständigen persistenten Mounts. Das gemeinsame
stdlib-Werkzeug läuft mit Python 3.10+ und Docker Compose auf dem Docker-Host.
`inventory` zeigt die aufgelösten Hostpfade, keine ENV-Inhalte. Profil,
`--env-file` und gegebenenfalls `--project-name` müssen dem laufenden Deployment
entsprechen. Vorhandene Container-Mounts werden mit Compose abgeglichen; ein
abweichender alter Datenpfad oder ein noch genutztes Volume führt zum Abbruch.

```sh
python3 scripts/file_backup.py --profile webserver --env-file .env inventory
python3 scripts/file_backup.py --profile webserver --env-file .env create --output ./backups --stop-services
python3 scripts/file_backup.py verify ./backups/backup-DATUM-ID
mkdir -p ./restores
python3 scripts/file_backup.py restore ./backups/backup-DATUM-ID ./restores/probe
```

Für lokale Installationen `--profile local` verwenden. `--stop-services` hält die
zuvor laufenden Projektdienste in der festgelegten Reihenfolge an und startet nur
diese bestehenden Container anschließend wieder, auch bei Fehlern oder SIGINT/
SIGTERM. Dabei werden keine Container neu erstellt. Ohne das Flag müssen alle
schreibenden Container bereits gestoppt sein. Andere Container, die denselben
Hostpfad schreibend nutzen, blockieren die Sicherung. Externe Dateischreiber wie
SFTP-Uploads, Synchronisierung und administrative Skripte müssen ebenfalls für
die gesamte Sicherung pausieren; erkannte Dateiänderungen verwerfen den Snapshot.

SQLite, JSON und sämtliche weiteren Dateien werden gemeinsam im Stillstand
kopiert. SQLite wird ausschließlich in der privaten Kopie geprüft und über die
SQLite-Backup-Schnittstelle normalisiert; vorhandene Journale/WAL gehen in die
fertige Datenbank ein. Quelldatenbanken werden nie durch SQLite geöffnet oder
verändert. Symlinks, Spezialdateien, fehlende Datenbanken und nicht erfasste
Runtime-Mounts führen zum Abbruch. Ein laufendes Datenverzeichnis direkt zu
kopieren ist keine konsistente Sicherung.

Ein vollständiger Snapshot enthält `sources/`, die gewählte `.env` unter
`configuration/env`, Basis- und ausgewähltes Compose-Overlay sowie das Profil.
`manifest.json` enthält Prüfsummen, ursprüngliche Hostpfade, UID/GID und Rechte;
`COMPLETE` bestätigt den geprüften Abschluss. Snapshot und Restore gehören dem
ausführenden Operator (Ordner 0700, Dateien 0600), Originalrechte bleiben
Metadaten. Sicherungen enthalten Zugangsdaten und sind vertraulich.

`verify` und `restore` benötigen weder Docker noch ENV-Dateien. Restore schreibt
ausschließlich in ein neues Ziel und überschreibt keine Installation. Vor einer
bewussten Übernahme die isolierten Daten prüfen, Dienste anhalten und die
Runtime-Rechte setzen: Datenablagen gehören UID/GID 1000:1000; SQLite-Dateien
bleiben 0600. Originaldaten und bisherige Sicherung bis zur erfolgreichen
Wiederanlaufprüfung erhalten. Die Dateien aus `configuration/` sind geschützte
Wiederherstellungskopien; Versionsstand und übrigen Deploymentcode separat sichern.

Der Server-Backupjob muss abgeschlossene `backups/backup-*`-Ordner mit `COMPLETE`
einschließen, temporäre `.file-*`-Ordner ausschließen und Kopien außerhalb dieses
Servers aufbewahren. Hier ist kein periodischer Job eingerichtet. Die tatsächliche
Einbindung, Aufbewahrung und ein regelmäßiger isolierter Restoretest werden in
der Serverkonfiguration festgelegt. Backup- und Restoreordner sind von Git und
Docker-Builds ausgeschlossen.

Toolprüfung ausschließlich mit synthetischen Daten:

```sh
python3 -m unittest discover -s tests/backup -v
```

## Prüfen und betreiben

```bash
docker compose -f compose.yaml --profile test build web-tests api-tests collector-tests
docker compose -f compose.yaml --profile test run --rm --no-deps web-tests
docker compose -f compose.yaml --profile test run --rm --no-deps api-tests
docker compose -f compose.yaml --profile test run --rm --no-deps collector-tests

docker compose -f compose.yaml -f compose.webserver.yaml ps
docker compose -f compose.yaml -f compose.webserver.yaml logs --tail=100 collector
docker compose -f compose.yaml -f compose.webserver.yaml down
```

Tests verwenden synthetische Daten ohne Cloud-Zugriff. `/healthz` prüft Web/API;
der Collector-Healthcheck prüft seine laufende Ereignisschleife. Die Frische der
Messwerte wird separat im Dashboard angezeigt. Die inoffizielle Anker-API kann
sich ändern.

## Gemeinsamer Webapp-Standard

Web → API → SQLite ist in `webapp-profile.json` festgehalten. Dash bleibt erhalten; seine fünf lesenden POST-Callbacks sind im Web ausdrücklich klassifiziert. Login, Logout und weitere schreibende Browserrouten benötigen CSRF. Sitzungen besitzen eine absolute Frist (`SESSION_HOURS`); die Anmeldung wird bei alten Cookies ohne Ausstellungszeit einmal erneut verlangt.

Aktionsmeldungen verwenden `@fluxo/web-ui` 1.0.2. Meldungsattribute tragen Text, Kategorie und Ereignis-ID; ein schmaler Adapter reicht Dash-Ereignisse weiter, ohne React-eigene Knoten zu entfernen. Datenfrische und Collectorzustand bleiben dauerhaft sichtbar. Fluxo-Design 0.4.0 und beide Pakete liegen fest in `frontend/vendor/`; keine Laufzeitverbindung zur Bibliothek.

`npm --prefix frontend ci && npm --prefix frontend run build` erzeugt Hashassets und Manifest. Öffentliche Jinja-Seiten und Dash verwenden denselben geprüften Manifest-Lookup. Die Factory akzeptiert Settings/AuthSettings und einen injizierten API-Transport; der Gunicorn-Einstieg ist `solarbank_dashboard.wsgi:server`.

Vor dem Upgrade sämtliche Start-/Deploymentaufrufe um das gewünschte Overlay ergänzen. Datenpfade, UID/GID, Compose-Name, getrennte Collectorablage und drei Minuten Stopfrist bleiben erhalten. Keine Datenmigration erforderlich.

### Gemeinsame Prüfungen

`scripts/check` prüft Profil, npm-Clean-Install/Build, Backendtests und synthetische
Compose-Konfigurationen. Der Profilchecker belegt deklarierte Verträge; er ersetzt
keine Fachtests. Die CI baut alle drei Laufzeitimages und führt anschließend
`scripts/docker_integration.py` aus. Der Test prüft echte Collector-HTTP-Publikation,
API-Persistenz nach Neustart, Web-Anmeldung/Hashassets und API-Ausfall. Er verwendet
nur neu benannte Container, eine neue Linux-Testvolume und ein internes Testnetz,
die abschließend entfernt werden. Es erfolgen keine Anker-Cloud-Anfragen.

Lokal können die drei Image-Tags ausdrücklich über `ANKER_TEST_API_IMAGE`,
`ANKER_TEST_WEB_IMAGE` und `ANKER_TEST_COLLECTOR_IMAGE` gewählt werden. Die produktive
Bindmount-Konfiguration wird durch diese Testvolume nicht verändert.

### Begrenzte Dienstidentitäten

Die API verifiziert zwei unterschiedliche Bearer-Schlüssel und legt den Dienstakteur
selbst fest. Browserparameter können diese Zuordnung nicht ändern. Neue Endpunkte
erhalten keine implizite Berechtigung. Dies ist ein begrenzter Dienstvertrag für das
vorhandene private Dashboard; es entstehen keine neuen Benutzer- oder Fachrollen.

| Schlüssel / Dienst | Erlaubte Operationen |
| --- | --- |
| `API_TOKEN` / Web | GET/HEAD Geräte, Messwerte, letzte Messwerte, Snapshot, Collectorstatus; POST Login und Sessionprüfung |
| `COLLECTOR_API_TOKEN` / Collector | POST Messwerte, Abrufversuch, Abruffehler; GET/HEAD `/v1/collector/ready` |
| ohne Schlüssel | GET/HEAD `/healthz`, ausschließlich minimale Bereitschaft |

Falscher oder fehlender Schlüssel ergibt 401, ein gültiger Schlüssel außerhalb
seiner erlaubten Operationen 403. Der Collectorschlüssel erlaubt keine Fachlese-
oder Benutzeroperationen; der Webschlüssel erlaubt keine Messwert-/Statusänderung.
Beide Schlüssel müssen verschieden sein und mindestens 32 Zeichen ohne Leerzeichen
haben. Der Collector prüft seine Berechtigung beim Öffnen des API-Clients über
`/v1/collector/ready`, bevor er Messwerte publiziert. Der Endpunkt verändert nichts.

**Upgrade bestehender Installationen:** `API_TOKEN` und `SECRET_KEY` bleiben erhalten.
Einmal einen unabhängigen `COLLECTOR_API_TOKEN` erzeugen (beispielsweise
`openssl rand -hex 32`) und ausschließlich in der geschützten Betriebsumgebung
hinterlegen. Die Compose-Konfiguration fordert diesen Schlüssel für API und Collector;
sie gibt ihn nicht an Web weiter und gibt dem Collector nicht den Webschlüssel.
Für einen geplanten zusammenhängenden Wechsel den Collector anhalten, die API und
Web mit dem ausgewählten Overlay aktualisieren und danach den aktualisierten
Collector starten. Vor dem Start `docker compose -f compose.yaml -f
compose.webserver.yaml config --quiet` beziehungsweise das lokale Overlay prüfen.
Es werden keine Daten verschoben, Benutzer neu angelegt oder vorhandene Schlüssel
ersetzt. Ein direkt gestarteter API-Dienst ohne Collectorschlüssel bleibt für die
bisherigen Web-Lese-/Authaufrufe nutzbar, nimmt jedoch keine Collectoränderungen an.
Ein alter Collector mit nur `API_TOKEN` erhält nach dem API-Upgrade keine
Schreibberechtigung; es gibt absichtlich keinen vorübergehenden Schreibfallback.
