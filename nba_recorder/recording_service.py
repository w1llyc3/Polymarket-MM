"""Rolling local public-data recorder; no order authorization or wallet access."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime,timedelta
from pathlib import Path

from .unattended import CST,save


def recording_days(now):
    day=now.astimezone(CST).date()
    days=[day,day+timedelta(days=1)]
    if now.astimezone(CST).hour<7:days.insert(0,day-timedelta(days=1))
    return [d.isoformat() for d in days]


def console_health(root):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:8765/api/health',timeout=2) as response:
            value=json.load(response)
        if value.get('app')!='NBA_MM_LOCAL_CONSOLE_V2_2' or value.get('root_id')!=hashlib.sha256(str(root).encode()).hexdigest()[:16]:
            return 'FOREIGN_SERVICE'
        return 'HEALTHY' if value.get('owner_alive') and value.get('owner_responsive') else 'UNRESPONSIVE'
    except Exception:return 'UNAVAILABLE'


class RollingRecorder:
    def __init__(self,root,spawn=None,health=None):
        self.root=Path(root).resolve();self.directory=self.root/'data/unattended/service'
        self.directory.mkdir(parents=True,exist_ok=True)
        self.spawn=spawn or self._spawn;self.health=health or (lambda:console_health(self.root))
        self.workers={};self.retry={};self.completed=set();self.console=None;self.console_retry=0

    def _spawn(self,args,label):
        with (self.directory/(label+'.log')).open('ab') as log:
            return subprocess.Popen([sys.executable,*args],cwd=self.root,stdin=subprocess.DEVNULL,
                stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def tick(self,now):
        at=int(now.timestamp()*1000);wanted=recording_days(now)
        for day,process in list(self.workers.items()):
            code=process.poll()
            if code is not None:
                del self.workers[day]
                if code==0:self.completed.add(day)
                else:self.retry[day]=at+120000
        for day in wanted:
            if day in self.workers or day in self.completed or at<self.retry.get(day,0):continue
            path=self.root/'data/unattended'/day/'state.json'
            if path.exists():
                state=json.loads(path.read_text(encoding='utf-8'))
                if state.get('scheduler_status')=='COMPLETE':self.completed.add(day);continue
                config=state.get('recording_window')
                if config not in (None,{'pregame_s':1800,'postgame_s':1800,'record_only':True}):
                    # Do not change an already recorded account's mode/window.
                    self.retry[day]=at+120000;continue
            self.workers[day]=self.spawn(['-m','nba_recorder.unattended','--date',day,
                '--pre-minutes','30','--post-minutes','30','--record-only'],'day-'+day)
        status=self.health()
        if status=='UNAVAILABLE' and (self.console is None or self.console.poll() is not None) and at>=self.console_retry:
            self.console=self.spawn(['-m','nba_recorder.console','--no-browser'],'console')
            self.console_retry=at+60000
            status='STARTING'
        snapshot={'heartbeat_ms':at,'pid':os.getpid(),'mode':'RECORD_ONLY','pre_minutes':30,'post_minutes':30,
            'timezone':'Asia/Shanghai','requested_days':wanted,'day_worker_pids':{d:p.pid for d,p in self.workers.items()},
            'retry_after_ms':self.retry,'completed_days':sorted(self.completed),'console_status':status,
            'real_submission_calls':0,'live_enabled':False}
        save(self.directory/'state.json',snapshot)
        return snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);args=parser.parse_args()
    service=RollingRecorder(args.root)
    lock=open(service.directory/'service.lock','a+b')
    try:
        if os.name=='nt':
            import msvcrt
            lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();return  # Existing service owns the same project.
    try:
        while True:
            try:service.tick(datetime.now(CST))
            except Exception:
                import traceback
                with (service.directory/'errors.log').open('a',encoding='utf-8') as fp:
                    fp.write(datetime.now(CST).isoformat()+'\n'+traceback.format_exc()+'\n')
            time.sleep(30)
    finally:lock.close()


if __name__=='__main__':main()
