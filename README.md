# fluxo.io-Demoprojekt: Private Solardatenbank

**Fluxo-Demoprojekt** von **Thomas Walawgo** zur Veranschaulichung einer
selbst gehosteten Solardaten-Architektur. Die dockerisierte Instanz läuft selbst auf
einem sehr kleinen Server. Die Demo verarbeitet private Anker-SOLIX-Daten.

## Stack

- Python-Collector: Anker-Cloud alle 5 Minuten
- PostgreSQL: Messwerte und Rohdaten
- Plotly Dash: Diagramme, Filter, CSV und optionaler Login

## Start

```bash
cp .env.example .env
```

In `.env` mindestens setzen:

```dotenv
ANKER_USERNAME=
ANKER_PASSWORD=
POSTGRES_PASSWORD=
DASHBOARD_DB_PASSWORD=
```

```bash
docker compose up -d --build
docker compose ps
```

Dashboard: [http://127.0.0.1:8501](http://127.0.0.1:8501)

## Betrieb

```bash
docker compose logs --tail=100 collector
docker compose down
```

`.env` und Anker-Token enthalten private Daten und gehören nicht ins Repository.

Die Anker-API ist inoffiziell und kann sich ändern.
