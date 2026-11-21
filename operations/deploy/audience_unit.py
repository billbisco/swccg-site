from .render_units import HARDENING,linux_path


def render_audience(code_dir,state_dir):
    code_dir,state_dir=linux_path(code_dir),linux_path(state_dir)
    return f'''[Unit]
Description=SWCCG local aggregate-only audience receiver
Before=nginx.service
RequiresMountsFor={code_dir} {state_dir}
[Service]
Type=simple
User=swccg-ops-audience
Group=www-data
RuntimeDirectory=swccg-ops-audience
RuntimeDirectoryMode=0750
ProtectHome=yes
ReadWritePaths={state_dir} /run/swccg-ops-audience
ExecStart=/usr/bin/python3 -I -B {code_dir}/operations/deploy/site_audience.py --state {state_dir} --socket /run/swccg-ops-audience/traffic.sock --peer-uid 33
Restart=on-failure
RestartSec=10s
{HARDENING.replace('MemoryMax=100M','MemoryMax=64M').replace('CPUQuota=100%','CPUQuota=25%')}
[Install]
WantedBy=multi-user.target
'''
