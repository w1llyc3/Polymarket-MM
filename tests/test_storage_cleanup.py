"""Exercise the deletion script against isolated fixtures, never project data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.archive_storage_20261026 import TARGETS, archive_one


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell cleanup integration')
class StorageCleanupTests(unittest.TestCase):
    def prepare(self, folder):
        root = Path(folder); data = root / 'data'; scripts = root / 'scripts'
        scripts.mkdir(); output = data / 'archives/storage-cleanup-20261026'
        output.mkdir(parents=True)
        script = scripts / 'remove_verified_storage_20261026.ps1'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts' / script.name, script)
        entries = []
        for i, relative in enumerate(TARGETS, 1):
            p = data / relative
            if relative.endswith('discovery.jsonl'):
                p.parent.mkdir(parents=True)
                p.write_bytes(b'{"test":"fixture"}\n')
                (p.parent / 'scheduler.lock').write_bytes(b'0')
                (p.parent / 'state.json').write_text('{"scheduler_status":"COMPLETE"}')
            else:
                p.mkdir(parents=True)
                (p / 'paper.sqlite').write_bytes(b'isolated-test-database')
                (p / 'paper.sqlite-wal').write_bytes(b'isolated-test-wal')
                (p / 'paper.sqlite.writer.lock').write_bytes(b'0')
            entries.append(archive_one(data, relative, output / f'{i:02d}.zip'))
        manifest = output / 'manifest.json'
        manifest.write_text(json.dumps({'root': str(data), 'archives': entries}), encoding='utf-8')
        return data, script, manifest

    def invoke(self, script):
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(script), '-Execute'], capture_output=True, timeout=30)

    def test_verified_sources_removed_and_all_archives_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            data, script, manifest = self.prepare(folder)
            result = self.invoke(script)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            for relative in TARGETS: self.assertFalse((data / relative).exists())
            entries = json.loads(manifest.read_text(encoding='utf-8-sig'))['archives']
            self.assertTrue(all(e['originals_removed'] and (data / e['archive']).exists() for e in entries))
            self.assertTrue((data / 'unattended/2026-10-24/state.json').exists())

    def test_changed_source_blocks_all_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            data, script, _ = self.prepare(folder)
            (data / TARGETS[-1] / 'paper.sqlite').write_bytes(b'changed since archive')
            result = self.invoke(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Original changed after archive verification', result.stderr)
            self.assertTrue(all((data / relative).exists() for relative in TARGETS))

    def test_changed_archive_blocks_all_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            data, script, manifest = self.prepare(folder)
            (manifest.parent / '01.zip').write_bytes(b'corrupt')
            result = self.invoke(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Archive hash changed', result.stderr)
            self.assertTrue(all((data / relative).exists() for relative in TARGETS))
