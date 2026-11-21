"""Validated aggregate projections. Never persist raw log identifiers."""
from datetime import datetime, timezone, timedelta
import ipaddress
from pathlib import Path
import re
from .contracts import AdapterResult,Metric
from .health import read_receipt

SECURITY_KEYS=('community_active','community_dropped_packets','community_dropped_bytes','local_active','local_dropped_packets','local_dropped_bytes','processed_packets','interval_seconds','daily_community_dropped_packets','daily_local_dropped_packets','daily_processed_packets','daily_coverage_seconds')
BLOCK_KEYS=('wiki_blocked_requests_15m','wiki_blocked_429_15m','wiki_blocked_503_15m','wiki_blocked_other_15m','wiki_blocked_coverage_seconds','wiki_blocked_invalid_lines')
PLAYER_KEYS=('games','partial_ids','participants','local_accounts')
PLAY_STATUS_KEYS=('active_1d','active_7d','active_all','played_1d','played_7d','played_all','tables','hall')
AUDIENCE_KEYS=('page_requests','likely_browser_requests','known_automation_requests','unknown_requests','browser_address_groups','parsed_lines','invalid_lines','coverage_seconds')
SITE_SCOPES=('production','site','wiki','play','forum')
SITE_FIELDS=('browser','automation','unknown','browser_groups')
SECURITY_SPECS=tuple((f'security.{k}','bytes' if k.endswith('_bytes') else 'seconds' if k.endswith('_seconds') else 'count','all') for k in SECURITY_KEYS+BLOCK_KEYS)+(('security.source_checked_at','epoch_seconds','all'),)
PLAYER_SPECS=tuple((f'player.{k}','count',f'rolling_{d}d') for d in (1,7,30) for k in PLAYER_KEYS)+(('player.source_checked_at','epoch_seconds','all'),)+tuple((f'player.reported_{k}','count','all') for k in PLAY_STATUS_KEYS)+(('player.status_checked_at','epoch_seconds','all'),)
FORUM_SPECS=tuple((f'audience.forum_{k}','seconds' if k=='coverage_seconds' else 'count','all') for k in AUDIENCE_KEYS)
SITE_SPECS=tuple((f'audience.{d}_{k}','count',scope) for d in ('today','yesterday') for scope in SITE_SCOPES for k in SITE_FIELDS)+tuple((f'audience.{d}_coverage','seconds','all') for d in ('today','yesterday'))+(('audience.site_checked_at','epoch_seconds','all'),)
AUDIENCE_SPECS=FORUM_SPECS+SITE_SPECS


def unavailable(source,now,detail='missing_receipt'):
    return AdapterResult(source,now,(),status='unavailable',detail=detail)


def count(v):return v if type(v) is int and 0<=v<=2**53-1 else None


def project_network(path,source,now):
    try:
        d=read_receipt(path); row=d[source]
        if d['schema']!=1:return unavailable(source,now)
        row=row if isinstance(row,dict) else {}
        observed=count(row.get('observed_at'))
        if observed is None or observed>now:row={};observed=None
        specs=SECURITY_SPECS if source=='security' else PLAYER_SPECS
        status=d.get('player_status');status=status if isinstance(status,dict) and count(status.get('observed_at')) is not None and 0<=now-status['observed_at']<=900 else {}
        if source=='player' and row:
            for window in row['windows'].values():
                if any(count(window.get(k)) is None for k in PLAYER_KEYS) or window['partial_ids']>window['games'] or window['participants']>2*window['games']:row={};observed=None;break
        if not row and (source!='player' or not status):return unavailable(source,now,'invalid_receipt')
        result=[]
        for mid,unit,dim in specs:
            key=mid.split('.',1)[1]
            if key=='source_checked_at':val=observed
            elif source=='security':val=count(row['metrics'].get(key))
            elif key=='status_checked_at':val=status.get('observed_at')
            elif key.startswith('reported_'):val=count(status.get('metrics',{}).get(key.removeprefix('reported_')))
            else:val=count(row.get('windows',{}).get(dim.removeprefix('rolling_').removesuffix('d'),{}).get(key))
            result.append(Metric(mid,val,unit,dim))
        partial=any(x.value is None for x in result if not (source=='security' and x.metric_id.removeprefix('security.') in BLOCK_KEYS)) or (source=='security' and row['metrics'].get('daily_coverage_seconds',0)<86400); old=observed is not None and now-observed>(1800 if source=='security' else 3600)
        return AdapterResult(source,now,tuple(result),status='degraded' if partial or old else 'ok',detail='source_old' if old else 'partial_receipt' if partial else None)
    except (OSError,ValueError,TypeError,KeyError):return unavailable(source,now,'invalid_receipt')


