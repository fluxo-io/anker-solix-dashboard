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
compose.override.yaml    Webserver-Standard (Link)
.env.example             Konfigurationsvorlage
data/db/app.sqlite3      Messwerte, Benutzer und Anmeldeversuche
data/collector/authcache/ Anker-Anmeldecache
```

Dieser Aufbau, die Dienste `web`/`api`, die gemeinsamen ENV-Namen und die
Docker-Konfiguration entsprechen `www.fluxo.io`; hier kommt der Collector hinzu.
Die API ist nur im internen Compose-Netz erreichbar. Web und Collector greifen
über `API_URL=http://api:8001` mit `API_TOKEN` auf sie zu. Nur die API öffnet SQLite.

`data/` wird vom Docker-Host direkt eingebunden. Host und Container arbeiten auf
denselben Dateien, Änderungen bleiben auch nach Neubau und Neustart erhalten.
Es gibt keinen Kopier-, Import- oder Exportablauf. Ein leerer Datenordner wird
beim ersten Start initialisiert; neue Messwerte kommen ab dann vom Collector.
Alte PostgreSQL-Volumes und vorhandene Sicherungen werden nicht verändert.

## Einrichtung

```bash
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
```

Die beiden getrennten Zufallswerte als `SECRET_KEY` und `API_TOKEN` eintragen.
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
docker compose config --quiet
docker compose up --build -d
```

Dashboard: <https://kraftwerk.fluxo.io>. `compose.override.yaml` lädt automatisch
die Webserver-Konfiguration.

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

## Prüfen und betreiben

```bash
docker compose -f compose.yaml --profile test build web-tests api-tests collector-tests
docker compose -f compose.yaml --profile test run --rm --no-deps web-tests
docker compose -f compose.yaml --profile test run --rm --no-deps api-tests
docker compose -f compose.yaml --profile test run --rm --no-deps collector-tests

docker compose ps
docker compose logs --tail=100 collector
docker compose down
```

Tests verwenden synthetische Daten ohne Cloud-Zugriff. `/healthz` prüft Web/API;
der Collector-Healthcheck prüft seine laufende Ereignisschleife. Die Frische der
Messwerte wird separat im Dashboard angezeigt. Die inoffizielle Anker-API kann
sich ändern.
