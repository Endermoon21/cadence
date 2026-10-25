"""Spike 1: how complete is tidalapi for an artist, and do we get ISRC/UPC/duration?
Usage: s1_catalog.py <artist name> [tidal_artist_id]
Read-only. Writes out/<artist>.json. Throttled (0.4s between calls) so it can't trip rate limits."""
import json, os, sys, time, tidalapi
t = json.load(open(os.path.expanduser("~/.config/tidal_dl_ng/token.json")))
g = lambda k: t[k]["data"] if isinstance(t[k], dict) else t[k]
s = tidalapi.Session(); s.load_oauth_session(g("token_type"), g("access_token"), g("refresh_token"))
name = sys.argv[1]
calls = 0
def nap():
    global calls; calls += 1; time.sleep(0.4)
if len(sys.argv) > 2: a = tidalapi.artist.Artist(s, int(sys.argv[2]))
else: a = [x for x in s.search(name, models=[tidalapi.artist.Artist], limit=5)["artists"] if x.name.lower() == name.lower()][0]
print("artist", a.id, a.name)
def page(fn):
    out, off = [], 0
    while True:
        batch = fn(limit=50, offset=off); nap()
        out += batch
        if len(batch) < 50: return out
        off += 50
buckets = {"albums": page(a.get_albums), "ep_singles": page(a.get_ep_singles), "other": page(a.get_other)}
rel = {}
for b, items in buckets.items():
    for al in items: rel[al.id] = (b, al)
rows = []
for rid, (b, al) in sorted(rel.items(), key=lambda kv: str(kv[1][1].release_date)):
    tr = al.tracks(); nap()
    rows.append(dict(bucket=b, id=al.id, title=al.name, version=getattr(al, "version", None), type=al.type, upc=al.upc,
        date=str(al.release_date)[:10], explicit=al.explicit, n=al.num_tracks, available=al.available,
        artists=[x.name for x in al.artists],
        tracks=[dict(id=x.id, isrc=x.isrc, title=x.name, version=x.version, dur=x.duration, n=x.track_num, disc=x.volume_num,
                     explicit=x.explicit, artists=[y.name for y in x.artists]) for x in tr]))
json.dump(rows, open(os.path.expanduser(f"~/music-manager/spikes/out/{a.name}.json"), "w"), indent=1)
miss_isrc = sum(1 for r in rows for x in r["tracks"] if not x["isrc"]); total = sum(len(r["tracks"]) for r in rows)
print(f"{len(rows)} releases, {total} tracks, missing ISRC {miss_isrc}, missing UPC {sum(1 for r in rows if not r['upc'])}, api calls {calls}")
for r in rows: print(r["date"], f"{r['bucket']:10}", f"{str(r['type']):6}", f"{r['n']:>2}", r["title"], f"[{r['version']}]" if r["version"] else "", "" if r["available"] else "UNAVAILABLE")
