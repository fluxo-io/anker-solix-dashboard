#!/usr/bin/env python3
"""Validate the declared Web/API contract; optional resolved Compose inspection.

No application imports, environment-file reads, services or migrations. This is
a declaration/configuration check, not a substitute for integration tests.
"""
import argparse
import json
from pathlib import Path
import sys

STANDARD = 'fluxo-webapp/1.0'


def validate(profile, compose=None):
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    if not isinstance(profile, dict):
        return ['Projektprofil muss ein JSON-Objekt sein.']
    require(profile.get('standard') == STANDARD, 'Unbekannte Standardversion.')
    require(profile.get('architecture') == 'web-api-data', 'Architektur muss web-api-data sein.')
    api = profile.get('api', {})
    if not isinstance(api, dict):
        api = {}
    provisioning = api.get('provisioning')
    require(provisioning in {'managed', 'existing'}, 'API-Bereitstellung muss managed oder existing sein.')
    require(profile.get('interface') in {'jinja', 'dash'}, 'Oberfläche muss jinja oder dash sein.')
    require(profile.get('theme') in {'fluxo', 'bnho', 'lumi'}, 'Theme muss als unterstütztes Paket deklariert sein.')
    require(type(profile.get('backgroundWorker')) is bool, 'backgroundWorker muss ein Boolean sein.')
    require(profile.get('notifications') == 'fluxo-notifications/1.0', 'Gemeinsamer Meldungsvertrag fehlt.')
    data = profile.get('data', {})
    if not isinstance(data, dict):
        data = {}
    require(data.get('businessOwner') == 'api', 'Nur die API darf Fachdaten besitzen.')
    require(data.get('businessStorage') in {'sqlite', 'json', 'external', 'postgresql', 'mariadb'}, 'Speicherart fehlt oder ist unbekannt.')
    if provisioning == 'existing':
        require(data.get('businessStorage') == 'external', 'Vorhandene API verantwortet die externe Fachdatenhaltung.')
    elif provisioning == 'managed':
        require(data.get('businessStorage') != 'external', 'Eigene API benötigt eine konkret benannte Speicherart.')
    stores = data.get('technicalStorage')
    require(isinstance(stores, list), 'technicalStorage muss eine Liste sein.')
    for store in stores if isinstance(stores, list) else []:
        if not isinstance(store, dict):
            errors.append('Technischer Speicher benötigt Besitzer, Zweck und Speicherart.')
            continue
        require(store.get('owner') in {'web', 'api', 'collector', 'worker'}, 'Unbekannter Besitzer des technischen Speichers.')
        require(store.get('purpose') in {'session', 'app-state', 'collector-state', 'worker-state'}, 'Unbekannter technischer Speicherzweck.')
        require(store.get('storage') in {'sqlite', 'json', 'memory', 'cookie', 'files'}, 'Unbekannte technische Speicherart.')
        if store.get('owner') == 'web':
            require(store.get('purpose') == 'session', 'Webspeicher ist auf technische Sitzungen begrenzt.')
    exceptions = profile.get('exceptions')
    require(isinstance(exceptions, list), 'exceptions muss eine Liste sein.')
    for exception in exceptions if isinstance(exceptions, list) else []:
        if not isinstance(exception, dict):
            errors.append('Technische Ausnahme muss ein Objekt sein.')
            continue
        require(all(isinstance(exception.get(key), str) and exception[key].strip()
                    for key in ('rule', 'reason', 'scope', 'verification')), 'Ausnahme benötigt Regel, Grund, Umfang und Nachweis.')
        require(exception.get('rule') not in {'ARCH-01', 'API-01'}, 'Eine Ausnahme darf die API-Architektur nicht aufheben.')
    if compose is not None:
        services = compose.get('services', {}) if isinstance(compose, dict) else {}
        require(bool(services), 'Aufgelöste Compose-Dienste fehlen.')
        name = api.get('service', 'api')
        if provisioning == 'managed':
            require(name in services, 'Eigener API-Dienst fehlt in Compose.')
            service = services.get(name, {})
            require(not service.get('ports'), 'Interne API darf keinen Hostport veröffentlichen.')
            require(not service.get('healthcheck', {}).get('disable', False), 'API-Bereitschaftsprüfung darf nicht deaktiviert sein.')
            consumers = [value for key, value in services.items() if key != name and name in value.get('depends_on', {})]
            require(bool(consumers), 'Kein abhängiger Web-/Worker-Dienst für die eigene API gefunden.')
            api_sources = {str(mount.get('source')) for mount in service.get('volumes', [])
                           if isinstance(mount, dict) and mount.get('source')}
            for key, consumer in services.items():
                if key == name or consumer.get('profiles'):
                    continue
                other_sources = {str(mount.get('source')) for mount in consumer.get('volumes', [])
                                 if isinstance(mount, dict) and mount.get('source')}
                require(not api_sources.intersection(other_sources), f'{key} teilt einen API-Mount; Datenbesitz prüfen.')
        for key, service in services.items():
            for port in service.get('ports', []):
                require(isinstance(port, dict) and port.get('host_ip') in {'127.0.0.1', '::1'}, f'{key} veröffentlicht einen nicht lokalen Hostport.')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', nargs='?', type=Path, default=Path('webapp-profile.json'))
    parser.add_argument('--compose-json', type=Path, help='Bereits mit synthetischen Werten aufgelöstes Compose-JSON; keine private .env verwenden.')
    args = parser.parse_args()
    try:
        profile = json.loads(args.profile.read_text())
        compose = json.loads(args.compose_json.read_text()) if args.compose_json else None
        errors = validate(profile, compose)
    except (OSError, ValueError, TypeError) as exc:
        print(f'Profilprüfung nicht möglich: {type(exc).__name__}', file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print('Projektdeklaration geprüft. API-, UI-, Daten- und Integrationstests sind zusätzlich erforderlich.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
