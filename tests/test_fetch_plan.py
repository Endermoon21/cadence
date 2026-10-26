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
    assert titles(plan_missing(conn, scope="all")) == ["Three"]


def test_variants_only_with_include_variants():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    add_track(conn, rel, add_recording(conn, a, "Song (Instrumental)", variant="instrumental"), 2,
              "Song (Instrumental)", 2)
    assert titles(plan_missing(conn, scope="all")) == ["Song"]
    assert titles(plan_missing(conn, include_variants=True, scope="all")) == ["Song", "Song (Instrumental)"]


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
    [item] = plan_missing(conn, scope="all")
    assert (item.tidal_track_id, item.recording_id, item.release, item.tidal_release_id) == (502, remaster, "Harvest", 102)
    assert item.target_rel == "Neil Young/Harvest/05 - Heart of Gold.flac"
    assert plan_missing(conn, ext=".m4a", scope="all")[0].target_rel.endswith(".m4a")


def test_item_fields_and_unusable_tracks():
    conn, a, rel = seed()
    rec = add_recording(conn, a, "Old Man", isrc="USRE17200012", dur=204)
    add_track(conn, rel, rec, 4, "Old Man", 7, dur=203, disc=2, version="Live")
    add_track(conn, rel, add_recording(conn, a, "No Tidal id"), 5, "No Tidal id", None)
    unavailable = add_track(conn, rel, add_recording(conn, a, "Gone"), 6, "Gone", 8)
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (unavailable,))
    [i] = plan_missing(conn, scope="all")
    assert (i.artist, i.release, i.disc, i.number, i.title, i.tidal_track_id, i.duration_s, i.isrc, i.dest) == \
        ("Neil Young", "Harvest", 2, 4, "Old Man (Live)", 7, 203, "USRE17200012", "server")
    assert i.target_rel == "Neil Young/Harvest/D2-04 - Old Man (Live).flac"  # the release has discs 1 and 2


def test_artists_without_a_resolved_tidal_match_are_skipped():
    conn = ledger()
    for name, match in (("Pending", "pending"), ("Ambiguous", "ambiguous"), ("Manual", "manual")):
        a = add_artist(conn, name, tidal_id={"pending": 11, "ambiguous": 12, "manual": 13}[match], match=match)
        add_track(conn, add_release(conn, a, f"{name} LP", a), add_recording(conn, a, f"{name} Song"), 1,
                  f"{name} Song", 100 + a)
    assert titles(plan_missing(conn, scope="all")) == ["Manual Song"]


def test_scope_by_artist_and_release_and_destination():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "A"), 1, "A", 1)
    other = add_release(conn, a, "After the Gold Rush", 200, date="1970-01-01")
    add_track(conn, other, add_recording(conn, a, "B"), 1, "B", 2)
    b = add_artist(conn, "Someone Else", tidal_id=2)
    add_track(conn, add_release(conn, b, "Elsewhere", 300), add_recording(conn, b, "C"), 1, "C", 3)
    assert titles(plan_missing(conn, artist_id=a, scope="all")) == ["B", "A"]  # ordered by release date
    assert titles(plan_missing(conn, release_id=other)) == ["B"]
    conn.execute("INSERT INTO want(artist_id, dest) VALUES (?, 'both')", (a,))
    conn.execute("INSERT INTO want(release_id, dest) VALUES (?, 'fiio')", (other,))
    assert {i.title: i.dest for i in plan_missing(conn, scope="all")} == {"A": "both", "B": "fiio", "C": "server"}


# ---- scope, folder naming, near-duplicates ----

from cadence.fetch.plan import strip_markers  # noqa: E402


def test_strip_markers_only_removes_remaster_and_edition_wording():
    assert strip_markers("I've Been Waiting for You (2009 Remaster)") == "I've Been Waiting for You"
    assert strip_markers("Round & Round (It Won't Be Long) [2009 Remaster]") == "Round & Round (It Won't Be Long)"
    assert strip_markers("Heart of Gold - Remastered 2009") == "Heart of Gold"
    assert strip_markers("Decade (2017 Remaster)", release=True) == "Decade"
    assert strip_markers("After The Gold Rush (50th Anniversary)", release=True) == "After The Gold Rush"
    assert strip_markers("Old Man (Live at Massey Hall)") == "Old Man (Live at Massey Hall)"
    assert strip_markers("(Don't Fear) The Reaper") == "(Don't Fear) The Reaper"
    assert strip_markers("Remastered") == "Remastered"  # never an empty name


def scoped():
    """Neil Young: Decade (partly owned, existing folder) and Lonely Album (nothing owned)."""
    conn = ledger()
    a = add_artist(conn)
    old = add_release(conn, a, "Decade", 10, date="1977-10-28")
    new = add_release(conn, a, "Decade (2017 Remaster)", 11, date="2017-01-01")
    conn.execute("UPDATE release SET group_key = 'album|decade' WHERE id IN (?, ?)", (old, new))
    have = add_recording(conn, a, "Sugar Mountain")
    want = add_recording(conn, a, "Cowgirl in the Sand (2017 Remaster)", dur=600)
    add_track(conn, old, have, 1, "Sugar Mountain", 101)
    add_track(conn, new, have, 1, "Sugar Mountain", 111)
    add_track(conn, new, want, 2, "Cowgirl in the Sand", 112, version="2017 Remaster", dur=600)
    add_file(conn, "Neil Young/Decade/01 - Sugar Mountain.flac", a, have)
    lonely = add_release(conn, a, "Lonely Album: Part 1 (Deluxe Edition)", 20, date="2020-01-01")
    add_track(conn, lonely, add_recording(conn, a, "Nobody Home (2020 Remaster)"), 3, "Nobody Home", 201,
              version="2020 Remaster")
    conn.commit()
    return conn, a


