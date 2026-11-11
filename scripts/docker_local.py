#!/usr/bin/env python3
"""Prepare ./data and invoke the complete local Compose configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys


def capture(command, env, cwd):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        # Compose errors may include operator configuration; do not echo its output.
        raise RuntimeError('Docker-Konfiguration nicht verfügbar. .env und Docker prüfen.')
    return result.stdout


def validate_mounts(mounts, data, compatible=()):
    for mount in mounts:
        target = mount.get('target', mount.get('Destination', ''))
        if target != '/data' and not target.startswith('/data/'):
            continue
        kind = mount.get('type', mount.get('Type'))
        source = mount.get('source', mount.get('Source', ''))
        expected = data / target.removeprefix('/data').lstrip('/')
        mapping = (str(Path(source).resolve()), target)
        if kind != 'bind' or (Path(source).resolve() != expected.resolve() and mapping not in compatible):
            raise RuntimeError('Bisherige Datenablage liegt außerhalb von ./data. Bestand zuerst sichern und kontrolliert übernehmen; alten Container danach gezielt entfernen.')


def main():
    project = Path(__file__).resolve().parents[1]
    data = project / 'data'
    if data.is_symlink() or (data.exists() and not data.is_dir()):
        raise RuntimeError('./data muss ein echtes Projektverzeichnis sein.')
    env = os.environ.copy()
    if os.getuid() == 0:
        raise RuntimeError('Lokalen Start als normaler Benutzer ausführen.')
    # Values are process-local; neither .env nor operator settings are rewritten.
    env.update(FLUXO_DATA_UID=str(os.getuid()), FLUXO_DATA_GID=str(os.getgid()))
    args = sys.argv[1:] or ['up', '-d', '--build', '--wait']
    # Keep project selection identical for config/inspection/start. Profiles and
    # ordinary subcommand options are passed unchanged to Compose.
    prefix = []
    while args and args[0] in ('-p', '--project-name', '--env-file', '--profile'):
        if len(args) < 2:
            raise RuntimeError('Wert für Compose-Option fehlt.')
        prefix += args[:2]
        args = args[2:]
    if not args:
        raise RuntimeError('Compose-Befehl fehlt.')
    if args[0].startswith('-'):
        raise RuntimeError('Unterstützte globale Optionen: --project-name, --env-file, --profile.')
    base = ['docker', 'compose', *prefix, '-f', 'compose.yaml']
    config = json.loads(capture([*base, 'config', '--format', 'json'], env, project))
    needs_data = any(mount.get('target') == '/data' for service in config['services'].values() for mount in service.get('volumes', []))
    for service in config['services'].values():
        validate_mounts(service.get('volumes', []), data)
    marker = project / 'docker-local.json'
    policy = json.loads(marker.read_text()) if marker.exists() else {}
    if marker.exists():
        for relative in policy.get('legacyPaths', []):
            old = project / relative
            if old.is_dir() and any(old.iterdir()):
                raise RuntimeError(f'Bisherige Ablage {relative} zuerst kontrolliert nach ./data übernehmen und außerhalb des Projektordners archivieren.')
    ids = capture(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + config['name']], env, project).split()
    if ids:
        for container in json.loads(capture(['docker', 'inspect', *ids], env, project)):
            service = container.get('Config', {}).get('Labels', {}).get('com.docker.compose.service')
            compatible = {(str((project / row['source']).resolve()), row['target'])
                          for row in policy.get('legacyMounts', []) if row['service'] == service}
            validate_mounts(container.get('Mounts', []), data, compatible)
    if needs_data:
        data.mkdir(mode=0o700, exist_ok=True)
    command = [*base, '-f', 'compose.local.yaml', *args]
    return subprocess.call(command, cwd=project, env=env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        raise SystemExit(str(error))
