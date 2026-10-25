#!/usr/bin/env python3
"""Spike 4: YouTube Music as gap-finder. Read-only, unauthenticated, ~1 request/s.

usage: s4_ytm_catalog.py "<artist>" <out.json>
Dumps the artist's YT Music albums + singles (with per-release track lists).
"""
import json, sys, time
from ytmusicapi import YTMusic

GAP = 1.0
artist, out = sys.argv[1], sys.argv[2]
yt = YTMusic()


def pause():
    time.sleep(GAP)


def full_grid(browse_id, params, kind):
    """ytmusicapi 1.12.3's get_artist_albums posts the channel id, which no longer returns the grid;
    the section's own MPAD... browse id does. Parse the grid items directly."""
    r = yt._send_request("browse", {"browseId": browse_id, "params": params})
    tab = r["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]
    grid = tab["contents"][0]["gridRenderer"]
    if grid.get("continuations"):
        print("WARNING: grid has continuations; list may be longer than parsed")
    out = []
    for it in grid["items"]:
        m = it["musicTwoRowItemRenderer"]
        runs = [x["text"] for x in m["subtitle"]["runs"]]
        year = next((x for x in runs if x.isdigit() and len(x) == 4), None)
        out.append({"title": m["title"]["runs"][0]["text"], "year": year,
                    "type": runs[0] if runs and not runs[0].isdigit() else kind[:-1].title(),
                    "browseId": m["title"]["runs"][0]["navigationEndpoint"]["browseEndpoint"]["browseId"]})
    return out


cands = yt.search(artist, filter="artists", limit=5)
pause()
pick = next((c for c in cands if c["artist"].casefold() == artist.casefold()), cands[0] if cands else None)
if not pick:
    sys.exit("no artist match")
cid = pick["browseId"]
print("artist:", pick["artist"], cid, "| candidates:", [(c["artist"], c["browseId"]) for c in cands])

page = yt.get_artist(cid)
pause()
releases = []
for kind in ("albums", "singles"):
    sec = page.get(kind)
    if not sec:
        continue
    items = sec["results"]
    if sec.get("params"):  # the page only shows a teaser; the full list lives on the section's own browse page
        items = full_grid(sec["browseId"], sec["params"], kind)
        print(kind, "teaser", len(sec["results"]), "-> full", len(items))
        pause()
    for it in items:
        releases.append({"bucket": kind, "title": it["title"], "year": it.get("year"),
                         "type": it.get("type"), "browseId": it["browseId"]})

for r in releases:
    try:
        a = yt.get_album(r["browseId"])
    except Exception as e:  # keep going; record the failure
        r["error"] = repr(e)
        pause()
        continue
    r["artists"] = [x["name"] for x in a.get("artists", [])]
    r["trackCount"] = a.get("trackCount")
    r["tracks"] = [{"title": t["title"], "dur": t.get("duration_seconds"), "videoId": t.get("videoId"),
                    "available": t.get("isAvailable"), "explicit": t.get("isExplicit"),
                    "artists": [x["name"] for x in (t.get("artists") or [])]} for t in a.get("tracks", [])]
    pause()

json.dump({"artist": pick["artist"], "channelId": cid, "releases": releases}, open(out, "w"), indent=1, ensure_ascii=False)
print("releases:", len(releases), "->", out)
