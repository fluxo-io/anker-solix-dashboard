"""Actual project Compose contracts, synthetic ENV/data, no runtime containers."""
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('profile_backup_tool', ROOT / 'scripts/file_backup.py')
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose is needed for configuration validation')
class ProjectBackupProfileTests(unittest.TestCase):
    def test_real_compose_profiles_capture_all_persistent_mounts(self):
        for profile in ('local', 'webserver'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary).resolve()
                for name in ('file-backup.json', 'compose.yaml', 'compose.local.yaml', 'compose.webserver.yaml'):
                    shutil.copyfile(ROOT / name, project / name)
                env = {
                    'SECRET_KEY': 'synthetic-session-key-00000000000000000000',
                    'API_TOKEN': 'synthetic-web-key-00000000000000000000',
                    'COLLECTOR_API_TOKEN': 'synthetic-collector-key-00000000000000000000',
                    'ANKER_USERNAME': 'synthetic@example.invalid',
                    'ANKER_PASSWORD': 'synthetic-password',
                    'CANONICAL_ORIGIN': 'https://example.invalid',
                    'HOST_DATA_DIR': str(project / 'host-data'),
                    'DATA_DIR': str(project / 'host-data'),
                }
                (project / '.env').write_text(''.join(key + '=' + value + '\n' for key, value in env.items()))
                with patch.dict(os.environ, env), patch.object(backup, 'all_containers', return_value=[]):
                    plan = backup.resolve(project, profile, '.env', 'synthetic-file-backup')
                    self.assertTrue(plan['sources'])
                    for source in plan['sources']:
                        data = Path(source['hostPath'])
                        self.assertTrue(backup.within(data, project))
                        data.mkdir(parents=True, exist_ok=True)
                        (data / 'nested').mkdir()
                        (data / 'nested/state.json').write_text('{"synthetic":true}')
                        (data / 'artifact.bin').write_bytes(b'\x00\x01\xff')
                        for db in source['sqlite']:
                            (data / db).parent.mkdir(parents=True, exist_ok=True)
                            with closing(sqlite3.connect(data / db)) as connection:
                                connection.execute('CREATE TABLE example (value TEXT)')
                                connection.execute("INSERT INTO example VALUES ('synthetic')")
                                connection.commit()
                    snapshot = backup.create_backup(plan, project / 'backups')
                    manifest = backup.verify_backup(snapshot)
                    self.assertEqual(len(plan['sources']), len(manifest['sources']))
                    restored = backup.restore_backup(snapshot, project / 'restored')
                    for source in plan['sources']:
                        self.assertEqual(b'\x00\x01\xff', (restored / 'sources' / source['name'] / 'artifact.bin').read_bytes())


if __name__ == '__main__':
    unittest.main()
