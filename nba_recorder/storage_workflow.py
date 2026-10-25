"""Lossless, in-place cold storage for completed research (Windows NTFS).

No deletion, SQLite rewriting, live collection, or order submission. The OS
decompresses reads transparently, so existing charts and audit paths still work.
"""
import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as fp:
        for block in iter(lambda: fp.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def allocated_bytes(path):
    if os.name != 'nt':
        return Path(path).stat().st_blocks * 512
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetCompressedFileSizeW.restype = ctypes.c_ulong
    high = ctypes.c_ulong()
    ctypes.set_last_error(0)
    low = kernel.GetCompressedFileSizeW(str(Path(path).resolve()), ctypes.byref(high))
    if low == 0xffffffff and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    return (high.value << 32) | low


def checked(root, path):
    root, path = Path(root).resolve(), Path(path).absolute()
    path.resolve().relative_to(root / 'data')
    for p in (path, *path.parents):
        if p == root:
            break
        s = p.lstat()
        # WOF compression itself is a reparse point, but is not a path redirect.
        if p.is_symlink() or (getattr(s, 'st_file_attributes', 0) & 0x400 and
                             getattr(s, 'st_reparse_tag', 0) != 0x80000017):
            raise ValueError('Redirected path is not a storage candidate')
    return path


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = path.open('a+b')
    try:
        fp.seek(0, 2)
        if fp.tell() == 0:
            fp.write(b'0'); fp.flush()
        fp.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fp.close()


def eligible(root, path):
    p = checked(root, path)
    rel = p.relative_to(Path(root).resolve() / 'data')
    if not p.is_file() or any('.incomplete-' in part for part in rel.parts):
        raise ValueError('Not a completed file')
    if rel.parts[0] in ('research', 'day-studies', 'strategy-studies'):
        if p.name != 'paper.sqlite' and not (rel.parts[0] == 'research' and p.suffix == '.jsonl'):
            raise ValueError('Preserve shared inputs, reports and charts')
        result = json.loads((p.parent / 'result.json').read_text(encoding='utf-8'))
        if result.get('shutdown_ledger_consistent') is not True or result.get('real_submission_calls') != 0:
            raise ValueError('Missing completed, consistent Paper evidence')
        if any(Path(str(p.parent / 'paper.sqlite') + suffix).exists() and
               Path(str(p.parent / 'paper.sqlite') + suffix).stat().st_size for suffix in ('-wal', '-journal')):
            raise ValueError('Uncheckpointed database; leave intact')
        return p.parent / 'paper.sqlite.writer.lock'
    if len(rel.parts) == 3 and rel.parts[0] == 'unattended' and p.name == 'discovery.jsonl':
        # The V2 logger never appends to discovery.jsonl. Its durable cursor must
        # postdate the retired log. Otherwise require an idle scheduler lock.
        cursor = p.parent / 'discovery-log-state.json'
        summary = p.parent / 'discovery-summary.jsonl'
        if cursor.exists() and summary.exists() and cursor.stat().st_mtime_ns >= p.stat().st_mtime_ns:
            return p.parent / 'retired-log-compression.lock'
        return p.parent / 'scheduler.lock'
    raise ValueError('Outside approved cold-storage scope')


def plan(root, scope=None):
    root = Path(root).resolve()
    scope = Path(scope).resolve() if scope else root / 'data'
    scope.relative_to(root / 'data')
    candidates = []
    for folder in ('research', 'day-studies', 'strategy-studies'):
        base = root / 'data' / folder
        candidates.extend(base.rglob('paper.sqlite'))
        if folder == 'research':
            candidates.extend(base.rglob('*.jsonl'))
    candidates.extend((root / 'data/unattended').glob('*/discovery.jsonl'))
    items, skipped = [], []
    for p in sorted(set(candidates)):
        if scope != p and scope not in p.parents:
            continue
        try:
            eligible(root, p)
            items.append({'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size})
        except (ValueError, OSError, KeyError) as e:
            skipped.append({'path': str(p.relative_to(root)), 'reason': str(e)})
    return {'items': items, 'skipped': skipped, 'logical_bytes': sum(i['bytes'] for i in items)}


def compress_one(root, item, algorithm='XPRESS8K'):
    if os.name != 'nt':
        raise RuntimeError('Transparent compression requires Windows NTFS; nothing modified')
    if algorithm not in ('XPRESS8K', 'LZX'):
        raise ValueError('Unsupported algorithm')
    root = Path(root).resolve(); p = checked(root, root / item['path'])
    with lock(eligible(root, p)):
        eligible(root, p)
        before = (p.stat().st_size, p.stat().st_mtime_ns)
        digest = sha256(p); physical_before = allocated_bytes(p)
        command = [os.path.join(os.environ['SystemRoot'], 'System32', 'compact.exe'),
                   '/C', '/A', '/F', '/EXE:' + algorithm, str(p)]
        completed = subprocess.run(command, capture_output=True, text=True, errors='replace')
        after_digest = sha256(p)
        if before != (p.stat().st_size, p.stat().st_mtime_ns) or digest != after_digest:
            raise RuntimeError('Content changed during compression; retained in place, investigate')
        if completed.returncode:
            raise RuntimeError('compact failed: ' + completed.stdout + completed.stderr)
        return {**item, 'sha256': digest, 'allocated_before': physical_before,
                'allocated_after': allocated_bytes(p), 'algorithm': algorithm,
                'verified_ms': int(time.time() * 1000), 'status': 'VERIFIED',
                'content_deleted': False}


def apply(root, scope=None, algorithm='XPRESS8K'):
    root = Path(root).resolve(); directory = root / 'data/storage-workflow'
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / (str(int(time.time())) + '-' + uuid.uuid4().hex[:8] + '.json')
    with lock(directory / 'maintenance.lock'):
        report = {'mode': 'LOSSLESS_TRANSPARENT_COMPRESSION', 'plan': plan(root, scope),
                  'results': [], 'errors': [], 'real_submission_calls': 0}
        def save():
            temporary = report_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(report_path)
        save()
        for item in report['plan']['items']:
            print('COMPRESS ' + item['path'], flush=True)
            try:
                result = compress_one(root, item, algorithm)
                report['results'].append(result)
                print(json.dumps(result), flush=True)
            except Exception as e:
                report['errors'].append({'path': item['path'], 'error': str(e)})
                print('FAILED ' + item['path'] + ': ' + str(e), flush=True)
            save()
        report['saved_bytes'] = sum(r['allocated_before'] - r['allocated_after'] for r in report['results'])
        report['completed_ms'] = int(time.time() * 1000); save()
    return report_path, report


def after_research(root, output):
    """Run only after result publication and all model writers are closed."""
    try:
        Path(output).resolve().relative_to(Path(root).resolve() / 'data')
    except ValueError:
        # External exports and temporary test workspaces are never managed.
        return
    policy = Path(root) / 'data/storage-policy.json'
    if not policy.exists() or not json.loads(policy.read_text(encoding='utf-8')).get('compress_completed_research'):
        return
    try:
        path, result = apply(root, output)
        if result['errors']:
            print('STORAGE_MAINTENANCE_FAILED; research retained: ' + str(path), flush=True)
    except Exception as e:
        # A storage failure must not erase or relabel completed research results.
        target = Path(output) / 'storage-maintenance-error.json'
        target.write_text(json.dumps({'error': str(e), 'time_ms': int(time.time()*1000)}), encoding='utf-8')
        print('STORAGE_MAINTENANCE_FAILED: ' + str(e), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('plan', 'apply'))
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--scope', type=Path)
    p.add_argument('--algorithm', choices=('XPRESS8K', 'LZX'), default='XPRESS8K')
    a = p.parse_args()
    if a.action == 'plan':
        print(json.dumps(plan(a.root, a.scope), ensure_ascii=False, indent=2))
    else:
        path, report = apply(a.root, a.scope, a.algorithm)
        print('REPORT ' + str(path))
        if report['errors']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
