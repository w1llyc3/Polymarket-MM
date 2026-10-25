import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.archive_storage_20261026 import archive_one, checked_path, idle_lock, verify_archive


class StorageArchiveTests(unittest.TestCase):
    def test_archive_uses_owner_handle_for_locked_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / 'interrupted'; source.mkdir()
            lock = source / 'paper.sqlite.writer.lock'; lock.write_bytes(b'0')
            (source / 'paper.sqlite-wal').write_bytes(b'preserve wal')
            with idle_lock(lock) as fp:
                result = archive_one(root, 'interrupted', root / '01.zip', {lock.resolve(): fp})
            verify_archive(root / '01.zip', result['files'])

    def test_lossless_bundle_keeps_originals_and_wal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / 'interrupted'; source.mkdir()
            payloads = {'paper.sqlite': b'\x00\xff' * 8192, 'paper.sqlite-wal': b'uncheckpointed', 'result.json': b'{"reason":"interrupted"}'}
            for name, payload in payloads.items(): (source / name).write_bytes(payload)
            result = archive_one(root, 'interrupted', root / '01.zip')
            self.assertFalse(result['originals_removed'])
            self.assertEqual(result['source_bytes'], sum(map(len, payloads.values())))
            for name, payload in payloads.items(): self.assertEqual((source / name).read_bytes(), payload)
            verify_archive(root / '01.zip', result['files'])
            with self.assertRaises(FileExistsError): archive_one(root, 'interrupted', root / '01.zip')

    def test_corrupt_archive_not_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / 'source').write_bytes(b'evidence')
            result = archive_one(root, 'source', root / '01.zip')
            with zipfile.ZipFile(root / '01.zip', 'w') as z: z.writestr('source', b'changed!')
            with self.assertRaises(ValueError): verify_archive(root / '01.zip', result['files'])
            self.assertEqual((root / 'source').read_bytes(), b'evidence')

    def test_mutation_during_verification_preserves_original_and_no_final_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / 'source').write_bytes(b'first')
            def changed(*_): (root / 'source').write_bytes(b'changed later')
            with patch('scripts.archive_storage_20261026.verify_archive', side_effect=changed):
                with self.assertRaises(ValueError): archive_one(root, 'source', root / '01.zip')
            self.assertFalse((root / '01.zip').exists())
            self.assertEqual((root / 'source').read_bytes(), b'changed later')

    def test_rejects_root_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'data'; root.mkdir()
            with self.assertRaises(ValueError): checked_path(root, '..')
            with self.assertRaises(ValueError): checked_path(root, '.')
