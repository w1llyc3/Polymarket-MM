"""One-off, lossless archive of approved old logs/interrupted replays. No deletion."""
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import time
import zipfile

TARGETS = [
    'unattended/2026-10-22/discovery.jsonl',
    'unattended/2026-10-23/discovery.jsonl',
    'unattended/2026-10-24/discovery.jsonl',
    'day-studies/2026-10-21/5676016/REDECODED-L0.incomplete-1792596914015170600',
    'day-studies/2026-10-21/5676016/REDECODED-L1.incomplete-1792597090581723400',
    'day-studies/2026-10-21/5676016/REDECODED-L1.incomplete-1792597171506635100',
    'day-studies/2026-10-21/5676021/REDECODED-L0.incomplete-1792597640646027000',
    'day-studies/2026-10-21/5680507/REDECODED-L0.incomplete-1792597710562281000',
]


def checked_path(root, relative):
    path = root / relative
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve())
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or getattr(item.stat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('symlink/junction/reparse point is not an archive candidate')
    if resolved == root.resolve():
        raise ValueError('data root itself is never a candidate')
    return resolved


def file_sha(path):
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


@contextmanager
def idle_lock(path):
    # Acquire the recorder's existing OS lock without changing its bytes.
    fp = path.open('r+b')
    try:
        fp.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fp
    finally:
        fp.close()


def verify_archive(path, files):
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != sorted(f['path'] for f in files):
            raise ValueError('archive member list differs from source')
        for item in files:
            h = hashlib.sha256(); count = 0
            with archive.open(item['path']) as fp:
                for block in iter(lambda: fp.read(1024 * 1024), b''):
                    h.update(block); count += len(block)
            if count != item['bytes'] or h.hexdigest() != item['sha256']:
                raise ValueError('decompressed content differs: ' + item['path'])


def archive_one(root, relative, destination, locked_files=None):
    source = checked_path(root, relative)
    paths = sorted(source.rglob('*')) if source.is_dir() else [source]
    paths = [checked_path(root, p.relative_to(root)) for p in paths if p.is_file()]
    if not paths:
        raise ValueError('empty archive candidate')
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
    temporary = destination.with_suffix('.zip.partial')
    if destination.exists() or temporary.exists():
        raise FileExistsError('archive already exists; never overwrite: ' + str(destination))
    files = []
    with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        for p in paths:
            relative_file = p.relative_to(root).as_posix()
            h = hashlib.sha256(); count = 0
            locked = (locked_files or {}).get(p)
            reader = nullcontext(locked) if locked is not None else p.open('rb')
            with reader as fp, archive.open(relative_file, 'w', force_zip64=True) as output:
                fp.seek(0)
                for block in iter(lambda: fp.read(1024 * 1024), b''):
                    h.update(block); count += len(block); output.write(block)
            if (p.stat().st_size, p.stat().st_mtime_ns) != before[p] or count != before[p][0]:
                raise ValueError('source changed during archive: ' + str(p))
            files.append({'path': relative_file, 'bytes': count, 'mtime_ns': before[p][1], 'sha256': h.hexdigest()})
    verify_archive(temporary, files)
    # Check for added/removed files and mutations during decompression validation.
    after = sorted(p for p in source.rglob('*') if p.is_file()) if source.is_dir() else [source]
    if after != paths or any((p.stat().st_size, p.stat().st_mtime_ns) != before[p] for p in paths):
        raise ValueError('source changed before verification completed')
    temporary.replace(destination)
    return {'source': relative, 'archive': destination.relative_to(root).as_posix(),
            'source_bytes': sum(f['bytes'] for f in files), 'archive_bytes': destination.stat().st_size,
            'archive_sha256': file_sha(destination), 'files': files,
            'verification': 'FULL_DECOMPRESSION_SIZE_AND_SHA256_MATCH', 'originals_removed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(); root = args.root.resolve() / 'data'
    output = root / 'archives/storage-cleanup-20261026'
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'created_ms': int(time.time() * 1000), 'root': str(root),
                'scope': TARGETS, 'deletes_files': False, 'archives': []}
    for index, relative in enumerate(TARGETS, 1):
        source = checked_path(root, relative)
        lock = source.parent / 'scheduler.lock' if relative.endswith('discovery.jsonl') else source / 'paper.sqlite.writer.lock'
        with idle_lock(lock) as lock_fp:
            if relative.endswith('discovery.jsonl'):
                state = json.loads((source.parent / 'state.json').read_text(encoding='utf-8'))
                if state.get('scheduler_status') != 'COMPLETE':
                    raise ValueError('scheduler not complete; leave its log untouched')
            print(json.dumps({'stage': 'ARCHIVING', 'source': relative}), flush=True)
            item = archive_one(root, relative, output / f'{index:02d}.zip', {lock.resolve(): lock_fp})
        manifest['archives'].append(item)
        temp = output / 'manifest.json.tmp'
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(output / 'manifest.json')
        print(json.dumps({k: v for k, v in item.items() if k != 'files'}), flush=True)
    print(json.dumps({'stage': 'ALL_ARCHIVES_VERIFIED', 'manifest': str(output / 'manifest.json')}), flush=True)


if __name__ == '__main__':
    main()
