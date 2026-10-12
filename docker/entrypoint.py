"""Prepare a bind mount, then run the service without root privileges."""
import os
from pathlib import Path
import sys


def main():
    os.umask(0o077)
    if os.getuid() == 0:
        root = Path('/data')
        root.mkdir(parents=True, exist_ok=True)
        # Only this service's explicitly mounted data directory is touched.
        for directory, folders, files in os.walk(root, followlinks=False):
            for path in [Path(directory), *(Path(directory) / name for name in folders + files)]:
                if path.is_symlink():
                    raise RuntimeError('Symlinks im Datenordner sind nicht erlaubt.')
                os.chown(path, 1000, 1000, follow_symlinks=False)
        os.setgroups([])
        os.setgid(1000)
        os.setuid(1000)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
