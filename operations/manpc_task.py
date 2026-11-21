"""Bounded MANPC verification only; Windows Task Scheduler owns the lifecycle.

No save submissions, login changes, raw URLs in logs, or desktop interaction.
The existing VPS owner consumes any verified receipts. Status is aggregate-only.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

INTERVAL = 1800
LIMIT = 3
STATES = {'running', 'completed', 'waiting', 'provider_backoff', 'error'}
REMOTE_STATE = '/root/.local/state/swccg-wayback'
REMOTE_REPO = '/opt/swccg-ops/repos/swccg-wiki'
READ = '''import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from ops import wayback_worker as w
h=json.load(open('/root/.local/state/swccg-wayback/health.json'))
from datetime import datetime,timezone
t=datetime.fromisoformat(h['checked_at'].replace('Z','+00:00')).timestamp()
if not 0<=time.time()-t<=900:
 print(json.dumps({'allowed':False,'rows':[]})); sys.exit(0)
if h.get('mode') == 'half_open' and hasattr(w,'reliability'):
 d=json.load(open(w.DONE)); row=w.reliability.canary_candidate(w,Path('/root/.local/state/swccg-wayback'),d,h)
 print(json.dumps({'allowed':bool(row),'rows':[row] if row else []})); sys.exit(0)
if h.get('mode') != 'healthy' or h.get('outcome') == 'paused':
 print(json.dumps({'allowed':False,'rows':[]})); sys.exit(0)
d=json.load(open(w.DONE)); rows=[r for r in w.verify_candidates(d,limit=len(d)) if r.get('capture')]
recovery=w.recovery.manual_candidates(w,Path('/root/.local/state/swccg-wayback'),d) if hasattr(w,'recovery') else []
stale=w.reliability.manual_stale_candidates(w,Path('/root/.local/state/swccg-wayback'),d) if hasattr(w,'reliability') else []
slot=int(time.time())//1800
def take(values,n):
 i=(slot*n)%max(1,len(values)); return (values[i:]+values[:i])[:n]
chosen=take(recovery,1); chosen_urls={r['url'] for r in chosen}; chosen+=take([r for r in stale if r['url'] not in chosen_urls],1); chosen_urls={r['url'] for r in chosen}
rows=[r for r in rows if r['url'] not in chosen_urls]
print(json.dumps({'allowed':True,'rows':chosen+take(rows,3-len(chosen))}))'''
WRITE = '''import json,os,sys,tempfile
from pathlib import Path
p=Path('/root/.local/state/swccg-wayback'); x=json.load(sys.stdin)
def put(path,data):
 fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.manpc-')
 with os.fdopen(fd,'w') as f: json.dump(data,f); f.flush(); os.fsync(f.fileno())
 os.replace(tmp,path)
if x['updates']:
 put(p/'verify-inbox'/('manpc-scheduled-'+str(x['status']['started_at'])+'.json'),{'schema':1,'lane':'manpc','updates':x['updates']})
put(p/'manpc-health.json',x['status'])'''


def ssh(code, payload=None):
    command = 'cd '+REMOTE_REPO+' && python3 -B -c '+shlex.quote(code)
    result = subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=10',
                             '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2','gemp',command],
                            input=None if payload is None else json.dumps(payload),
                            text=True,capture_output=True,timeout=45,check=True,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return result.stdout


def load_state(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}


def log_status(directory, state):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'status.json'; tmp=directory/'status.tmp'
    tmp.write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8'); os.replace(tmp,path)
    log=directory/'runs.log'
    if log.exists() and log.stat().st_size>128*1024:
        os.replace(log,directory/'runs.previous.log')
    with log.open('a',encoding='utf-8') as f:
        f.write(json.dumps(state,separators=(',',':'))+'\n')


class ProviderStop(Exception):
    pass


def retry_delay(raw, now):
    try:
        delay=int(raw)
    except (TypeError,ValueError):
        try:
            delay=int(parsedate_to_datetime(raw).timestamp()-now)
        except (TypeError,ValueError,OverflowError):
            delay=0
    return max(INTERVAL,delay)


def run(directory, worker, connection=ssh, clock=time.time):
    start=int(clock()); old=load_state(directory/'status.json')
    state={'schema':1,'started_at':start,'checked_at':start,'state':'running',
           'last_success_at':old.get('last_success_at',0),'last_verified_count':0,
           'attempted':0,'errors':0,'next_expected_at':start+INTERVAL,
           'cooldown_until':old.get('cooldown_until',0)}
    updates={}
    def publish():
        state['checked_at']=int(clock()); log_status(directory,state)
        connection(WRITE,{'status':state,'updates':updates})
    original=worker.archive_response
    def observe(url, status, headers):
        original(url,status,headers)
        if worker.urllib.parse.urlsplit(url).hostname=='web.archive.org' and (status in (401,429) or status>=500):
            state['cooldown_until']=int(clock())+retry_delay((headers or {}).get('Retry-After'),clock())
            raise ProviderStop()
    try:
        publish()
        if start<state['cooldown_until']:
            state['state']='provider_backoff'
        else:
            response=json.loads(connection(READ)); rows=response['rows']
            if response.get('allowed') is not True:
                state['state']='waiting'
            elif not isinstance(rows,list) or len(rows)>LIMIT:
                raise ValueError('invalid candidate batch')
            else:
                worker.archive_response=observe
                for row in rows:
                    if clock()-start>360: break
                    state['attempted']+=1
                    try:
                        status, info=worker.verify(row['url'],row['capture'])
                        if status=='verified':
                            updates[row['url']]={'capture':row['capture'],'status':'verified','verify':info,'job_id':row.get('job_id') or ''}
                    except worker.VerifyRetry as exc:
                        state['errors']+=1
                        if exc.side=='capture':
                            state['cooldown_until']=int(clock())+INTERVAL
                            raise ProviderStop() from exc
                    if clock()-start<360: time.sleep(2)
                state['state']='completed'; state['last_success_at']=int(clock())
    except ProviderStop:
        state['state']='provider_backoff'; state['errors']+=1
    except Exception:
        state['state']='error'; state['errors']+=1
    finally:
        worker.archive_response=original
    state['last_verified_count']=len(updates)
    try:
        publish()
    except Exception:
        state['state']='error'; state['errors']+=1; state['last_verified_count']=0
        log_status(directory,state); return 1
    return int(state['state']=='error')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wiki-ops',type=Path,required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(args.wiki_ops.resolve()))
    worker=importlib.import_module('wayback_worker')
    # Keep ordinary PC use responsive. Larger captures remain with the VPS.
    worker.MAX_FETCH=min(worker.MAX_FETCH,8*1024*1024)
    worker.MAX_BODY=min(worker.MAX_BODY,worker.MAX_FETCH)
    return run(args.state_dir,worker)


if __name__=='__main__':
    raise SystemExit(main())
