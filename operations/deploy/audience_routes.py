"""Prepare exact production-only nginx route edits; operator validates/reloads."""
from pathlib import Path
import argparse

INCLUDE='    include /etc/nginx/snippets/swccg-operations-audience-route.conf;\n'
DEFAULT_LOG='    access_log /var/log/nginx/access.log;\n'


def replace_once(text,old,new):
    if text.count(old)!=1:raise ValueError('route anchor changed; review exact source')
    return text.replace(old,new,1)


def prepare(paths):
    result={}
    for name,text in paths.items():
        if 'swccg-operations-audience-route.conf' in text:raise ValueError('already installed')
        if name=='swccg.com':
            old='    location / {\n        try_files $uri $uri/ =404;\n'
            new='    location / {\n        access_log /var/log/nginx/access.log;\n        include /etc/nginx/snippets/swccg-operations-audience-route.conf;\n        try_files $uri $uri/ =404;\n'
            result[name]=replace_once(text,old,new)
        elif name=='swccg-267-apex.conf':
            old='location /play/ {\n    proxy_pass http://127.0.0.1:17001/gemp-swccg/;\n'
            result[name]=replace_once(text,old,'location /play/ {\n'+DEFAULT_LOG+INCLUDE+'    proxy_pass http://127.0.0.1:17001/gemp-swccg/;\n')
        elif name=='swccg-267-wiki.conf':
            old='location /wiki/ { include /etc/nginx/snippets/swccg-hsts.conf;\n    include /etc/nginx/snippets/swccg-267-wiki-guard.conf;\n    access_log /var/log/nginx/access.log wiki_rid if=$wiki_log_main;  # #631 rid\n    access_log /var/log/nginx/wiki-rcl-blocked.log combined if=$wiki_block_rcl;\n'
            t=replace_once(text,old,old+INCLUDE)
            old='location @wiki_live_app { include /etc/nginx/snippets/swccg-hsts.conf;\n'
            result[name]=replace_once(t,old,old+DEFAULT_LOG+INCLUDE)
        elif name=='swccg-267-forum.conf':
            old='    access_log /var/log/nginx/access.log forum_noquery;\n'
            result[name]=replace_once(text,old,old+INCLUDE)
        else:raise ValueError('unsupported route')
    if len(result)!=4:raise ValueError('incomplete route set')
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    files=[Path('/etc/nginx/sites-available/swccg.com')]+[Path('/etc/nginx/snippets')/n for n in ('swccg-267-apex.conf','swccg-267-wiki.conf','swccg-267-forum.conf')]
    texts={f.name:f.read_text() for f in files};new=prepare(texts)
    for n,t in new.items():
        with (a.output/n).open('x') as f:f.write(t)


if __name__=='__main__':main()
