# Include inside the swccg.com HTTPS server block, before location /.
# Card faces: holotable Images-HT/starwars  →  /cards/
# GEMP chrome copied once from the PC CDN   →  /gemp/ /packs/ /rules/ /social/

location /cards/ {
    alias /opt/swccg-holotable/Images-HT/starwars/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /gemp/ {
    alias /opt/swccg-res/gemp/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /packs/ {
    alias /opt/swccg-res/packs/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /rules/ {
    alias /opt/swccg-res/rules/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /social/ {
    alias /opt/swccg-res/social/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /parsecs/ {
    alias /opt/swccg-res/parsecs/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location /wp/ {
    alias /opt/swccg-res/wp/;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location = /swccg_gold64.png {
    alias /opt/swccg-res/swccg_gold64.png;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location = /cards/holo.jpg {
    alias /opt/swccg-res/cards/holo.jpg;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location = /cards/rules.png {
    alias /opt/swccg-res/cards/rules.png;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}

location = /cards/carbonite.gif {
    alias /opt/swccg-res/cards/carbonite.gif;
    expires 365d;
    add_header Cache-Control "public, max-age=31536000, immutable";
    access_log off;
}
