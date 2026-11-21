"""Loopback-only nginx contract test using synthetic verifier and aggregate response."""
import http.server
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error


class Check(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        cookie=self.headers.get('Cookie')
        code={'fixture=staff':204,'fixture=stale':403,'fixture=outage':503,'fixture=unexpected':200}.get(cookie,401)
        self.send_response(code);self.end_headers()
    def log_message(self,*a):pass


class Data(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        valid=(self.headers.get('X-SWCCG-Staff-Decision')=='204' and self.headers.get('Cookie') is None and self.headers.get('Host')=='localhost')
        self.send_response(200 if valid else 403);self.end_headers();self.wfile.write(b'fixture dashboard' if valid else b'denied')
    def log_message(self,*a):pass


def freeport():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def run(staging):
    staging=Path(staging); port=freeport()
    check=http.server.HTTPServer(('127.0.0.1',0),Check);data=http.server.HTTPServer(('127.0.0.1',0),Data)
    for server in (check,data):threading.Thread(target=server.serve_forever,daemon=True).start()
    snippet=(Path(__file__).parent/'staff-gateway.conf').read_text().replace('127.0.0.1:17031',f'127.0.0.1:{check.server_port}').replace('http://unix:/run/swccg-operations-gateway/dashboard.sock:/',f'http://127.0.0.1:{data.server_port}/')
    conf=staging/'fixture-nginx.conf'
    conf.write_text(f'pid {staging}/nginx.pid; error_log {staging}/nginx-error.log; events {{worker_connections 32;}} http {{ map $host $swccg_csp_ro {{ default ""; }} map $host $swccg_csp_ro_ep {{ default ""; }} access_log off; client_body_temp_path {staging}/body; proxy_temp_path {staging}/proxy; server {{listen 127.0.0.1:{port}; {snippet} }} }}')
    subprocess.run(['nginx','-t','-c',str(conf),'-p',str(staging)],check=True,capture_output=True)
    proc=subprocess.Popen(['nginx','-c',str(conf),'-p',str(staging),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        def request(path,cookie=None,method='GET'):
            headers={'X-SWCCG-Staff-Decision':'204'} # forged client header must be stripped
            if cookie:headers['Cookie']=cookie
            req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,headers=headers,method=method)
            try:
                with urllib.request.urlopen(req,timeout=3) as r:return r.status,r.read()
            except urllib.error.HTTPError as e:return e.code,e.read()
        for _ in range(20):
            try:request('/admin/operations/');break
            except OSError:time.sleep(.2)
        expected=[(None,403),('fixture=stale',403),('fixture=staff',200),('fixture=outage',500),('fixture=unexpected',403)]
        for cookie,status in expected:
            got,body=request('/admin/operations/',cookie);assert got==status,(cookie,got,status)
            assert (b'fixture dashboard' in body)==(status==200)
        assert request('/_swccg_operations_staff_check','fixture=staff')[0]==404
        assert request('/admin/operations/snapshot.json','fixture=staff')[0]==404
        assert request('/admin/operations/','fixture=staff','POST')[0]==403
        print('PASS: anonymous, stale, authorized, outage, unexpected 200, forged header, internal route, extra path, POST')
    finally:
        proc.terminate();proc.wait(timeout=10);check.shutdown();data.shutdown();check.server_close();data.server_close()


if __name__=='__main__':
    import sys
    run(sys.argv[1])
