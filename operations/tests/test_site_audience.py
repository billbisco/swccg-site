import json
import ipaddress
import unittest
from operations.deploy.site_audience import fresh,apply,summary,roll,add,estimate,M


class AudienceStreamTests(unittest.TestCase):
    def test_group_dedup_across_scopes_and_no_raw_storage(self):
        now=1767268800;s=fresh(now)
        for scope in ('site','wiki','play'):
            self.assertTrue(apply(s,f'v1 {now} {scope} browser 192.0.2.4'.encode(),now))
        self.assertTrue(apply(s,f'v1 {now} wiki automation 192.0.2.5'.encode(),now))
        r=summary(s);self.assertEqual(1,r['scopes']['production']['browser_groups']);self.assertEqual(3,r['scopes']['production']['browser']);self.assertEqual(1,r['scopes']['production']['automation'])
        self.assertNotIn('192.0.2',json.dumps(s));self.assertNotIn('salt',json.dumps(r))
        self.assertFalse(apply(s,f'v1 {now-121} site browser 192.0.2.4'.encode(),now))

    def test_day_rotation_removes_key_and_retains_only_aggregate(self):
        s=fresh(1767268800);new=roll(s,1767268800+86400)
        self.assertNotEqual(s['salt'],new['salt']);self.assertNotIn('salt',new['previous']);self.assertEqual(s['day'],new['previous']['day'])
        self.assertIsNone(roll(s,1767268800+86400*3)['previous'])

    def test_sketch_accuracy_small_fixture(self):
        r=[0]*M;k=b'a'*32
        for i in range(1,1000):add(r,k,ipaddress.ip_address(i))
        self.assertLess(abs(estimate(r)-999),100)
