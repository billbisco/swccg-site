# swccg.com — how to put the site back together

Fan encyclopedia and play stack for [swccg.com](https://swccg.com). Not Lucasfilm, not the Players Committee.

If the VPS is gone, start here. Clone these public GitHub repos (all under [billbisco](https://github.com/billbisco)):

| Repo | What it is |
| --- | --- |
| **[swccg-site](https://github.com/billbisco/swccg-site)** (this repo) | Map, nginx snippet, how to assemble |
| **[swccg-gemp](https://github.com/billbisco/swccg-gemp)** | Play engine (Test `env/test`, Play `master`) |
| **[swccg-wiki](https://github.com/billbisco/swccg-wiki)** | Encyclopedia wikitext (`pages/` + `INDEX.tsv`) |
| **[swccg-wiki-files](https://github.com/billbisco/swccg-wiki-files)** | Wiki `File:` uploads (scans, rulebook art, PDFs) |
| **[swccg-holotable](https://github.com/billbisco/swccg-holotable)** | Mirror of [swccgpc/holotable](https://github.com/swccgpc/holotable) card faces |

Card faces are **mirrored**, then served from **https://swccg.com/cards/** so Play/Wiki do not hotlink `res.starwarsccg.org`. Pull holotable when the PC adds a virtual set:

```bash
cd /opt/swccg-holotable && git fetch origin master && git reset --hard origin/master
```

Upstream stays [swccgpc/holotable](https://github.com/swccgpc/holotable). This fork is the swccg.com copy.

## Rebuild order

1. Provision a Linux box. Install Docker, nginx, certbot, git, MediaWiki (see `swccg-wiki` README).
2. Clone `swccg-holotable` to `/opt/swccg-holotable`. Run `sudo bash apply-swccg-res.sh` from this repo so nginx serves `/cards/`, `/gemp/`, `/packs/`, `/rules/`, `/social/` on `swccg.com`.
3. Import wiki wikitext (`python tools/import_pages.py` / `apply-tsv.sh`) and `importImages` from `swccg-wiki-files`.
4. Clone `swccg-gemp`, copy `.env.example` → `.env` (secrets stay off GitHub), `docker compose up`. Point DNS: `play.swccg.com`, `test.swccg.com`, `wiki.swccg.com`, `swccg.com`.
5. GEMP card URLs rewrite `https://res.starwarsccg.org` → `https://swccg.com` at runtime (`Card.getImageUrl`). Confirm a card gif: `https://swccg.com/cards/Premiere-Light/large/lukeskywalker.gif`

## What stays off public GitHub

Live `.env`, Admin passwords, SSH keys, HANDOFF with host internals, dest-note identity dumps. Use `.env.example`.

## License

Fan encyclopedia. Respective rights belong to their owners. Star Wars, SWCCG, card names, card text, and related marks remain with Lucasfilm Ltd., Disney, Decipher, and/or the Players Committee.
