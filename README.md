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
für Web, `COLLECTOR_API_TOKEN` für den Collector. Nur die API öffnet SQLite und berechnet Energie, Geräte-/Anlagensummen, Frische und Verfügbarkeit. `/v1/dashboard` liefert die fachlich ausgewerteten Daten; Web formatiert sie und zeichnet Diagramme.

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

### Anmeldung prüfen

Der Dashboard-Zugang verwendet das in SQLite gespeicherte Startkonto. Die
Anker-Cloud-Zugangsdaten `ANKER_USERNAME`/`ANKER_PASSWORD` gehören nur zum Collector.
Das Benutzerkonto liegt unter `<HOST_DATA_DIR>/db/app.sqlite3` in `auth_users`; der
Webdienst speichert seine Sitzung im signierten Browsercookie. Ein geändertes
`INIT_ADMIN_PASSWORD` setzt das Passwort eines vorhandenen Kontos nicht zurück.
`SECRET_KEY` beim Neubau beibehalten, sonst werden bestehende Sitzungen ungültig.

Bei Loginproblemen zuerst den gewählten Compose-Overlay, `config --quiet`, den
API-/Web-Healthcheck und den unveränderten Hostdatenpfad kontrollieren. Ein neuer
leerer Datenpfad wäre ein anderer Bestand. Im Server-Overlay sind HTTPS-Cookies
und Proxy-Vertrauen aktiv; beim lokalen HTTP-Start das lokale Overlay verwenden.
Nach Ablauf der konfigurierten Frist (standardmäßig acht Stunden) ist eine neue
Anmeldung erforderlich. Ein Wechsel der Clientadresse beendet die Sitzung nicht. `/logout` bleibt auch bei
API-Ausfall erreichbar; erst das bestätigte Formular beendet die lokale Sitzung.

`HOST_DATA_DIR` wählt den Host-Datenordner, standardmäßig `./data`. Wenn es nicht
gesetzt ist, bleibt der bisherige Hostalias `DATA_DIR` wirksam. Beide Werte bezeichnen
denselben vorhandenen Datenbestand; beim Upgrade keinen neuen leeren Pfad wählen.
Vor dem Start die Unterordner `db` und `collector` unter diesem Pfad ausdrücklich
anlegen (beim Standard: `mkdir -p data/db data/collector`).

Die Container bereiten ausschließlich ihren eigenen Daten-Unterordner vor und
starten danach die Anwendung mit UID/GID 1000. Fehlende Bind-Mount-Quellen werden
nicht automatisch angelegt; ein Tippfehler im Hostpfad führt zum Startabbruch. `data/` darf keine symbolischen Links enthalten.
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
`INIT_ADMIN_PASSWORD_HASH`, `PORT`, `HOST_DATA_DIR`, `SESSION_COOKIE_NAME`, `SESSION_COOKIE_SECURE`,
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
`HOST_DATA_DIR` (bisheriger Alias `DATA_DIR`) und die beiden Unterordner bleiben unverändert. Es wird kein
HTTP-Dateizugriff auf Datenbanken oder Anmeldecaches eingerichtet.

| Bestand | Hostpfad | Snapshotpfad |
| --- | --- | --- |
| SQLite einschließlich möglicher Journale | `${HOST_DATA_DIR}/db` (Standard `./data/db`) | `sources/database/` |
| Vollständiger Collectorbestand einschließlich Anker-Anmeldecache und weiterer Dateien | `${HOST_DATA_DIR}/collector` (Standard `./data/collector`) | `sources/collector/` |

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

Aktionsmeldungen verwenden `@fluxo/web-ui` 1.0.2. Dash liefert Text, Kategorie und stabile Ereignis-ID über drei `dcc.Store`-Kanäle. Ein registrierter Clientside-Callback übergibt neue Ereignisse an die gemeinsame Toast-Bibliothek und quittiert sie in einem begrenzten Speicher. Wiederholtes Polling erzeugt keine doppelten Meldungen; eine neue Aktion oder ein erneuter Ausfall nach Erholung darf erneut melden. Es gibt keine DOM-Beobachtung und keinen Eingriff in React-Knoten. Datenfrische und Collectorzustand bleiben dauerhaft sichtbar. Fluxo-Design 0.5.0 und beide Pakete liegen fest in `frontend/vendor/`; keine Laufzeitverbindung zur Bibliothek.

`npm --prefix frontend ci && npm --prefix frontend run build` erzeugt Hashassets und Manifest. Öffentliche Jinja-Seiten und Dash verwenden denselben geprüften Manifest-Lookup. Die Factory akzeptiert Settings/AuthSettings und einen injizierten API-Transport; der Gunicorn-Einstieg ist `solarbank_dashboard.wsgi:server`.

