"""Generate the isolated, local-only gateway listener; no auth changes by itself."""
from .reader_unit import render_reader_unit


def render_gateway(code_dir,store_dir,peer_uid=33,peer_group='www-data'):
    if type(peer_uid) is not int or peer_uid<=0 or peer_group!='www-data':raise ValueError('invalid gateway peer')
    text=render_reader_unit(code_dir,store_dir)
    return (text.replace('SWCCG private read-only dashboard Unix socket','SWCCG verified staff gateway Unix socket')
        .replace('Group=swccg-operations-read','Group=www-data\nSupplementaryGroups=swccg-operations-read')
        .replace('/run/swccg-operations-reader','/run/swccg-operations-gateway')
        .replace('RuntimeDirectory=swccg-operations-reader','RuntimeDirectory=swccg-operations-gateway')
        .replace('RuntimeDirectoryMode=0700','RuntimeDirectoryMode=0750')
        .replace('dashboard.sock\n','dashboard.sock --gateway-peer-uid '+str(peer_uid)+'\n'))
