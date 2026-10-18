"""Bounded local NBA public-data/Paper scheduler. No authenticated exchange client."""
import argparse
import asyncio
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import PublicAPI, utc_ms
from .discovery import discover
from .inplay.runner import run_paper

CST = timezone(timedelta(hours=8))


def bounds(day):
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=CST)
    return int(start.timestamp()*1000), int((start+timedelta(days=1)).timestamp()*1000)


def eligible(m):
    return bool(m.get('venue') == 'polymarket.com' and m.get('collection_allowed')
                and not m.get('identity_conflict') and len(m.get('outcomes', [])) == 2)


def paper_allowed(m):
    return bool(eligible(m) and m.get('research_eligible') and m.get('rules_verified')
                and m.get('season_type') in {'PRESEASON', 'REGULAR_SEASON', 'PLAY_IN', 'PLAYOFFS'})


def due(m, now, start, end):
    tip = m.get('game_start_ms')
    return eligible(m) and type(tip) is int and start <= tip < end and tip-3600000 <= now < tip+7*3600000


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


class Control:
    paper_budget = '25'
    paper_plan = 'SMALL'
    research_overrides = {}

    def __init__(self, market):
        self.mode = 'REAL_PAPER' if paper_allowed(market) else 'OBSERVATION'
        self.runner = None

    def attach(self, runner):
        self.runner = runner
        with runner.om.db:
            runner.om.db.execute("INSERT OR IGNORE INTO ip_session_config VALUES('console_origin',?)",
                (json.dumps({'data_source': 'REAL_OBSERVED', 'execution_source': 'PAPER_EXECUTION' if self.mode == 'REAL_PAPER' else 'OBSERVATION_ONLY'}),))
            runner.om._log('UNATTENDED_SESSION', detail={'virtual_budget': self.paper_budget, 'mode': self.mode,
                'manual_phase': False, 'restart_never_auto_resumes': True})

    async def pump(self):
        pass

    def begin_stop(self):
        pass

    def finished(self, result):
        with self.runner.om.db:
            self.runner.om._log('CONSOLE_SESSION_FINISHED', detail={
                'coverage': result.get('recording_coverage'), 'source': 'REAL_OBSERVED',
                'watchdog_ticks': self.runner.ticks})


async def run(day, root, scan_s=120, once=False):
    start, end = bounds(day)
    directory = root/'data'/'unattended'/day
    directory.mkdir(parents=True, exist_ok=True)
    # OS lock survives neither a process crash nor reboot; the file is not a lease.
    lock = open(directory/'scheduler.lock', 'a+b')
    if os.name == 'nt':
        import msvcrt
        lock.write(b'0'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = directory/'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
        'day': day, 'timezone': 'Asia/Shanghai', 'games': {}, 'mode': 'PAPER_ONLY',
        'per_game_virtual_budget': '25', 'accounts_independent': True, 'real_submission_calls': 0}
    state['scheduler_status'] = 'PREFLIGHT_RUNNING' if once else 'RUNNING'
    state.pop('stopped_ms', None)
    active = {}
    api = PublicAPI()

    async def game(m, entry):
        try:
            tip = m['game_start_ms']
            remaining = max(60, min(28800, math.ceil((tip+7*3600000-utc_ms())/1000)))
            authorized = {**m, 'research_authorized': paper_allowed(m)}
            control = Control(m)
            db = root/'data'/'console'/(entry['session_id']+'.sqlite')
            db.parent.mkdir(parents=True, exist_ok=True)
            result = await run_paper(db, db.with_name(db.stem+'-source.sqlite'), m['slug'], m['market_id'],
                seconds=remaining, control=control, research=True, postgame_s=3600, initial_market=authorized)
            save(directory/(m['market_id']+'-report.json'), result)
            entry.update(status='FINISHED', stop_ms=utc_ms(), coverage=result.get('recording_coverage'),
                         ledger_consistent=result.get('ledger_and_oms_consistent'))
        except Exception as error:
            # No automatic retry/re-funding/reset after an execution fault.
            entry.update(status='FAILED_NEEDS_REVIEW', error=type(error).__name__, stop_ms=utc_ms())
        finally:
            save(state_path, state)

    try:
        while utc_ms() < end+7*3600000:
            now = utc_ms()
            try:
                scan = await discover(api, start, end-1)
                with (directory/'discovery.jsonl').open('a', encoding='utf-8') as fp:
                    fp.write(json.dumps(scan, ensure_ascii=False)+'\n')
                state.update(last_scan_ms=utc_ms(), discovery_status=scan['status'],
                             schedule_only=scan['schedule_only'], scan_error=None)
                for m in scan['markets']:
                    key = m['market_id']
                    if key not in state['games']:
                        state['games'][key] = {'market': m, 'session_id': uuid.uuid4().hex, 'status': 'SCHEDULED'}
                    entry = state['games'][key]
                    if key in active or entry['status'] not in {'SCHEDULED', 'RUNNING'}:
                        continue
                    if not once and due(m, now, start, end):
                        entry.update(status='RUNNING', started_ms=now, mode='REAL_PAPER' if paper_allowed(m) else 'OBSERVATION',
                                     late_start_ms=max(0, now-(m['game_start_ms']-3600000)))
                        # Persist intent before starting; restart reuses same account, never resets risk.
                        save(state_path, state)
                        active[key] = asyncio.create_task(game(m, entry))
                    elif now >= m['game_start_ms']+7*3600000:
                        entry['status'] = 'MISSED_WINDOW_NOT_COMPLETE'
            except Exception as error:
                state.update(scan_error=type(error).__name__, last_scan_failed_ms=utc_ms())
            state.update(heartbeat_ms=utc_ms(), active_games=sum(not t.done() for t in active.values()),
                         disk_free_bytes=__import__('shutil').disk_usage(root).free)
            save(state_path, state)
            if once:
                break
            await asyncio.sleep(scan_s)
        await asyncio.gather(*active.values())
        state.update(scheduler_status='COMPLETE' if not once else 'PREFLIGHT_ONLY', stopped_ms=utc_ms())
        save(state_path, state)
    finally:
        for task in active.values():
            if not task.done(): task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
        await api.close()
        lock.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date', required=True)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument('--once', action='store_true', help='One real discovery pass; no simulated data')
    a = p.parse_args()
    try:
        asyncio.run(run(a.date, a.root.resolve(), once=a.once))
    except Exception:
        import traceback
        directory = a.root.resolve()/'data'/'unattended'
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'scheduler-errors.log').open('a', encoding='utf-8') as fp:
            fp.write(datetime.now(CST).isoformat()+'\n'+traceback.format_exc()+'\n')
        raise


if __name__ == '__main__':
    main()
