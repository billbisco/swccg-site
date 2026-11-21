"""Validated aggregate projections. Never persist raw log identifiers."""
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import re
from .contracts import AdapterResult,Metric
from .health import read_receipt

SECURITY_KEYS=('community_active','community_dropped_packets','community_dropped_bytes','local_active','local_dropped_packets','local_dropped_bytes','processed_packets')
PLAYER_KEYS=('games','partial_ids','participants','local_accounts')
AUDIENCE_KEYS=('page_requests','likely_browser_requests','known_automation_requests','unknown_requests','browser_address_groups','parsed_lines','invalid_lines','coverage_seconds')
SECURITY_SPECS=tuple((f'security.{k}','bytes' if k.endswith('_bytes') else 'count','all') for k in SECURITY_KEYS)+(('security.source_checked_at','epoch_seconds','all'),)
PLAYER_SPECS=tuple((f'player.{k}','count',f'rolling_{d}d') for d in (1,7,30) for k in PLAYER_KEYS)+(('player.source_checked_at','epoch_seconds','all'),)
AUDIENCE_SPECS=tuple((f'audience.forum_{k}','seconds' if k=='coverage_seconds' else 'count','all') for k in AUDIENCE_KEYS)


def unavailable(source,now,detail='missing_receipt'):
    return AdapterResult(source,now,(),status='unavailable',detail=detail)


def count(v):return v if type(v) is int and 0<=v<=2**53-1 else None


def project_network(path,source,now):
    try:
        d=read_receipt(path); row=d[source]
        if d['schema']!=1 or not isinstance(row,dict):return unavailable(source,now)
        observed=count(row['observed_at'])
        if observed is None or observed>now:return unavailable(source,now,'invalid_receipt')
        if source=='player':
            for window in row['windows'].values():
                if any(count(window.get(k)) is None for k in PLAYER_KEYS) or window['partial_ids']>window['games'] or window['participants']>2*window['games']:
                    return unavailable(source,now,'invalid_receipt')
        specs=SECURITY_SPECS if source=='security' else PLAYER_SPECS
        result=[]
        for mid,unit,dim in specs:
            key=mid.split('.',1)[1]
            if key=='source_checked_at':val=observed
            elif source=='security':val=count(row['metrics'].get(key))
            else:val=count(row['windows'][dim.removeprefix('rolling_').removesuffix('d')].get(key))
            result.append(Metric(mid,val,unit,dim))
        partial=any(x.value is None for x in result); old=now-observed>(900 if source=='security' else 3600)
        return AdapterResult(source,now,tuple(result),status='degraded' if partial or old else 'ok',detail='source_old' if old else 'partial_receipt' if partial else None)
    except (OSError,ValueError,TypeError,KeyError):return unavailable(source,now,'invalid_receipt')


LINE=re.compile(r'^([^ ]+) \S+ \S+ \[([^\]]+)\] "([A-Z]+) ([^ ]+) HTTP/[^" ]+" ([0-9]{3}) \S+ "(?:[^"\\]|\\.)*" "((?:[^"\\]|\\.)*)"')
BOT=re.compile(r'bot|crawler|spider|headless|curl|wget|python|scrapy|slurp|httpclient|preview',re.I)
BROWSER=re.compile(r'Chrome/|Firefox/|Safari/|Edg/|OPR/',re.I)


def forum_projection(current,previous,now):
    """Scan only two small dedicated forum logs; unique address groups are ephemeral."""
    start=now-86400; first=None; parsed=invalid=0; counts=dict.fromkeys(AUDIENCE_KEYS,0); groups=set()
    try:
        snapshots=[]
        for path in (previous,current):
            st=path.stat()
            if st.st_size>4*1024*1024:return unavailable('audience',now,'source_limit')
            with path.open('rb') as f: raw=f.read(4*1024*1024+1)
            if len(raw)>4*1024*1024:return unavailable('audience',now,'source_limit')
            snapshots.append((path,st.st_dev,st.st_ino,raw))
        if any((p.stat().st_dev,p.stat().st_ino)!=(dev,ino) for p,dev,ino,_ in snapshots) or snapshots[0][1:3]==snapshots[1][1:3]:return unavailable('audience',now,'source_changed')
        for _,_,_,raw in snapshots:
            for line in raw.splitlines():
                m=LINE.match(line.decode('utf-8','replace'))
                if not m:invalid+=1;continue
                try:
                    ts=int(datetime.strptime(m[2],'%d/%b/%Y:%H:%M:%S %z').timestamp());ip=ipaddress.ip_address(m[1])
                except ValueError:invalid+=1;continue
                first=ts if first is None else min(first,ts)
                if not start<=ts<=now:continue
                parsed+=1
                path=m[4].split('?',1)[0]
                # Public forum page navigations only. Static assets and POSTs are excluded.
                if m[3]!='GET' or not 200<=int(m[5])<400 or not (path.endswith('.php') or path.endswith('/')):continue
                counts['page_requests']+=1;ua=m[6]
                if BOT.search(ua):counts['known_automation_requests']+=1
                elif 'Mozilla/' in ua and BROWSER.search(ua):counts['likely_browser_requests']+=1;groups.add(ip)
                else:counts['unknown_requests']+=1
        counts.update(browser_address_groups=len(groups),parsed_lines=parsed,invalid_lines=invalid,coverage_seconds=min(86400,max(0,now-first)) if first is not None else 0)
        # Missing coverage is explicitly incomplete, not a confident zero for the day.
        partial=first is None or first>start or invalid>0
        return AdapterResult('audience',now,tuple(Metric(mid,counts[mid.removeprefix('audience.forum_')],unit,dim) for mid,unit,dim in AUDIENCE_SPECS),status='degraded' if partial else 'ok',detail='partial_receipt' if partial else None)
    except (OSError,ValueError):return unavailable('audience',now,'missing_receipt')
