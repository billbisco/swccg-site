from pathlib import Path
import unittest
from operations.deploy.gateway_unit import render_gateway


class GatewayConfigTests(unittest.TestCase):
    def test_isolated_socket_and_exact_auth_routes(self):
        unit=render_gateway('/opt/ops','/mnt/data/store')
        self.assertIn('RuntimeDirectoryMode=0750',unit);self.assertIn('SupplementaryGroups=swccg-operations-read',unit)
        self.assertIn('--gateway-peer-uid 33',unit);self.assertIn('PrivateNetwork=yes',unit)
        conf=(Path(__file__).resolve().parents[1]/'deploy'/'staff-gateway.conf').read_text()
        self.assertIn('auth_request /_swccg_operations_staff_check;',conf)
        self.assertIn('internal;',conf);self.assertIn('proxy_pass_request_headers off;',conf)
