"""Prepare the mounted data directory, then replace this process as a non-root user."""
import os
from pathlib import Path
import stat
import sys


def prepare(root=Path('/data')):
    os.umask(0o077)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('Datenordner fehlt oder ist ein Symlink.')
    for relative in filter(None, os.environ.get('DATA_SUBDIRS', '').split(',')):
        target = root / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or target.is_symlink():
            raise RuntimeError('Ungültiger Datenunterordner.')
        # Reject symlink parents before mkdir to avoid writes outside the mount.
        if any(parent.is_symlink() for parent in (target, *target.parents) if parent != root.parent):
            raise RuntimeError('Datenunterordner enthält einen Symlink.')
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = root.stat()
    uid = int(os.environ.get('DATA_UID') or owner.st_uid or 10001)
    gid = int(os.environ.get('DATA_GID') or (owner.st_gid if owner.st_uid else 10001))
    if uid <= 0 or gid < 0:
        raise RuntimeError('Der Anwendungsprozess benötigt eine UID größer null.')
    if os.getuid() == 0:
        # Validate the entire tree before changing permissions. Never follow links.
        paths = [root]
        for directory, folders, files in os.walk(root, followlinks=False):
            paths.extend(Path(directory) / name for name in folders + files)
        for path in paths:
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RuntimeError('Datenordner enthält einen Symlink oder eine Spezialdatei.')
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise RuntimeError('Datenordner enthält eine Datei mit mehreren Hardlinks.')
        for path in paths:
            info = path.lstat()
            required = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
            if info.st_mode & required != required:
                path.chmod(stat.S_IMODE(info.st_mode) | required)
            if (info.st_uid, info.st_gid) != (uid, gid):
                os.chown(path, uid, gid, follow_symlinks=False)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    if os.getuid() == 0 or not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError('Datenordner ist für den Anwendungsprozess nicht beschreibbar.')


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Startbefehl fehlt.')
    prepare()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
