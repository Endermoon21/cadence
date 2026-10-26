from cadence.fetch.plan import plan_missing
from tests.fetch_fakes import add_artist, add_file, add_recording, add_release, add_track, ledger


def titles(items):
    return [i.title for i in items]


def seed():
    conn = ledger()
    a = add_artist(conn)
    rel = add_release(conn, a, "Harvest", 100)
    return conn, a, rel


def test_owned_clusters_are_skipped_even_when_owned_through_another_member():
    conn, a, rel = seed()
    r1, r2, r3 = (add_recording(conn, a, t) for t in ("One", "Two", "Three"))
    conn.execute("UPDATE recording SET canon_id = ? WHERE id = ?", (r2, r1))  # r1 is a member of r2's cluster
    conn.execute("UPDATE recording SET canon_id = ? WHERE id = ?", (r2, r2))
    for n, (r, t) in enumerate([(r1, "One"), (r2, "Two"), (r3, "Three")], 1):
        add_track(conn, rel, r, n, t, 1000 + n)
    add_file(conn, "x/y/01.flac", a, r1)  # owns the cluster {r1, r2} through its non-canonical member
    assert titles(plan_missing(conn)) == ["Three"]


def test_variants_only_with_include_variants():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    add_track(conn, rel, add_recording(conn, a, "Song (Instrumental)", variant="instrumental"), 2,
              "Song (Instrumental)", 2)
    assert titles(plan_missing(conn)) == ["Song"]
    assert titles(plan_missing(conn, include_variants=True)) == ["Song", "Song (Instrumental)"]


def test_canonical_recordings_track_is_chosen_over_the_original_and_compilations():
    conn, a, _ = seed()
    orig = add_recording(conn, a, "Heart of Gold")
    remaster = add_recording(conn, a, "Heart of Gold (2009 Remaster)")
    for rid in (orig, remaster):
        conn.execute("UPDATE recording SET canon_id = ? WHERE id = ?", (remaster, rid))
    old = add_release(conn, a, "Harvest", 101, date="1972-02-01")
    new = add_release(conn, a, "Harvest (2009 Remaster)", 102, date="2009-01-01")
    comp = add_release(conn, a, "Greatest Hits", 103, type="compilation", date="1980-01-01")
    add_track(conn, old, orig, 5, "Heart of Gold", 501)
    add_track(conn, comp, remaster, 1, "Heart of Gold (2009 Remaster)", 503)
    add_track(conn, new, remaster, 5, "Heart of Gold (2009 Remaster)", 502)
    [item] = plan_missing(conn)
    assert (item.tidal_track_id, item.recording_id, item.release, item.tidal_release_id) == (502, remaster, "Harvest (2009 Remaster)", 102)
    assert item.target_rel == "Neil Young/Harvest (2009 Remaster)/05 - Heart of Gold (2009 Remaster).flac"
    assert plan_missing(conn, ext=".m4a")[0].target_rel.endswith(".m4a")


def test_item_fields_and_unusable_tracks():
    conn, a, rel = seed()
    rec = add_recording(conn, a, "Old Man", isrc="USRE17200012", dur=204)
    add_track(conn, rel, rec, 4, "Old Man", 7, dur=203, disc=2, version="Live")
    add_track(conn, rel, add_recording(conn, a, "No Tidal id"), 5, "No Tidal id", None)
    unavailable = add_track(conn, rel, add_recording(conn, a, "Gone"), 6, "Gone", 8)
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (unavailable,))
    [i] = plan_missing(conn)
    assert (i.artist, i.release, i.disc, i.number, i.title, i.tidal_track_id, i.duration_s, i.isrc, i.dest) == \
        ("Neil Young", "Harvest", 2, 4, "Old Man (Live)", 7, 203, "USRE17200012", "server")
    assert i.target_rel == "Neil Young/Harvest/04 - Old Man (Live).flac"


def test_artists_without_a_resolved_tidal_match_are_skipped():
    conn = ledger()
    for name, match in (("Pending", "pending"), ("Ambiguous", "ambiguous"), ("Manual", "manual")):
        a = add_artist(conn, name, tidal_id={"pending": 11, "ambiguous": 12, "manual": 13}[match], match=match)
        add_track(conn, add_release(conn, a, f"{name} LP", a), add_recording(conn, a, f"{name} Song"), 1,
                  f"{name} Song", 100 + a)
    assert titles(plan_missing(conn)) == ["Manual Song"]


def test_scope_by_artist_and_release_and_destination():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "A"), 1, "A", 1)
    other = add_release(conn, a, "After the Gold Rush", 200, date="1970-01-01")
    add_track(conn, other, add_recording(conn, a, "B"), 1, "B", 2)
    b = add_artist(conn, "Someone Else", tidal_id=2)
    add_track(conn, add_release(conn, b, "Elsewhere", 300), add_recording(conn, b, "C"), 1, "C", 3)
    assert titles(plan_missing(conn, artist_id=a)) == ["B", "A"]  # ordered by release date
    assert titles(plan_missing(conn, release_id=other)) == ["B"]
    conn.execute("INSERT INTO want(artist_id, dest) VALUES (?, 'both')", (a,))
    conn.execute("INSERT INTO want(release_id, dest) VALUES (?, 'fiio')", (other,))
    assert {i.title: i.dest for i in plan_missing(conn)} == {"A": "both", "B": "fiio", "C": "server"}
