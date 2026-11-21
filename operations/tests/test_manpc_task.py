import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import parse

from operations.manpc_task import run, READ, WRITE, retry_delay


class VerifyRetry(Exception):
    side='capture'


class ManpcTaskTests(unittest.TestCase):
    def test_remote_selection_program_is_valid_python(self):
        compile(READ,'<remote-selection>','exec')
        compile(WRITE,'<remote-receipt>','exec')
    def test_waits_on_owner_hold_without_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            worker=SimpleNamespace(archive_response=lambda *a:None, VerifyRetry=VerifyRetry,
                                   verify=lambda *a:self.fail('must not fetch'))
            def connect(code,payload=None):
                if code==READ: return json.dumps({'allowed':False,'rows':[]})
                calls.append(json.loads(json.dumps(payload)))
            self.assertEqual(0,run(Path(tmp),worker,connect,lambda:10000))
            self.assertEqual('waiting',calls[-1]['status']['state'])
            self.assertEqual({},calls[-1]['updates'])

    def test_receipt_and_provider_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent=[]; calls=[]
            worker=SimpleNamespace(archive_response=lambda *a:None,VerifyRetry=VerifyRetry,urllib=SimpleNamespace(parse=parse))
            def verify(url,cap):
                calls.append(url)
                if len(calls)==2: worker.archive_response('https://web.archive.org/web/1',429,{'Retry-After':'7200'})
                return 'verified',{'method':'sha256','at':'2023-01-01T00:00:00Z'}
            worker.verify=verify
            def connect(code,payload=None):
                if code==READ: return json.dumps({'allowed':True,'rows':[{'url':str(i),'capture':'capture'} for i in range(3)]})
                sent.append(json.loads(json.dumps(payload)))
            with patch('operations.manpc_task.time.sleep'):
                self.assertEqual(0,run(Path(tmp),worker,connect,lambda:10000))
            self.assertEqual(2,len(calls))
            self.assertEqual('provider_backoff',sent[-1]['status']['state'])
            self.assertEqual(17200,sent[-1]['status']['cooldown_until'])
            self.assertEqual(1,len(sent[-1]['updates']))
            self.assertNotIn('capture',(Path(tmp)/'runs.log').read_text())

    def test_retry_after_floor(self):
        self.assertEqual(1800,retry_delay('40',0))
        self.assertEqual(7200,retry_delay('7200',0))