Vor dem Upgrade sämtliche Start-/Deploymentaufrufe um das gewünschte Overlay ergänzen. Datenpfade, UID/GID, Compose-Name, getrennte Collectorablage und drei Minuten Stopfrist bleiben erhalten. Für die gemeinsame Anmeldung gilt zusätzlich die unten beschriebene Auth-Migration.

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
| `API_TOKEN` / Web | GET/HEAD Geräte, Dashboardauswertung, Messwerte, letzte Messwerte, Snapshot, Collectorstatus; POST Login, Sessionprüfung und Logout |
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

### Home-Screen- und Dock-Verknüpfungen

Neue Verknüpfungen auf dem iPad und im Mac-Dock verwenden das gemeinsame blaue
Fluxo-Icon. Dash sowie Anmelde-, Rechts- und Fehlerseiten binden dieselben
Apple-Touch-Icons und `site.webmanifest` ein. Die festen PNGs liegen unter
`web/src/solarbank_dashboard/static/icons/fluxo/1.0.0/`. Manifest und Icons sind
auch ohne gültige Sitzung und ohne API-Aufruf erreichbar. Sie bleiben außerhalb
des bereinigten `dist/`-Ordners und werden vom Docker-Webimage mitkopiert.
Startadresse und Anzeigemodus werden nicht überschrieben. Bereits gespeicherte
Verknüpfungen nach dem Deployment bei Bedarf löschen und neu anlegen, damit das
Gerät sein zwischengespeichertes Icon ersetzt.


### ENV-Vertrag

| Variable | Typ / Einheit | Standard | Pflicht / geheim | Zuständigkeit |
| --- | --- | --- | --- | --- |
| `SECRET_KEY` | Zufallstext, ≥32 Zeichen | keiner | ja / ja | Web/API: Sitzungs- und Authkonfiguration |
| `API_TOKEN` | Text, ≥32 Zeichen ohne Leerzeichen | keiner | ja / ja | Web/API: begrenzter Lesedienst |
| `COLLECTOR_API_TOKEN` | separater Text, ≥32 Zeichen | keiner | Compose ja / ja | Collector/API: begrenzter Schreibdienst |
| `API_URL` | HTTP(S)-URL | direkt `http://127.0.0.1:8001`, Compose `http://api:8001` | nein / nein | Web/Collector: interne API |
| `API_TIMEOUT_SECONDS` | ganze Sekunden, 1–60 | `5` | nein / nein | Web/Collector: HTTP-Zeitlimit; Health höchstens 2 Sekunden |
| `API_DASHBOARD_TIMEOUT_SECONDS` | ganze Sekunden, 1–30 | `20` | nein / nein | Web: Auswertung großer Zeitfenster; normales API- und Healthbudget bleiben getrennt |
| `HOST_DATA_DIR` | Hostpfad | `./data` | Ordner vorbereiten / nein | API `db/`, Collector `collector/` |
| `DATA_DIR` | Hostalias / interner Containerpfad | Host `./data`, API `/data` | nein / nein | Hostalias nur ohne `HOST_DATA_DIR`; im API-Container fest `/data` |
| `PORT` | TCP-Port | `8501` | nein / nein | lokales Web-Overlay |
| `INIT_ADMIN_USERNAME` | Benutzername | `admin` | initial / nein | API: Startkonto |
| `INIT_ADMIN_PASSWORD` | Passwort, ≥12 Zeichen | leer | initial Passwort oder Hash / ja | API: Startkonto; danach entfernen |
| `INIT_ADMIN_PASSWORD_HASH` | Werkzeug-Passworthash | leer | alternativ / ja | API: Startkonto; Klartext hat Vorrang |
| `SESSION_HOURS` | Stunden; ganze Sekunden, höchstens 31 Tage | `8` | nein / nein | API/Web: absolute Sitzungsfrist |
| `SESSION_COOKIE_NAME` | Cookie-Name, 1–80 Zeichen | `solarbank_session`, bei Secure `__Host-solarbank_session` | nein / nein | Web: je Instanz eigener Namensraum |
| `SESSION_COOKIE_SECURE` | `true`/`false` | lokal `false`, Server `true` | nein / nein | Web: HTTPS-Cookie |
| `CANONICAL_ORIGIN` | HTTP(S)-Origin | Server `https://kraftwerk.fluxo.io` | für Server gesetzt / nein | Web: Host-/Origin-Prüfung |
| `TRUST_PROXY` | `true`/`false` | lokal `false`, Server `true` | nein / nein | Web: ein vertrauenswürdiger Proxy |
| `REVERSE_PROXY_NETWORK` | Docker-Netzname | `reverse-proxy` | Servernetz bereitstellen / nein | Webserver-Overlay |
| `AUTH_ENABLED` | `true`/`false` | `true`, Server erzwingt `true` | nein / nein | Web: Anmeldung |
| `ANKER_USERNAME`, `ANKER_PASSWORD` | Kontodaten | keiner | Collector ja / ja | Collector: Cloud-Anmeldung |
| `ANKER_COUNTRY` | Ländercode | `DE` | nein / nein | Collector |
| `ANKER_DEVICE_SN` | Seriennummer | leer = alle | nein / nein | Collector: Geräteauswahl |
| `ANKER_PAYLOAD_ENCRYPTION` | `true`/`false` | `false` | nein / nein | Collector: Cloud-Nutzlast |
| `POLL_INTERVAL_SECONDS` | ganze Sekunden, 60–86400 | `300` | nein / nein | API/Web/Collector: Messintervall |
| `API_REQUEST_TIMEOUT_SECONDS` | Sekunden | `90` | nein / nein | Collector: Cloud-Timeout |
| `MAX_RETRIES` | ganze Zahl | `2` | nein / nein | Collector: Cloud-Abrufe; keine pauschale Mutation-Wiederholung |
| `LOG_LEVEL` | Log-Level | `INFO` | nein / nein | Collector: Betriebslogs |
| `DASHBOARD_TIMEZONE` | IANA-Zeitzone | `Europe/Berlin` | nein / nein | API: Tagesgrenzen; Web: Darstellung |
| `ANKER_SOURCE_TIMEZONE` | IANA-Zeitzone oder leer | leer | nein / nein | API/Web: Interpretation der Cloud-Messzeit |