def test_default_scope_is_partial_only():
    conn, a = scoped()
    assert [i.title for i in plan_missing(conn)] == ["Cowgirl in the Sand"]
    assert [i.title for i in plan_missing(conn, scope="partial")] == ["Cowgirl in the Sand"]
    assert sorted(i.title for i in plan_missing(conn, scope="all")) == ["Cowgirl in the Sand", "Nobody Home"]
    [explicit] = plan_missing(conn, release_id=conn.execute("SELECT id FROM release WHERE title LIKE 'Lonely%'").fetchone()[0])
    assert explicit.title == "Nobody Home"  # asking for a release by id is explicit


def test_an_existing_library_folder_of_the_release_group_is_reused():
    conn, a = scoped()
    [i] = plan_missing(conn)
    assert (i.album_dir, i.release) == ("Neil Young/Decade", "Decade")
    assert i.target_rel == "Neil Young/Decade/02 - Cowgirl in the Sand.flac"  # not "Decade (2017 Remaster)"
    # the exact existing spelling wins, even an odd one
    conn.execute("UPDATE file SET path = 'Neil Young/DECADE/01 - Sugar Mountain.flac'")
    assert plan_missing(conn)[0].album_dir == "Neil Young/DECADE"


def test_a_new_album_gets_the_cleaned_sanitised_name():
    conn, a = scoped()
    lonely = [i for i in plan_missing(conn, scope="all") if i.title == "Nobody Home"][0]
    assert lonely.release == "Lonely Album: Part 1"            # tag keeps the real name
    assert lonely.album_dir == "Neil Young/Lonely Album - Part 1"  # folder is exFAT-safe, edition marker gone
    assert lonely.target_rel == "Neil Young/Lonely Album - Part 1/03 - Nobody Home.flac"


def test_near_duplicate_of_an_owned_file_is_skipped_and_reported():
    conn = ledger()
    a = add_artist(conn)
    hh = add_release(conn, a, "Hitchhiker", 30)
    owned = add_recording(conn, a, "Hitchhiker", dur=277)
    twin = add_recording(conn, a, "Hitchhiker (Like an Inca #1)", dur=277)
    other = add_recording(conn, a, "Campaigner", dur=259)
    add_track(conn, hh, owned, 7, "Hitchhiker", 701, dur=277)
    add_track(conn, hh, twin, 8, "Hitchhiker (Like an Inca #1)", 702, dur=277)
    add_track(conn, hh, other, 9, "Campaigner", 703, dur=259)
    add_file(conn, "Neil Young/Hitchhiker/07 - Hitchhiker.flac", a, owned)
    conn.execute("UPDATE file SET tag_title = 'Hitchhiker', duration_s = 277.387")
    skipped = []
    assert titles(plan_missing(conn, skipped=skipped)) == ["Campaigner"]
    [(item, path)] = skipped
    assert (item.title, path) == ("Hitchhiker (Like an Inca #1)", "Neil Young/Hitchhiker/07 - Hitchhiker.flac")
    # a different duration is a different track
    conn.execute("UPDATE track SET duration_s = 300 WHERE tidal_id = 702")
    assert titles(plan_missing(conn)) == ["Hitchhiker (Like an Inca #1)", "Campaigner"]


# ---- discs ----

def two_discs():
    conn, a, rel = seed()
    r1, r2 = (add_recording(conn, a, "Intro") for _ in range(2))
    add_track(conn, rel, r1, 1, "Intro", 1, disc=1)
    add_track(conn, rel, r2, 1, "Intro", 2, disc=2)
    return conn, a, rel, r1, r2


def test_same_numbered_same_titled_tracks_on_two_discs_get_distinct_targets():
    conn, *_ = two_discs()
    items = plan_missing(conn, scope="all")
    assert [(i.disc, i.target_rel) for i in items] == [(1, "Neil Young/Harvest/D1-01 - Intro.flac"),
                                                       (2, "Neil Young/Harvest/D2-01 - Intro.flac")]
    assert plan_missing(conn, ext=".m4a", scope="all")[1].target_rel == "Neil Young/Harvest/D2-01 - Intro.m4a"


def test_single_disc_names_are_unchanged():
    conn, a, rel = seed()
    for n in (1, 2):
        add_track(conn, rel, add_recording(conn, a, f"Song {n}"), n, f"Song {n}", n)
    assert [i.target_rel for i in plan_missing(conn, scope="all")] == ["Neil Young/Harvest/01 - Song 1.flac",
                                                                       "Neil Young/Harvest/02 - Song 2.flac"]


def test_disc_count_covers_tracks_that_are_not_fetchable_or_not_missing():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, r1)  # disc 1 is owned under the old single-disc name
    conn.execute("UPDATE track SET tidal_id = NULL WHERE disc = 1")
    # the owned cluster is not planned, so the old name is never claimed again; disc 2 gets its own disc-prefixed name
    assert [i.target_rel for i in plan_missing(conn, scope="all")] == ["Neil Young/Harvest/D2-01 - Intro.flac"]