LINE=re.compile(r'^([^ ]+) \S+ \S+ \[([^\]]+)\] "([A-Z]+) ([^ ]+) HTTP/[^" ]+" ([0-9]{3}) \S+ "(?:[^"\\]|\\.)*" "((?:[^"\\]|\\.)*)"')
BLOCK_LINE=re.compile(rb'^\S+ \S+ \S+ \[([^\]]+)\] "(?:[^"\\]|\\.)*" ([0-9]{3}) ')
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
        return AdapterResult('audience',now,tuple(Metric(mid,counts[mid.removeprefix('audience.forum_')],unit,dim) for mid,unit,dim in FORUM_SPECS),status='degraded' if partial else 'ok',detail='partial_receipt' if partial else None)
    except (OSError,ValueError):return unavailable('audience',now,'missing_receipt')


def site_projection(forum,site_path,now):
    d={};observed=None
    if site_path is not None:
        try:
            candidate=read_receipt(site_path);t=count(candidate.get('observed_at'))
            if candidate.get('schema')==1 and t is not None and 0<=now-t<=300: d=candidate;observed=t
        except (OSError,ValueError,TypeError):pass
    forum_values={m.metric_id:m.value for m in forum.samples}
    samples=[Metric(mid,forum_values.get(mid),unit,dim) for mid,unit,dim in FORUM_SPECS]
    for mid,unit,dim in SITE_SPECS:
        key=mid.removeprefix('audience.');val=None
        if key=='site_checked_at':val=observed
        else:
            period,field=key.split('_',1);r=d.get(period)
            if isinstance(r,dict):
                expected=(datetime.fromtimestamp(now,timezone.utc).date()-timedelta(days=period=='yesterday')).isoformat()
                if r.get('day')==expected:
                    if field=='coverage':val=count(r.get('coverage_seconds'));val=val if val is not None and val<=86400 else None
                    else:
                        scopes=r.get('scopes');row=scopes.get(dim) if isinstance(scopes,dict) else None
                        val=count(row.get(field)) if isinstance(row,dict) else None
        samples.append(Metric(mid,val,unit,dim))
    return AdapterResult('audience',now,tuple(samples),status='ok' if observed and forum.status=='ok' else 'degraded',detail=None if observed and forum.status=='ok' else 'partial_receipt')


def blocked_counts(current,previous,now):
    """Bounded tails only; no request, address or UA is retained."""
    start=now-900;first=None;invalid=0;totals=dict.fromkeys(BLOCK_KEYS,0); identities=set();current_cutoff=None
    try:
        for path in (previous,current):
            st=path.stat()
            if (st.st_dev,st.st_ino) in identities:return {}
            identities.add((st.st_dev,st.st_ino))
            with path.open('rb') as f:
                offset=max(0,st.st_size-2*1024*1024);f.seek(offset);raw=f.read(2*1024*1024)
            if (path.stat().st_dev,path.stat().st_ino)!=(st.st_dev,st.st_ino):return {}
            lines=raw.splitlines()
            if offset:lines=lines[1:]
            if raw and not raw.endswith(b'\n'):lines=lines[:-1]
            for line in lines:
                m=BLOCK_LINE.match(line)
                if not m:invalid+=1;continue
                try:ts=int(datetime.strptime(m[1].decode('ascii'),'%d/%b/%Y:%H:%M:%S %z').timestamp())
                except (ValueError,UnicodeError):invalid+=1;continue
                first=ts if first is None else min(ts,first)
                if path==current and offset:current_cutoff=ts if current_cutoff is None else min(current_cutoff,ts)
                if start<=ts<=now:
                    totals['wiki_blocked_requests_15m']+=1
                    key='wiki_blocked_'+(m[2].decode() if m[2] in (b'429',b'503') else 'other')+'_15m';totals[key]+=1
        if current_cutoff is not None and current_cutoff>start:first=current_cutoff
        totals['wiki_blocked_coverage_seconds']=min(900,max(0,now-first)) if first is not None else 0
        totals['wiki_blocked_invalid_lines']=invalid
        return totals
    except OSError:return {}


def security_projection(base,current,previous,now):
    values={m.metric_id:m.value for m in base.samples}
    blocked=blocked_counts(current,previous,now) if current and previous else {}
    for k,v in blocked.items():values['security.'+k]=v
    samples=tuple(Metric(mid,values.get(mid),unit,dim) for mid,unit,dim in SECURITY_SPECS)
    return AdapterResult('security',now,samples,status='degraded' if base.status!='ok' or blocked.get('wiki_blocked_coverage_seconds',0)<900 else 'ok',detail='partial_receipt' if base.status!='ok' or blocked.get('wiki_blocked_coverage_seconds',0)<900 else None)
