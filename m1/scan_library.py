"""M1 step 1: read-only library tag scan -> JSON lines. Never writes into the music tree.
Usage (inside container, music mounted read-only at /music):
  python scan_library.py /music /out/library_scan.jsonl [--limit N]
mutagen facts used: FLAC .info.{length,sample_rate,bits_per_sample,channels}, .pictures; Vorbis comments are
case-insensitive lists. MP4 .info.{length,bitrate,sample_rate,bits_per_sample,codec}; tags are atom keys
('\xa9nam', '----:com.apple.iTunes:ISRC' as bytes lists, 'covr')."""
import json, os, sys, time
from mutagen import File
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

AUDIO = {".flac", ".m4a", ".mp3", ".opus", ".ogg", ".wav"}
root, out = sys.argv[1], sys.argv[2]
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

def first(tags, *keys):
    for k in keys:
        for kk in (k, k.upper(), k.lower()):
            v = tags.get(kk)
            if v:
                v = v[0] if isinstance(v, (list, tuple)) else v
                return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    return None

def rec(path, rel):
    st = os.stat(path)
    r = dict(path=rel, size=st.st_size, mtime=int(st.st_mtime), ext=os.path.splitext(path)[1].lower())
    try:
        if r["ext"] == ".flac":
            a = FLAC(path); t = a.tags or {}
            r.update(codec="flac", dur=round(a.info.length, 3), sr=a.info.sample_rate, bits=a.info.bits_per_sample,
                     ch=a.info.channels, art=bool(a.pictures))
            r.update(title=first(t, "title"), artist=first(t, "artist"), albumartist=first(t, "albumartist", "album artist"),
                     album=first(t, "album"), track=first(t, "tracknumber"), disc=first(t, "discnumber"),
                     date=first(t, "date", "year"), isrc=first(t, "isrc"), mb_track=first(t, "musicbrainz_trackid"),
                     mb_album=first(t, "musicbrainz_albumid"), mb_artist=first(t, "musicbrainz_artistid"))
        elif r["ext"] == ".m4a":
            a = MP4(path); t = a.tags or {}
            r.update(codec=str(a.info.codec), dur=round(a.info.length, 3), sr=a.info.sample_rate,
                     bits=a.info.bits_per_sample, br=a.info.bitrate, ch=a.info.channels, art="covr" in t)
            r.update(title=first(t, "\xa9nam"), artist=first(t, "\xa9ART"), albumartist=first(t, "aART"), album=first(t, "\xa9alb"),
                     track=(f"{t['trkn'][0][0]}/{t['trkn'][0][1]}" if t.get("trkn") else None),
                     disc=(f"{t['disk'][0][0]}/{t['disk'][0][1]}" if t.get("disk") else None), date=first(t, "\xa9day"),
                     isrc=first(t, "----:com.apple.iTunes:ISRC"), mb_track=first(t, "----:com.apple.iTunes:MusicBrainz Track Id"))
        else:
            a = File(path, easy=True); t = a.tags or {} if a else {}
            r.update(codec=a.__class__.__name__ if a else None, dur=round(a.info.length, 3) if a else None,
                     title=first(t, "title"), artist=first(t, "artist"), albumartist=first(t, "albumartist"), album=first(t, "album"),
                     track=first(t, "tracknumber"), date=first(t, "date"), isrc=first(t, "isrc"))
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return r

n = bad = 0; t0 = time.time()
with open(out, "w") as f:
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if name.startswith("._") or os.path.splitext(name)[1].lower() not in AUDIO: continue
            p = os.path.join(d, name); rr = rec(p, os.path.relpath(p, root))
            f.write(json.dumps(rr, ensure_ascii=False) + "\n"); n += 1; bad += "error" in rr
            if n % 500 == 0: print(f"{n} files, {bad} errors, {time.time()-t0:.0f}s", flush=True)
            if limit and n >= limit: break
        if limit and n >= limit: break
print(f"DONE {n} files, {bad} errors, {time.time()-t0:.0f}s")
