"""One-shot source projection: local CrowdSec aggregate and existing Play JDBC owner."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import os
import tempfile
import sqlite3
import re


def command(args, timeout, stdin=None):
    r=subprocess.run(args,input=stdin,text=True,capture_output=True,timeout=timeout,check=True)
    if len(r.stdout)>256*1024: raise ValueError('output too large')
    return json.loads(r.stdout)


def bouncer_interval(payload):
    d=json.loads(payload); rows=d['metrics']
    if len(rows)!=1:raise ValueError('unexpected interval shape')
    row=rows[0]; end=row['meta']['utc_now_timestamp']; seconds=row['meta']['window_size_seconds']
    if type(end) is not int or type(seconds) is not int or not 0<seconds<=1800:raise ValueError('invalid interval')
    sums={};seen=set()
    for item in row['items']:
        labels=item['labels']; origin=labels.get('origin'); iptype=labels.get('ip_type'); name=item['name'];unit=item['unit'];v=item['value']
        if iptype not in ('ipv4','ipv6') or type(v) is not int or v<0:raise ValueError('invalid aggregate row')
        mapping={'CAPI':'community','crowdsec':'local'}
        if origin in mapping and (name,unit) in (('active_decisions','ip'),('dropped','packet'),('dropped','byte')):
            key=mapping[origin]+{'ip':'_active','packet':'_dropped_packets','byte':'_dropped_bytes'}[unit]
        elif origin is None and (name,unit)==('processed','packet'):key='processed_packets'
        else:continue
        if (key,iptype) in seen:raise ValueError('duplicate aggregate row')
        seen.add((key,iptype));sums[key]=sums.get(key,0)+v
    if not sums:raise ValueError('no known counters')
    return end,seconds,sums


def security(db='/var/lib/crowdsec/data/crowdsec.db'):
    # CrowdSec's already-persisted remediation component intervals, not raw decisions.
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True,timeout=3) as conn:
        conn.execute('PRAGMA query_only=ON')
        raw=[r[0] for r in conn.execute("SELECT payload FROM metrics WHERE generated_type='RC' AND generated_by='cs-firewall-bouncer-gemp' ORDER BY id DESC LIMIT 100")]
    if not raw:raise ValueError('no aggregate receipts')
    rows=[bouncer_interval(x) for x in raw if len(x)<65536]
    if not rows:raise ValueError('no valid aggregate receipts')
    end,seconds,latest=rows[0]; now=int(time.time())
    if not 0<=now-end<=3600:raise ValueError('stale interval')
    result=dict(latest);result['interval_seconds']=seconds
    total={key:0 for key in ('community_dropped_packets','local_dropped_packets','processed_packets')}; coverage=0; boundary=end
    for e,s,metrics in rows:
        if e>boundary or e-s<end-86400:continue
        if any(key not in metrics for key in total):continue
        for key in total:total[key]+=metrics[key]
        coverage+=s;boundary=e-s
    result.update({'daily_'+k:v for k,v in total.items()});result['daily_coverage_seconds']=coverage
    return {'observed_at':end,'metrics':result}


def player(source):
    shell='d=$(mktemp -d); trap \'rm -f "$d/PlayAggregate.java"; rmdir "$d"\' EXIT; cat > "$d/PlayAggregate.java"; java -Xmx32m -XX:MaxMetaspaceSize=96m -XX:ActiveProcessorCount=1 -cp /opt/gemp-swccg/src/gemp-swccg-async/target/web.jar "$d/PlayAggregate.java"'
    return command(['docker','exec','-i','gemp_swccg_app_1','sh','-c',shell],30,source.read_text(encoding='utf-8'))


def parse_play_status(text):
    if len(text)>8192:raise ValueError('status response budget')
    active=re.search(r'Active players:</b>&nbsp;&nbsp;(\d+) \(in last 24 hours\),&nbsp;&nbsp;(\d+) \(in last week\),&nbsp;&nbsp;(\d+) \(all time\)',text)
    games=re.search(r'Games played:</b>&nbsp;&nbsp;(\d+) \(in last 24 hours\),&nbsp;&nbsp;(\d+) \(in last week\),&nbsp;&nbsp;(\d+) \(all time\)',text)
    live=re.search(r'Tables count: (\d+)&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Players in hall count: (\d+)',text)
    if not active or not games or not live:raise ValueError('unknown status contract')
    return dict(zip(('active_1d','active_7d','active_all','played_1d','played_7d','played_all','tables','hall'),map(int,(*active.groups(),*games.groups(),*live.groups()))))


def player_status():
    r=subprocess.run(['docker','exec','gemp_swccg_app_1','curl','-fsS','--max-time','5','http://127.0.0.1/gemp-swccg-server/'],capture_output=True,text=True,timeout=10,check=True)
    return {'observed_at':int(time.time()),'metrics':parse_play_status(r.stdout)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--java-source',type=Path,required=True);args=p.parse_args()
    try: old=json.loads(args.output.read_text(encoding='utf-8'))
    except (OSError,ValueError):old={}
    now=int(time.time()); result={'schema':1,'security':None,'player':None,'player_status':None}
    try:result['security']=security()
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError):pass
    oldplayer=old.get('player')
    if isinstance(oldplayer,dict) and type(oldplayer.get('observed_at')) is int and 0<=now-oldplayer['observed_at']<1800:result['player']=oldplayer
    else:
        try:result['player']=player(args.java_source)
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):pass
    try:result['player_status']=player_status()
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):pass
    with tempfile.NamedTemporaryFile(mode='w',dir=args.output.parent,delete=False,encoding='utf-8') as f:
        json.dump(result,f); f.flush(); os.fsync(f.fileno()); tmp=f.name
    os.chmod(tmp,0o600); os.replace(tmp,args.output)


if __name__=='__main__':main()
