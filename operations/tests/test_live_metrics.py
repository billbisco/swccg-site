import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime,timezone
from operations.live_metrics import forum_projection, project_network,blocked_counts
from operations.deploy.network_metrics import bouncer_interval,parse_play_status

NOW=int(datetime(2026,1,2,12,tzinfo=timezone.utc).timestamp())


class LiveMetricsTests(unittest.TestCase):
    def test_blocked_request_projection_no_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);old=p/'old';new=p/'new'
            old.write_text('192.0.2.1 - - [02/Jan/2026:11:44:00 +0000] "GET /private HTTP/1.1" 429 0 "-" "fixture"\n')
            new.write_text('192.0.2.2 - - [02/Jan/2026:11:59:00 +0000] "GET /private HTTP/1.1" 503 0 "-" "fixture"\n')
            r=blocked_counts(new,old,NOW);self.assertEqual(1,r['wiki_blocked_requests_15m']);self.assertEqual(1,r['wiki_blocked_503_15m']);self.assertEqual(900,r['wiki_blocked_coverage_seconds']);self.assertNotIn('192.0.2',str(r))
    def test_play_status_contract(self):
        html='<b>Active players:</b>&nbsp;&nbsp;1 (in last 24 hours),&nbsp;&nbsp;2 (in last week),&nbsp;&nbsp;3 (all time)<br/><b>Games played:</b>&nbsp;&nbsp;4 (in last 24 hours),&nbsp;&nbsp;5 (in last week),&nbsp;&nbsp;6 (all time)<h2>Tables count: 7&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Players in hall count: 8</h2>'
        self.assertEqual(8,parse_play_status(html)['hall']);self.assertEqual(5,parse_play_status(html)['played_7d'])
    def test_bouncer_counts_distinct_origins_without_identity(self):
        items=[{'name':'dropped','unit':'packet','labels':{'origin':'crowdsec','ip_type':ip},'value':n} for ip,n in [('ipv4',5),('ipv6',2)]]
        raw=json.dumps({'metrics':[{'meta':{'utc_now_timestamp':NOW,'window_size_seconds':900},'items':items}]})
        end,seconds,counts=bouncer_interval(raw);self.assertEqual((NOW,900,7),(end,seconds,counts['local_dropped_packets']))
    def test_forum_groups_are_ephemeral_and_bots_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); previous=p/'old'; current=p/'current'
            def line(ip,t,ua,url='/forum/viewtopic.php?private=yes'):
                return f'{ip} - - [{t}] "GET {url} HTTP/1.1" 200 3 "-" "{ua}"\n'
            previous.write_text(line('192.0.2.19','01/Jan/2026:11:59:00 +0000','Mozilla/5.0 Firefox/12'))
            current.write_text(line('192.0.2.19','02/Jan/2026:11:00:00 +0000','Mozilla/5.0 Firefox/12')+line('192.0.2.19','02/Jan/2026:11:01:00 +0000','Mozilla/5.0 Firefox/12')+line('192.0.2.20','02/Jan/2026:11:02:00 +0000','ExampleBot/1'))
            r=forum_projection(current,previous,NOW); v={m.metric_id:m.value for m in r.samples}
            self.assertEqual(1,v['audience.forum_browser_address_groups']);self.assertEqual(2,v['audience.forum_likely_browser_requests']);self.assertEqual(1,v['audience.forum_known_automation_requests']);self.assertNotIn('192.0.2',str(r));self.assertNotIn('private=yes',str(r))

    def test_play_source_never_exposes_ids_and_rejects_bad_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'net.json'; window={'games':3,'partial_ids':1,'participants':4,'local_accounts':2}
            d={'schema':1,'player':{'observed_at':NOW,'windows':{str(i):dict(window) for i in (1,7,30)},'secret':'ignored'}};p.write_text(json.dumps(d))
            r=project_network(p,'player',NOW);self.assertEqual('degraded',r.status);self.assertNotIn('ignored',str(r))
            d['player']['windows']['1']['participants']=100;p.write_text(json.dumps(d));self.assertEqual('unavailable',project_network(p,'player',NOW).status)
