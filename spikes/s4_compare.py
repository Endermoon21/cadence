#!/usr/bin/env python3
"""Compare a Tidal dump (s1) with a YT Music dump (s4) for one artist. Offline."""
import json, re, sys

tidal_f, ytm_f, name = sys.argv[1], sys.argv[2], sys.argv[3]
tidal = json.load(open(tidal_f))
ytm = json.load(open(ytm_f))["releases"]


def core(s):
    s = s.casefold()
    s = re.sub(r"\((original mix|album version|remaster(ed)?( \d{4})?|explicit|deluxe.*?)\)", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# own releases only: Tidal's get_other bucket contains appears-on albums
t_own = [r for r in tidal if r["artists"] and r["artists"][0].casefold() == name.casefold()]
y_own = [r for r in ytm if r.get("artists") and r["artists"][0].casefold() == name.casefold()]

t_keys = {(core(r["title"]), len(r["tracks"])) for r in t_own}
y_keys = {(core(r["title"]), len(r.get("tracks", []))) for r in y_own}
t_titles = {core(r["title"]) for r in t_own}
y_titles = {core(r["title"]) for r in y_own}

print(f"Tidal own releases: {len(t_own)} | YT Music own releases: {len(y_own)} (of {len(ytm)} listed)")
print("\nrelease titles ONLY on YT Music:", sorted(y_titles - t_titles) or "none")
print("release titles ONLY on Tidal:   ", sorted(t_titles - y_titles) or "none")
print("same title, different track count:",
      sorted(k for k in (t_keys ^ y_keys) if k[0] in (t_titles & y_titles)) or "none")

# recording level: title match + duration within 3 s
t_tracks = [(core(t["title"]), t["dur"], r["title"]) for r in t_own for t in r["tracks"]]
y_tracks = [(core(t["title"]), t["dur"], r["title"], t.get("available")) for r in y_own for t in r.get("tracks", [])]


def hit(title, dur, pool):
    return any(title == p[0] and p[1] is not None and dur is not None and abs(p[1] - dur) <= 3 for p in pool)


y_only = sorted({(t, d, rel) for t, d, rel, _ in y_tracks if not hit(t, d, t_tracks)})
t_only = sorted({(t, d, rel) for t, d, rel in t_tracks if not hit(t, d, y_tracks)})
print(f"\nrecordings on YT Music with no Tidal match ({len(y_only)}):")
for t, d, rel in y_only:
    print("  ", t, d, "| on:", rel)
print(f"recordings on Tidal with no YT Music match ({len(t_only)}):")
for t, d, rel in t_only:
    print("  ", t, d, "| on:", rel)
print("\nunavailable YT tracks:", [(t, rel) for t, d, rel, a in y_tracks if a is False] or "none")
