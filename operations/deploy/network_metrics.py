"""One-shot source projection: local CrowdSec aggregate and existing Play JDBC owner."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import os
import tempfile


def command(args, timeout, stdin=None):
    r=subprocess.run(args,input=stdin,text=True,capture_output=True,timeout=timeout,check=True)
    if len(r.stdout)>256*1024: raise ValueError('output too large')
    return json.loads(r.stdout)


def security():
    d=command(['cscli','metrics','show','bouncers','-o','json'],10)
    rows=d['bouncers']; v=rows['cs-firewall-bouncer-gemp']
    result={}
    for group,name in (('CAPI','community'),('crowdsec','local')):
        r=v[group]; result[name+'_active']=r['active_decisions']['ip']; result[name+'_dropped_packets']=r['dropped']['packet'];result[name+'_dropped_bytes']=r['dropped']['byte']
    result['processed_packets']=v['']['processed']['packet']
    if any(type(n) is not int or n<0 for n in result.values()):raise ValueError('invalid security aggregate')
    return {'observed_at':int(time.time()),'metrics':result}


def player(source):
    shell='d=$(mktemp -d); trap \'rm -f "$d/PlayAggregate.java"; rmdir "$d"\' EXIT; cat > "$d/PlayAggregate.java"; java -Xmx32m -XX:MaxMetaspaceSize=96m -XX:ActiveProcessorCount=1 -cp /opt/gemp-swccg/src/gemp-swccg-async/target/web.jar "$d/PlayAggregate.java"'
    return command(['docker','exec','-i','gemp_swccg_app_1','sh','-c',shell],30,source.read_text(encoding='utf-8'))


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--java-source',type=Path,required=True);args=p.parse_args()
    try: old=json.loads(args.output.read_text(encoding='utf-8'))
    except (OSError,ValueError):old={}
    now=int(time.time()); result={'schema':1,'security':None,'player':None}
    try:result['security']=security()
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):pass
    oldplayer=old.get('player')
    if isinstance(oldplayer,dict) and type(oldplayer.get('observed_at')) is int and 0<=now-oldplayer['observed_at']<1800:result['player']=oldplayer
    else:
        try:result['player']=player(args.java_source)
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):pass
    with tempfile.NamedTemporaryFile(mode='w',dir=args.output.parent,delete=False,encoding='utf-8') as f:
        json.dump(result,f); f.flush(); os.fsync(f.fileno()); tmp=f.name
    os.chmod(tmp,0o600); os.replace(tmp,args.output)


if __name__=='__main__':main()