Cookies gelten für den Host, unabhängig vom Port. Weitere Instanzen auf derselben
Domain brauchen unterschiedliche Namen. Path bleibt `/`, Domain bleibt unset;
`__Host-`/`__Secure-` benötigen Secure. Die App bietet kein Remember-me an; der
Flask-Login-Cookienamensraum ist vorsorglich ebenfalls instanzbezogen.

Betriebslogs nach stdout/stderr sind von fachlichen Messwerten getrennt. Compose
rotiert sie mit 3 × 10 MiB je Container. Der technische Collectorstatus beschreibt
Abrufe; er ersetzt kein vollständiges Benutzeraudit. Messwerte sind Fachbestand
in SQLite und werden von der API verwaltet. Dieses Projekt führt kein neues
Audit- oder Statistikmodul allein zur Angleichung ein. Tokens, Passwörter und
Cloud-Nutzlasten gehören nicht ins Betriebslog.

Die Sicherung bleibt vorbereitet: Inventar und manuelle Werkzeuge sind vorhanden,
aber kein regelmäßiger Backupjob, Zeitplan oder externes Ziel wird eingerichtet.


Die API begrenzt gleichzeitige Dashboardauswertungen auf eine je Gunicorn-Worker
(zwei Worker mit je vier Threads). Weitere Auswertungen erhalten 503 mit kurzem
`Retry-After`; Health-, Auth- und Collectoraufrufe behalten freie Threads. Web
begrenzt Dashboardantworten separat mit `API_DASHBOARD_TIMEOUT_SECONDS` (20 s),
gewöhnliche API-Aufrufe mit `API_TIMEOUT_SECONDS` (5 s) und Healthabfragen auf
2 s/16 KiB. HTTP-Healthprobe 3 s, Docker-Healthcheck 5 s. 100.000 synthetische
Messwerte ergaben lokal 35,46 MiB Antwort und 6,175 s Auswertung; dies ist ein
Lastnachweis dieser Testumgebung, keine Zusage für die Produktivmaschine.

Diagramm-Schrift und neutrale Diagrammfarben verwenden denselben `tokens.json`-Export wie die Oberfläche. Webpack legt ihn mit Inhalts-Hash im Asset-Manifest ab; das Webimage enthält ihn vollständig und prüft ihn beim Start. Die fachlichen Messreihen behalten ihre eigenen Farben. Login, Fehlerseiten und Dash verwenden dieselben Fluxo-Favicons und Homescreen-Metadaten.

`frontend/src/dash-widgets.css` ordnet die vollständige Dash-4-Farbpalette den Fluxo-Tokens zu, einschließlich portierter Dropdowns und Datumsauswahl. Bei Dash-Upgrades sind geöffnete Widgets sowie Fokus, Auswahl und Paginierung bei 320 Pixeln zu prüfen.


## Gemeinsamer Login und Benutzerspeicher

Web und API installieren dasselbe feste Pythonpaket `fluxo_auth` 1.1.0 aus
`vendor/`; `vendor/SHA256SUMS` prüft das Paket. Benutzer, Sitzungen und
Anmeldeversuche liegen in den gemeinsamen `auth_*`-Tabellen der bestehenden
`data/db/app.sqlite3`. Messwerte und Collectorstatus bleiben in derselben Datei;
der Webdienst erhält keinen Datenbankmount. Es gibt keine zweite Kontenquelle.

