#!/usr/bin/env python3
"""Protected, offline file snapshots. Never opens a source SQLite database.

Run on the Docker host. Docker writers are controlled; external writers must
also be stopped. No API imports, deployment, scheduling, or in-place restore.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid


FORMAT = "fluxo-file-backup/1"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
CHUNK = 1024 * 1024
_interrupted = None


class BackupError(RuntimeError):
    pass


def check_interrupt():
    if _interrupted is not None:
        raise BackupError("Vorgang unterbrochen; zuvor laufende Dienste werden wieder gestartet.")


@contextmanager
def interrupt_guard():
    global _interrupted
    _interrupted = None
    previous = {}

    def requested(signum, _frame):
        global _interrupted
        _interrupted = signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, requested)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_docker(arguments):
    # Never echo Docker's stdout/stderr: Compose and inspect may contain secrets.
    # A separate process group lets the current Docker stop finish before recovery.
    try:
        result = subprocess.run(["docker", *arguments], capture_output=True,
                                text=True, start_new_session=True, check=False)
    except OSError:
        raise BackupError("Docker konnte nicht ausgeführt werden.") from None
    if result.returncode:
        raise BackupError(f"Docker-Befehl fehlgeschlagen (Exit {result.returncode}); keine Sicherung bestätigt.")
    return result.stdout


def docker_json(arguments):
    try:
        return json.loads(run_docker(arguments))
    except (ValueError, TypeError):
        raise BackupError("Docker lieferte keine gültige strukturierte Antwort.") from None


def all_containers():
    ids = run_docker(["ps", "-aq", "--no-trunc"]).split()
    if not ids:
        return []
    result = docker_json(["inspect", *ids])
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise BackupError("Containerbestand konnte nicht geprüft werden.")
    return result


def active(container):
    state = container.get("State", {})
    return bool(state.get("Running") or state.get("Restarting") or state.get("Paused"))


def labels(container):
    return container.get("Config", {}).get("Labels") or {}


def container_service(container, project):
    values = labels(container)
    if values.get("com.docker.compose.project") != project:
        return None
    if str(values.get("com.docker.compose.oneoff", "false")).lower() == "true":
        return None
    return values.get("com.docker.compose.service")


def relative(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise BackupError("Ungültiger relativer Dateipfad.")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in value.split("/")):
        raise BackupError("Ungültiger relativer Dateipfad.")
    return str(path)


def within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def overlap(left, right):
    return within(left, right) or within(right, left)


def mode_info(info):
    return {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid, "mtime_ns": info.st_mtime_ns}


def signature(info):
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_ino, info.st_dev, info.st_uid, info.st_gid)


def tree(path):
    """Inventory every entry without following links; include the root as ''."""
    result = {}

    def visit(current, key):
        try:
            info = current.lstat()
        except OSError:
            raise BackupError("Eine Quelldatei fehlt oder kann nicht gelesen werden.") from None
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise BackupError("Symlinks und Spezialdateien sind im Sicherungsbestand nicht erlaubt.")
        result[key] = info
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                visit(child, child.name if not key else key + "/" + child.name)
    visit(path, "")
    return result


def same_tree(path, before):
    after = tree(path)
    if set(before) != set(after) or any(signature(before[key]) != signature(after[key]) for key in before):
        raise BackupError("Quelldateien wurden während der Sicherung verändert; Sicherung verworfen.")


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def copy_file(source, destination, expected=None):
    private_dir(destination.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(source, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (expected is not None and signature(info) != signature(expected)):
            raise BackupError("Quelldatei wurde ersetzt oder ist keine reguläre Datei.")
        with os.fdopen(descriptor, "rb", closefd=False) as reader:
            with destination.open("xb") as writer:
                destination.chmod(0o600)
                while True:
                    check_interrupt()
                    block = reader.read(CHUNK)
                    if not block:
                        break
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
        if signature(os.fstat(descriptor)) != signature(info):
            raise BackupError("Quelldatei wurde während des Kopierens verändert.")
    finally:
        os.close(descriptor)


def copy_tree(source, destination, entries):
    for key, info in entries.items():
        check_interrupt()
        original = source if not key else source / key
        target = destination if not key else destination / key
        if stat.S_ISDIR(info.st_mode):
            private_dir(target)
        else:
            copy_file(original, target, info)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            check_interrupt()
            digest.update(block)
    return digest.hexdigest()


def normalize_sqlite(copy_path):
    """Recover/merge journals only in a disposable copy, then publish one DB."""
    if not copy_path.is_file() or copy_path.is_symlink():
        raise BackupError("Eine erwartete SQLite-Datei fehlt.")
    with copy_path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise BackupError("Eine erwartete SQLite-Datei hat kein gültiges SQLite-Format.")
    output = copy_path.with_name(".sqlite-normalize-" + uuid.uuid4().hex)
    sidecars = [copy_path.with_name(copy_path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    included = [item.name for item in sidecars if item.exists()]
    try:
        with closing(sqlite3.connect(copy_path, timeout=5)) as source:
            with closing(sqlite3.connect(output)) as target:
                source.backup(target, pages=256, progress=lambda *_: check_interrupt())
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError("SQLite-Integritätsprüfung fehlgeschlagen.")
                version = target.execute("PRAGMA user_version").fetchone()[0]
        output.chmod(0o600)
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(output, copy_path)
        for item in sidecars:
            item.unlink(missing_ok=True)
        return {"user_version": version, "incorporated_sidecars": included}
    except sqlite3.Error:
        raise BackupError("SQLite-Kopie konnte nicht konsistent geprüft werden.") from None
    finally:
        output.unlink(missing_ok=True)


def read_profile(root):
    try:
        value = json.loads((root / "file-backup.json").read_text())
    except (OSError, ValueError):
        raise BackupError("file-backup.json fehlt oder ist ungültig.") from None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise BackupError("Unbekannte Dateisicherungsversion.")
    sources, order = value.get("sources"), value.get("stopOrder")
    if not isinstance(sources, list) or not isinstance(order, list):
        raise BackupError("sources/stopOrder müssen eindeutige Listen sein.")
    if any(not isinstance(name, str) or not NAME.fullmatch(name) for name in order):
        raise BackupError("Ungültiger Dienstname in stopOrder.")
    if len(set(order)) != len(order):
        raise BackupError("stopOrder enthält doppelte Dienste.")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"name", "service", "target", "sqlite"}:
            raise BackupError("Ungültiger Sicherungsquelleneintrag.")
        for key in ("name", "service"):
            if not isinstance(source[key], str) or not NAME.fullmatch(source[key]):
                raise BackupError("Ungültiger Quellen-/Dienstname.")
        if source["name"] in seen or source["service"] not in order:
            raise BackupError("Quellennamen müssen eindeutig und Dienste in stopOrder enthalten sein.")
        seen.add(source["name"])
        target = source["target"]
        if not isinstance(target, str) or not target.startswith("/") or str(PurePosixPath(target)) != target or ".." in PurePosixPath(target).parts:
            raise BackupError("Mountziel muss ein normalisierter absoluter Containerpfad sein.")
        if not isinstance(source["sqlite"], list):
            raise BackupError("SQLite-Dateien müssen eindeutig angegeben sein.")
        for item in source["sqlite"]:
            relative(item)
        if len(set(source["sqlite"])) != len(source["sqlite"]):
            raise BackupError("SQLite-Dateien müssen eindeutig angegeben sein.")
    return value


def resolve(root, profile_name, env_file, project_name):
    declaration = read_profile(root)
    configuration = [root / "compose.yaml", root / ("compose." + profile_name + ".yaml")]
    environment = Path(env_file)
    if not environment.is_absolute():
        environment = root / environment
    for path in [environment, *configuration]:
        if not path.is_file() or path.is_symlink():
            raise BackupError("Gewählte Konfigurationsdatei fehlt oder ist ein Symlink.")
    command = ["compose", "--project-directory", str(root), "--env-file", str(environment)]
    if project_name:
        command += ["--project-name", project_name]
    for path in configuration:
        command += ["-f", str(path)]
    config = docker_json([*command, "config", "--format", "json"])
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict) or not config.get("name"):
        raise BackupError("Compose-Konfiguration ist unvollständig.")
    services = config["services"]
    mounts = {}
    for service in declaration["stopOrder"]:
        if service not in services or services[service].get("profiles"):
            raise BackupError("stopOrder darf nur reguläre vorhandene Runtime-Dienste enthalten.")
        for mount in services[service].get("volumes", []):
            if mount.get("type") in {"bind", "volume"}:
                mounts[(service, mount["target"])] = mount
    output = []
    covered = set()
    for source in declaration["sources"]:
        key = (source["service"], source["target"])
        if key not in mounts:
            raise BackupError("Eine deklarierte Quelle entspricht keinem persistenten Compose-Mount.")
        if key in covered:
            raise BackupError("Ein Mount darf nur einmal gesichert werden.")
        covered.add(key)
        mount = mounts[key]
        item = dict(source, type=mount["type"])
        if mount["type"] == "bind":
            raw = Path(mount.get("source", ""))
            if not raw.is_absolute() or raw.is_symlink():
                raise BackupError("Hostquelle muss absolut sein und darf kein Symlink sein.")
            item["hostPath"] = str(raw.resolve())
        else:
            logical = mount.get("source")
            item["volumeName"] = config.get("volumes", {}).get(logical, {}).get("name", logical)
        output.append(item)
    if covered != set(mounts):
        raise BackupError("Ein persistenter Runtime-Mount fehlt in file-backup.json; Sicherung unvollständig.")
    return {"project": config["name"], "profile": profile_name, "sources": output,
            "stopOrder": declaration["stopOrder"], "configuration": configuration,
            "environment": environment, "declaration": root / "file-backup.json"}


def writable_sources(container, sources):
    for mount in container.get("Mounts", []):
        if not mount.get("RW") or mount.get("Type") != "bind":
            continue
        host = Path(mount.get("Source", "")).resolve()
        if any(overlap(host, Path(source["hostPath"])) for source in sources):
            return True
    return False


def assert_runtime_mounts(plan, containers=None):
    """Refuse a stale deployment still using a different bind or named volume."""
    if not plan["sources"]:
        return
    for container in all_containers() if containers is None else containers:
        service = container_service(container, plan["project"])
        if service in plan["stopOrder"]:
            declared = {source["target"] for source in plan["sources"] if source["service"] == service}
            actual = {mount.get("Destination") for mount in container.get("Mounts", [])
                      if mount.get("Type") in {"bind", "volume"}}
            if actual != declared:
                raise BackupError("Vorhandener Runtime-Container hat nicht vollständig erfasste oder abweichende Datenmounts.")
        for source in plan["sources"]:
            if source["service"] != service:
                continue
            matches = [mount for mount in container.get("Mounts", [])
                       if mount.get("Destination") == source["target"]]
            if len(matches) != 1 or matches[0].get("Type") != source["type"]:
                raise BackupError("Vorhandener Container nutzt eine andere Datenablage als Compose; zuerst kontrolliert migrieren.")
            mount = matches[0]
            if source["type"] == "bind":
                actual = Path(mount.get("Source", "")).resolve()
                agrees = str(actual) == source["hostPath"]
            else:
                agrees = mount.get("Name") == source.get("volumeName")
            if not agrees:
                raise BackupError("Vorhandener Container und Compose verweisen auf unterschiedliche Quelldateien.")


def assert_no_writers(sources):
    if any(active(container) and writable_sources(container, sources) for container in all_containers()):
        raise BackupError("Ein Container schreibt noch in eine Sicherungsquelle.")


@contextmanager
def stopped_writers(plan, permit_stop):
    if not plan["sources"]:
        yield
        return
    containers = all_containers()
    assert_runtime_mounts(plan, containers)
    order, project = plan["stopOrder"], plan["project"]
    writers = [item for item in containers if active(item) and writable_sources(item, plan["sources"])]
    if any(container_service(item, project) not in order for item in writers):
        raise BackupError("Ein nicht zugeordneter Container verwendet die Quelldateien schreibend; zuerst manuell stoppen.")
    if writers and not permit_stop:
        raise BackupError("Schreibende Dienste laufen; bewusst --stop-services wählen oder alle Schreiber vorher stoppen.")
    previous = [item for item in containers if active(item) and container_service(item, project) in order] if permit_stop else []
    if any(item.get("State", {}).get("Paused") or item.get("State", {}).get("Restarting") for item in previous):
        raise BackupError("Pausierte/neustartende Container vor der Sicherung in einen eindeutigen Zustand bringen.")
    previous.sort(key=lambda item: (order.index(container_service(item, project)), item["Id"]))
    stopping = False
    try:
        check_interrupt()
        stopping = bool(previous)
        for item in previous:
            run_docker(["stop", item["Id"]])
            check_interrupt()
        assert_no_writers(plan["sources"])
        assert_runtime_mounts(plan)
        yield
        assert_no_writers(plan["sources"])
    finally:
        if stopping:
            failures = []
            # Starting existing IDs never recreates containers or starts previously stopped services.
            for item in reversed(previous):
                try:
                    run_docker(["start", item["Id"]])
                except BackupError:
                    failures.append(container_service(item, project))
            if failures:
                raise BackupError("Wiederanlauf fehlgeschlagen für: " + ", ".join(sorted(set(failures))))


def flush_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path, data):
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def build_manifest(stage, metadata, attributes):
    files, directories = [], []
    for key, info in tree(stage).items():
        if not key:
            continue
        record = {"path": key, **attributes.get(key, mode_info(info))}
        if stat.S_ISDIR(info.st_mode):
            directories.append(record)
        else:
            files.append({**record, "size": info.st_size, "sha256": sha256(stage / key)})
    manifest = {"format": FORMAT, **metadata, "files": files, "directories": directories,
                "restoreOwnership": "operator; original uid/gid/mode are metadata only"}
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    write_private(stage / "manifest.json", raw)
    write_private(stage / "COMPLETE", (hashlib.sha256(raw).hexdigest() + "\n").encode())
    flush_directory(stage)
    return manifest


def create_backup(plan, output, permit_stop=False):
    sources = plan["sources"]
    if any(item["type"] != "bind" for item in sources):
        raise BackupError("Benannte Volumes zuerst kontrolliert exportieren und auf Hostablage umstellen.")
    destination = Path(output).absolute().resolve()
    for source in sources:
        if within(destination, Path(source["hostPath"])):
            raise BackupError("Sicherungsziel darf nicht innerhalb einer Quelldatenablage liegen.")
    # Config files are also protected against concurrent edits.
    configs = [(plan["environment"], "configuration/env"),
               *[(path, "configuration/" + path.name) for path in plan["configuration"]],
               (plan["declaration"], "configuration/file-backup.json")]
    private_dir(destination)
    stage = Path(tempfile.mkdtemp(prefix=".file-backup-", dir=destination))
    published = destination / ("backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    try:
        with stopped_writers(plan, permit_stop):
            before = [(Path(item["hostPath"]), tree(Path(item["hostPath"]))) for item in sources]
            config_before = [(path, tree(path)) for path, _ in configs]
            attributes, databases = {}, []
            for source, (path, entries) in zip(sources, before):
                prefix = "sources/" + source["name"]
                for key, info in entries.items():
                    attributes[prefix + ("/" + key if key else "")] = mode_info(info)
                copy_tree(path, stage / prefix, entries)
                for database in source["sqlite"]:
                    full = prefix + "/" + database
                    metadata = normalize_sqlite(stage / full)
                    databases.append({"path": full, **metadata})
            for (path, target), (_, entries) in zip(configs, config_before):
                copy_file(path, stage / target, entries[""])
                attributes[target] = mode_info(entries[""])
            for path, entries in [*before, *config_before]:
                same_tree(path, entries)
            assert_no_writers(sources) if sources else None
            assert_runtime_mounts(plan)
            check_interrupt()
            build_manifest(stage, {"createdAt": datetime.now(timezone.utc).isoformat(),
                                   "project": plan["project"], "profile": plan["profile"],
                                   "sources": sources, "sqlite": databases}, attributes)
            # Validate the completed private copy before making it visible to a file backup job.
            verify_backup(stage)
            for path, entries in [*before, *config_before]:
                same_tree(path, entries)
            assert_no_writers(sources) if sources else None
            assert_runtime_mounts(plan)
            check_interrupt()
            os.rename(stage, published)
            flush_directory(destination)
        return published
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_backup(backup):
    root = Path(backup).absolute()
    entries = tree(root)
    if not stat.S_ISDIR(entries[""].st_mode):
        raise BackupError("Sicherung muss ein Verzeichnis sein.")
    try:
        raw = (root / "manifest.json").read_bytes()
        complete = (root / "COMPLETE").read_text().strip()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raise BackupError("Sicherung ist unvollständig oder ihr Manifest ist ungültig.") from None
    if not isinstance(manifest, dict) or complete != hashlib.sha256(raw).hexdigest() or manifest.get("format") != FORMAT:
        raise BackupError("Abschlussmarker oder Manifestprüfung fehlgeschlagen.")
    if not all(isinstance(manifest.get(key), list) for key in ("files", "directories", "sqlite")):
        raise BackupError("Ungültiges Sicherungsmanifest.")
    recorded_files, recorded_dirs = set(), set()
    for key, items, names in (("files", manifest["files"], recorded_files), ("directories", manifest["directories"], recorded_dirs)):
        for item in items:
            if not isinstance(item, dict):
                raise BackupError("Ungültiger Dateieintrag im Sicherungsmanifest.")
            name = relative(item.get("path"))
            if name in names or name in {"manifest.json", "COMPLETE"}:
                raise BackupError("Doppelter oder reservierter Manifestpfad.")
            names.add(name)
            info = entries.get(name)
            if info is None or (key == "files") != stat.S_ISREG(info.st_mode):
                raise BackupError("Dateibestand stimmt nicht mit dem Manifest überein.")
            if key == "files" and (item.get("size") != info.st_size or item.get("sha256") != sha256(root / name)):
                raise BackupError("Dateigröße oder Prüfsumme stimmt nicht.")
    actual_files = {key for key, info in entries.items() if stat.S_ISREG(info.st_mode)} - {"manifest.json", "COMPLETE"}
    actual_dirs = {key for key, info in entries.items() if key and stat.S_ISDIR(info.st_mode)}
    if actual_files != recorded_files or actual_dirs != recorded_dirs:
        raise BackupError("Nicht erfasste oder fehlende Dateien im Sicherungsordner.")
    with tempfile.TemporaryDirectory(prefix=".file-backup-verify-") as temporary:
        for number, item in enumerate(manifest["sqlite"]):
            if not isinstance(item, dict):
                raise BackupError("Ungültiger SQLite-Eintrag im Sicherungsmanifest.")
            name = relative(item.get("path"))
            if name not in recorded_files:
                raise BackupError("SQLite-Eintrag fehlt im Dateimanifest.")
            duplicate = Path(temporary) / (str(number) + ".sqlite3")
            copy_file(root / name, duplicate, entries[name])
            result = normalize_sqlite(duplicate)
            if result["user_version"] != item.get("user_version"):
                raise BackupError("SQLite-Schemaversion stimmt nicht mit der Sicherung überein.")
    same_tree(root, entries)
    return manifest


def new_target(value):
    path = Path(value).absolute()
    if path.exists() or path.is_symlink():
        raise BackupError("Ziel muss neu sein; vorhandene Verzeichnisse werden nicht überschrieben.")
    if not path.parent.is_dir():
        raise BackupError("Der übergeordnete Zielordner muss bereits existieren.")
    return path.parent.resolve() / path.name


def restore_backup(backup, destination):
    root = Path(backup).absolute().resolve()
    target = new_target(destination)
    if within(target, root):
        raise BackupError("Restoreziel darf nicht innerhalb der Sicherung liegen.")
    verify_backup(root)
    entries = tree(root)
    stage = Path(tempfile.mkdtemp(prefix=".file-restore-", dir=target.parent))
    try:
        copy_tree(root, stage, entries)
        verify_backup(stage)
        same_tree(root, entries)
        check_interrupt()
        if target.exists() or target.is_symlink():
            raise BackupError("Restoreziel wurde inzwischen angelegt.")
        os.rename(stage, target)
        flush_directory(target.parent)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def volume_users_stopped(volume):
    users = [item for item in all_containers() if any(mount.get("Type") == "volume" and
             mount.get("Name") == volume for mount in item.get("Mounts", []))]
    if any(active(item) for item in users):
        raise BackupError("Ein Container verwendet das Quellvolume noch; alle Nutzer müssen gestoppt sein.")


def content_inventory(root):
    entries = tree(root)
    return {name: ("directory",) if stat.S_ISDIR(info.st_mode) else
            ("file", info.st_size, sha256(root / name)) for name, info in entries.items()}


def migrate_volume(container, mount, destination):
    if not NAME.fullmatch(container) or not mount.startswith("/") or str(PurePosixPath(mount)) != mount or ".." in PurePosixPath(mount).parts:
        raise BackupError("Ungültige Containerkennung oder Mountadresse.")
    target = new_target(destination)
    result = docker_json(["inspect", "--type", "container", container])
    if not isinstance(result, list) or len(result) != 1 or active(result[0]):
        raise BackupError("Quellcontainer muss vorhanden und vollständig gestoppt sein.")
    matches = [item for item in result[0].get("Mounts", []) if item.get("Destination") == mount]
    if len(matches) != 1 or matches[0].get("Type") != "volume" or not matches[0].get("Name"):
        raise BackupError("Das angegebene Mountziel ist kein eindeutiges benanntes Volume.")
    volume = matches[0]["Name"]
    host_source = Path(matches[0].get("Source", "/"))
    if within(target, host_source.resolve()):
        raise BackupError("Migrationsziel darf nicht in der Quellablage liegen.")
    volume_users_stopped(volume)
    stage = Path(tempfile.mkdtemp(prefix=".file-migrate-", dir=target.parent))
    check = Path(tempfile.mkdtemp(prefix=".file-migrate-check-", dir=target.parent))
    try:
        source = result[0]["Id"] + ":" + mount.rstrip("/") + "/."
        run_docker(["cp", source, str(stage)])  # no -a and no -L
        first = content_inventory(stage)
        check_interrupt()
        volume_users_stopped(volume)
        # A second independent capture rejects sources changed during export.
        run_docker(["cp", source, str(check)])
        if first != content_inventory(check):
            raise BackupError("Volumeinhalt änderte sich während des Exports; Ziel verworfen.")
        databases = []
        for name, info in tree(check).items():
            if name and stat.S_ISREG(info.st_mode):
                path = check / name
                with path.open("rb") as stream:
                    header = stream.read(16)
                if header == b"SQLite format 3\x00" or path.suffix in {".sqlite", ".sqlite3", ".db"}:
                    databases.append(path)
        for path in databases:
            normalize_sqlite(path)  # validation copy only; exported bytes stay unchanged
        volume_users_stopped(volume)
        check_interrupt()
        for name, info in tree(stage).items():
            path = stage if not name else stage / name
            path.chmod(0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
        if target.exists() or target.is_symlink():
            raise BackupError("Migrationsziel wurde inzwischen angelegt.")
        os.rename(stage, target)
        flush_directory(target.parent)
        return target
    finally:
        for path in (stage, check):
            if path.exists():
                shutil.rmtree(path)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--profile", choices=("local", "webserver"), default="local")
    result.add_argument("--env-file", default=".env")
    result.add_argument("--project-name")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory")
    create = commands.add_parser("create")
    create.add_argument("--output", default="backups")
    create.add_argument("--stop-services", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("destination")
    migration = commands.add_parser("migrate-volume")
    migration.add_argument("--container", required=True)
    migration.add_argument("--mount", required=True)
    migration.add_argument("--destination", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    previous_umask = os.umask(0o077)
    try:
        with interrupt_guard():
            if args.command in {"inventory", "create"}:
                plan = resolve(root, args.profile, args.env_file, args.project_name)
                if args.command == "inventory":
                    assert_runtime_mounts(plan)
                    print(json.dumps({"project": plan["project"], "profile": plan["profile"],
                                      "sources": plan["sources"], "stopOrder": plan["stopOrder"]},
                                     ensure_ascii=False, indent=2))
                else:
                    path = create_backup(plan, args.output, args.stop_services)
                    print("Vollständige Dateisicherung: " + str(path))
            elif args.command == "verify":
                verify_backup(args.backup)
                print("Sicherung, Prüfsummen und SQLite-Integrität geprüft.")
            elif args.command == "restore":
                print("Isolierter Restore: " + str(restore_backup(args.backup, args.destination)))
                print("Dateien gehören dem ausführenden Operator; Runtime-Rechte vor Umschaltung bewusst setzen.")
            else:
                print("Volume unverändert exportiert: " + str(migrate_volume(args.container, args.mount, args.destination)))
                print("Quelle bleibt erhalten; keine automatische Umschaltung. Runtime-UID/GID vor Umschaltung prüfen.")
        return 0
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 130 if _interrupted is not None else 1
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        # Never expose file contents, credentials, raw subprocess output or DB errors.
        print("Dateisicherung fehlgeschlagen (" + type(error).__name__ + "); kein Erfolg bestätigt.", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
