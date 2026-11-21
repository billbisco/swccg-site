"""Local datagram aggregation of coarse production navigation events.

Raw addresses are used only in memory to update day-keyed cardinality sketches;
neither addresses, per-client hashes, paths nor user agents are ever persisted.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import struct
import tempfile
import time
from datetime import datetime,timezone,timedelta

SCOPES=('production','site','wiki','play','forum'); CLASSES=('browser','automation','unknown'); M=2048
LINE=re.compile(rb'(?:^| )v1 ([0-9]{1,14}(?:\.[0-9]{1,6})?) (site|wiki|play|forum) (browser|automation|unknown) ([0-9a-fA-F:.]+)$')


def day(now):return datetime.fromtimestamp(now,timezone.utc).date().isoformat()


def estimate(registers):
    zeros=registers.count(0)
    raw=(0.7213/(1+1.079/M))*M*M/sum(2.0**(-r) for r in registers)
    if zeros and raw<2.5*M:raw=M*math.log(M/zeros)
    return round(raw)


def add(registers,key,address):
    x=int.from_bytes(hmac.digest(key,address.packed,'sha256')[:8],'big');idx=x&(M-1);rest=x>>11
    registers[idx]=max(registers[idx],54-rest.bit_length())


def fresh(now,previous=None):
    return {'schema':1,'day':day(now),'salt':secrets.token_hex(32),'started_at':int(now),'coverage_seconds':0,'last_tick':int(now),'previous':previous,
            'scopes':{s:{'browser':0,'automation':0,'unknown':0,'registers':[0]*M} for s in SCOPES}}


def summary(state):
    return {'day':state['day'],'started_at':state['started_at'],'coverage_seconds':state['coverage_seconds'],
            'scopes':{k:{'browser':v['browser'],'automation':v['automation'],'unknown':v['unknown'],'browser_groups':min(v['browser'],estimate(v['registers']))} for k,v in state['scopes'].items()}}


def roll(state,now):
    if state['day']!=day(now):
        yesterday=(datetime.fromtimestamp(now,timezone.utc).date()-timedelta(days=1)).isoformat()
        return fresh(now,summary(state) if state['day']==yesterday else None)
    return state


def apply(state,blob,now):
    if len(blob)>256:return False
    m=LINE.search(blob)
    if not m:return False
    try:
        ts=float(m[1]);ip=ipaddress.ip_address(m[4].decode('ascii'))
        if not 0<=now-ts<=120 or day(ts)!=state['day']:return False
    except (ValueError,UnicodeError,OverflowError,OSError):return False
    scope=m[2].decode();kind=m[3].decode();key=bytes.fromhex(state['salt'])
    for s in ('production',scope):
        v=state['scopes'][s];v[kind]+=1
        if kind=='browser':add(v['registers'],key,ip)
    return True


def atomic(path,value,mode):
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as f:
        json.dump(value,f,separators=(',',':'));f.flush();os.fsync(f.fileno());name=f.name
    os.chmod(name,mode);os.replace(name,path)


def main():
    p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--socket',type=Path,required=True);p.add_argument('--peer-uid',type=int,required=True);a=p.parse_args()
    now=int(time.time());statepath=a.state/'sketch.json'; state=fresh(now)
    if statepath.exists():
        if statepath.stat().st_size>256*1024:raise ValueError('state budget')
        state=roll(json.loads(statepath.read_text()),now)
        if set(state['scopes'])!=set(SCOPES) or any(len(v['registers'])!=M for v in state['scopes'].values()):raise ValueError('invalid sketch')
        state['last_tick']=now
    if a.socket.exists():raise ValueError('existing audience socket')
    s=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1);s.settimeout(1);s.bind(str(a.socket));os.chmod(a.socket,0o660)
    stopped=False
    def stop(*_):
        nonlocal stopped;stopped=True
    signal.signal(signal.SIGTERM,stop)
    last=0
    try:
        while not stopped:
            now=int(time.time());state=roll(state,now)
            state['coverage_seconds']=min(86400,state['coverage_seconds']+min(2,max(0,now-state['last_tick'])));state['last_tick']=now
            if time.monotonic()-last>=30:
                atomic(statepath,state,0o600);atomic(a.state/'summary.json',{'schema':1,'observed_at':now,'today':summary(state),'yesterday':state['previous']},0o644);last=time.monotonic()
            try:blob,anc,flags,_=s.recvmsg(256,socket.CMSG_SPACE(struct.calcsize('3i')))
            except TimeoutError:continue
            uid=next((struct.unpack('3i',c[:struct.calcsize('3i')])[1] for level,kind,c in anc if level==socket.SOL_SOCKET and kind==socket.SCM_CREDENTIALS),None)
            if uid==a.peer_uid and not flags&socket.MSG_TRUNC:apply(state,blob,time.time())
    finally:
        s.close();a.socket.unlink(missing_ok=True)
        atomic(statepath,state,0o600)


if __name__=='__main__':main()