Beim ersten Start übernimmt die API bestehende Benutzer und Passwort-Hashes
transaktional. Benutzer erhalten eine stabile interne Kennung; der frühere
Passwort-Fingerprint wird nicht mehr als Sitzung verwendet. Namenskollisionen
nach der gemeinsamen Normalisierung brechen den Import ab. Vor dem produktiven
Upgrade konsistent sichern, API und Web gemeinsam aktualisieren und anschließend
einmal neu anmelden. Bestehende Passwörter und getrennte Dienstschlüssel bleiben
erhalten; Initialwerte überschreiben keine vorhandenen Konten.

Der Login verwendet zufällige serverseitige Sitzungstokens, deren HMAC in SQLite
liegt. Standard sind acht Stunden absolute Laufzeit und keine Remember-Anmeldung.
`SESSION_HOURS` erreicht sowohl API als auch Web; die API-Frist bleibt maßgeblich.
Ein IP-Wechsel allein meldet einen gültigen Benutzer nicht ab. Logout sperrt genau
diese Sitzung; Passwortänderung oder Deaktivierung sperren alle Sitzungen des
Kontos. Während eines API-Ausfalls ist lokales Abmelden weiterhin möglich; die
serverseitige Sperre benötigt die erreichbare API.

Der gemeinsame Schutz erlaubt fünf Anmeldeversuche in fünf Minuten und zwanzig
in einer Stunde pro Konto und Client. Bestehende Sperren bleiben beim Upgrade
wirksam; erfolgreiche Logins löschen frühere Client-Fehlversuche nicht.

Ein Passwort kann ohne neue Verwaltungsoberfläche interaktiv geändert werden:

```sh
docker compose -f compose.yaml -f compose.webserver.yaml exec api python -m solarbank_api.accounts admin
```

Das Passwort wird verdeckt eingegeben und nicht als Shellargument gespeichert.
Die Funktion benutzt denselben gemeinsamen Kontenspeicher und beendet alle
bisherigen Sitzungen. Sie legt eine fehlende Datenbank nicht neu an.

Die Auth-Daten bleiben über den bisherigen Host-Bindmount per SSH/SFTP erreichbar.
Eine konsistente Sicherung der dokumentierten Projektordner einschließlich `.env`,
`data/db`, `data/collector` und möglicher SQLite-Journale enthält sie vollständig.
Es wird kein regelmäßiger Backup-Job eingerichtet.

`SESSION_LIFETIME_MINUTES` bleibt als alternativer Kompatibilitätsname nutzbar,
wenn `SESSION_HOURS` leer ist. Sind beide gesetzt, müssen sie dieselbe Frist
angeben. Eine verkürzte Frist begrenzt bestehende Sitzungen dauerhaft; eine
spätere Verlängerung reaktiviert sie nicht.

### Nutzer, Passwortart und letzte Anmeldung

Über „Konto“ → „Nutzer verwalten“ erreicht das Verwaltungskonto die Nutzerübersicht. Neue Konten erhalten Dashboardzugriff ohne Verwaltungsrecht. Bei Anlage und Reset stehen festes Passwort und Initialpasswort zur Wahl; Initialpasswörter müssen vor Zugriff auf das Dashboard geändert werden. Der Status bleibt in der Übersicht sichtbar. Das eigene Passwort lässt sich unter „Konto“ ändern. Der interaktive API-Befehl `python -m solarbank_api.accounts USERNAME --initial` setzt ein Initialpasswort; ohne `--initial` bleibt es ein festes Passwort.

Die letzte erfolgreiche Passwortanmeldung wird in UTC dauerhaft erfasst und mit Sekunden sowie Zeitzone Europe/Berlin angezeigt. Fehlversuche, Sitzungsprüfungen und Passwortänderungen ändern diesen Zeitpunkt nicht; fehlende historische Angaben erscheinen als „Noch nicht erfasst“. Die einmalige Berechtigungsmigration erhält das konfigurierte Verwaltungskonto beziehungsweise das einzige vorhandene Altkonto; neue gewöhnliche Konten erhalten keine Verwaltungsrechte. Bei mehreren Altkonten muss `INIT_ADMIN_USERNAME` ein vorhandenes Verwaltungskonto benennen; nach Korrektur der Einstellung die API neu starten. Bis dahin bleiben vorhandene Dashboardzugänge erhalten, die Nutzerverwaltung ist noch nicht verfügbar. Das erstmalige Zuweisen des Verwaltungsrechts beendet die bisherigen Sitzungen dieses Kontos; danach ist eine Neuanmeldung nötig. Kontoänderungen prüfen die Berechtigung in derselben SQLite-Transaktion wie die Änderung. Produktive Daten bleiben beim lokalen Test unberührt.
