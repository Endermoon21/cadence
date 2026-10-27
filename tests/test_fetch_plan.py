import pytest

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


def test_a_linked_file_with_the_identical_title_is_not_a_near_duplicate():
    conn, a, rel = seed()
    owned, two, three = (add_recording(conn, a, "Intro", dur=90) for _ in range(3))
    add_track(conn, rel, owned, 1, "Intro", 801, dur=90)
    add_track(conn, rel, two, 2, "Intro", 802, dur=90)
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, owned)
    conn.execute("UPDATE file SET tag_title = 'Intro', duration_s = 90.2")
    skipped = []
    assert [(i.number, i.title) for i in plan_missing(conn, skipped=skipped)] == [(2, "Intro")]
    assert skipped == []
    add_track(conn, rel, three, 3, "Intro", 803, dur=90)  # a second unowned Intro: both are still planned
    assert [(i.number, i.title) for i in plan_missing(conn)] == [(2, "Intro"), (3, "Intro")]


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


def test_null_num_volumes_with_a_disc_two_row_is_multi_disc_as_before():
    conn, a, rel, r1, r2 = two_discs()
    assert conn.execute("SELECT num_volumes FROM release").fetchone()[0] is None
    assert {i.discs for i in plan_missing(conn, scope="all")} == {2}
    conn.execute("UPDATE track SET available = 0 WHERE disc = 2")  # unavailable and unlinked rows count too
    assert [i.target_rel for i in plan_missing(conn, scope="all")] == ["Neil Young/Harvest/D1-01 - Intro.flac"]
    conn.execute("UPDATE track SET available = NULL, tidal_id = NULL WHERE disc = 2")
    assert [i.target_rel for i in plan_missing(conn, scope="all")] == ["Neil Young/Harvest/D1-01 - Intro.flac"]


def test_volume_count_one_with_a_real_disc_two_row_is_still_multi_disc_so_names_do_not_collide():
    conn, a, rel, r1, r2 = two_discs()
    conn.execute("UPDATE release SET num_volumes = 1")
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all")] == \
        [(2, "Neil Young/Harvest/D1-01 - Intro.flac"), (2, "Neil Young/Harvest/D2-01 - Intro.flac")]


def test_volume_count_one_and_only_disc_one_rows_keeps_single_disc_names():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    for n in (1, None, 0):  # NULL and < 1 are not usable counts
        conn.execute("UPDATE release SET num_volumes = ?", (n,))
        assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all")] == \
            [(1, "Neil Young/Harvest/01 - Song.flac")]
    conn.execute("UPDATE release SET num_volumes = 2")  # Tidal says two discs even though only disc 1 has rows
    assert [i.discs for i in plan_missing(conn, scope="all")] == [2]


@pytest.mark.parametrize("volumes", [None, 0, -1])
def test_unusable_volume_counts_give_the_same_disc_count_as_null(volumes):
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    conn.execute("UPDATE release SET num_volumes = ?", (volumes,))
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all")] == \
        [(1, "Neil Young/Harvest/01 - Song.flac")]
    add_track(conn, rel, add_recording(conn, a, "Bonus"), 1, "Bonus", 2, disc=2)  # a real disc-2 row still prefixes
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all")] == \
        [(2, "Neil Young/Harvest/D1-01 - Song.flac"), (2, "Neil Young/Harvest/D2-01 - Bonus.flac")]


def test_the_disc_floor_is_per_release_so_a_sibling_with_a_disc_two_row_does_not_prefix_another_release():
    conn, a, x = seed()
    add_track(conn, x, add_recording(conn, a, "X1"), 1, "X1", 1)
    add_track(conn, x, add_recording(conn, a, "X2"), 1, "X2", 2, disc=2)
    y = add_release(conn, a, "After the Gold Rush", 200, date="1970-01-01")  # num_volumes NULL, disc 1 only
    add_track(conn, y, add_recording(conn, a, "Y1"), 1, "Y1", 3)
    assert {i.title: (i.discs, i.target_rel) for i in plan_missing(conn, scope="all")} == {
        "X1": (2, "Neil Young/Harvest/D1-01 - X1.flac"), "X2": (2, "Neil Young/Harvest/D2-01 - X2.flac"),
        "Y1": (1, "Neil Young/After the Gold Rush/01 - Y1.flac")}


# ---- legacy unprefixed files on multi-disc albums ----

def legacy_foo(volumes, discs=1):
    """An album with 'Foo' as track 2 (Tidal: 249 s) and an unlinked legacy `02 - Foo.flac` of 245 s."""
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Intro"), 1, "Intro", 1, dur=100)
    add_track(conn, rel, add_recording(conn, a, "Foo", dur=249), 2, "Foo", 2, dur=249)
    if discs == 2:
        add_track(conn, rel, add_recording(conn, a, "Outro"), 1, "Outro", 3, dur=100, disc=2)
    conn.execute("UPDATE release SET num_volumes = ?", (volumes,))
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)
    conn.execute("UPDATE file SET duration_s = 245")
    return conn


def test_multi_disc_legacy_file_with_the_same_title_and_number_is_owned_whatever_its_duration():
    conn = legacy_foo(2, discs=2)
    skipped = []
    assert titles(plan_missing(conn, skipped=skipped)) == ["Intro", "Outro"]
    [(item, path)] = skipped
    assert (item.title, path) == ("Foo", "Neil Young/Harvest/02 - Foo.flac")
    # only the same number: a tag title or a 'NN - Title' name is enough, a different number is not
    conn.execute("UPDATE file SET path = 'Neil Young/Harvest/05 - Foo.flac'")
    assert titles(plan_missing(conn)) == ["Intro", "Foo", "Outro"]
    conn.execute("UPDATE file SET path = 'Neil Young/Harvest/x.flac', tag_title = 'Foo', tag_track = '2/12'")
    assert titles(plan_missing(conn)) == ["Intro", "Outro"]


def test_multi_disc_rule_needs_the_exact_title_so_a_live_variant_does_not_own_the_studio_track():
    conn = legacy_foo(2, discs=2)
    conn.execute("UPDATE file SET path = 'Neil Young/Harvest/02 - Foo (Live).flac'")
    skipped = []
    assert titles(plan_missing(conn, skipped=skipped)) == ["Intro", "Foo", "Outro"]
    assert skipped == []


def test_one_legacy_file_is_not_claimed_by_same_numbered_same_titled_tracks_on_two_discs():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a)  # unlinked, matches both discs' track 1
    skipped = []
    assert [i.target_rel for i in plan_missing(conn, scope="all", skipped=skipped)] == \
        ["Neil Young/Harvest/D1-01 - Intro.flac", "Neil Young/Harvest/D2-01 - Intro.flac"]
    assert skipped == []


def test_multi_disc_rule_ignores_linked_files_so_disc_two_can_repeat_a_title_and_number():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, r1)  # owns disc 1's Intro only
    assert [i.target_rel for i in plan_missing(conn, scope="all")] == ["Neil Young/Harvest/D2-01 - Intro.flac"]


def test_single_disc_legacy_file_outside_the_duration_tolerance_is_not_claimed_so_it_stays_planned():
    # the executor then reports the occupied target as a conflict (test_fetch_run.py), never as fetched or existing
    conn = legacy_foo(1)
    skipped = []
    assert titles(plan_missing(conn, skipped=skipped)) == ["Intro", "Foo"]
    assert skipped == []
    conn.execute("UPDATE file SET duration_s = 248.5, tag_title = 'Foo'")  # within 3 s: the old rule still applies
    skipped = []
    assert titles(plan_missing(conn, skipped=skipped)) == ["Intro"]
    assert [p for _, p in skipped] == ["Neil Young/Harvest/02 - Foo.flac"]


# ---- a file that fits more than one track claims none ----

def test_one_file_cannot_claim_two_tracks_by_name_and_by_tag():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Foo"), 2, "Foo", 1)
    add_track(conn, rel, add_recording(conn, a, "Bar"), 5, "Bar", 2, disc=2)
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)  # the name says Foo #2, the tags say Bar #5
    conn.execute("UPDATE file SET tag_title = 'Bar', tag_track = '5/12'")
    skipped = []
    assert titles(plan_missing(conn, scope="all", skipped=skipped)) == ["Foo", "Bar"]
    assert skipped == []


def test_ambiguity_counts_owned_tracks_too():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/D2-01 - Intro.flac", a, r2)  # disc 2's Intro is owned
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a)         # fits disc 1's and disc 2's Intro
    skipped = []
    assert [i.target_rel for i in plan_missing(conn, scope="all", skipped=skipped)] == \
        ["Neil Young/Harvest/D1-01 - Intro.flac"]
    assert skipped == []


def test_ambiguity_counts_tracks_that_are_not_fetchable_too():
    conn, a, rel, r1, r2 = two_discs()
    conn.execute("UPDATE track SET tidal_id = NULL, available = 0 WHERE disc = 2")  # still a track of the album
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a)
    skipped = []
    assert [i.target_rel for i in plan_missing(conn, scope="all", skipped=skipped)] == \
        ["Neil Young/Harvest/D1-01 - Intro.flac"]
    assert skipped == []


def test_the_claim_is_the_same_whatever_the_scope_or_release_filter():
    conn, a, rel = seed()
    ed_a = add_release(conn, a, "Harvest", 101, date="1972-02-01")
    ed_b = add_release(conn, a, "Harvest (Deluxe Edition)", 102, date="2009-01-01")
    conn.execute("UPDATE release SET group_key = 'album|harvest' WHERE id IN (?, ?)", (ed_a, ed_b))
    conn.execute("UPDATE release SET num_volumes = 2 WHERE id = ?", (ed_a,))
    add_track(conn, ed_a, add_recording(conn, a, "Intro"), 1, "Intro", 1, disc=1)
    add_track(conn, ed_b, add_recording(conn, a, "Intro"), 1, "Intro", 2, disc=2)
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a)  # fits both editions' Intro
    seen = {}
    for name, kw in (("all", dict(scope="all")), ("partial", dict(scope="partial")), ("a", dict(release_id=ed_a)),
                     ("b", dict(release_id=ed_b))):
        skipped = []
        seen[name] = ([i.target_rel for i in plan_missing(conn, skipped=skipped, **kw)], skipped)
    assert all(skipped == [] for _, skipped in seen.values())
    assert seen["all"][0] == seen["partial"][0] == ["Neil Young/Harvest/D1-01 - Intro.flac",
                                                    "Neil Young/Harvest/D2-01 - Intro.flac"]
    assert seen["a"][0] == ["Neil Young/Harvest/D1-01 - Intro.flac"]
    assert seen["b"][0] == ["Neil Young/Harvest/D2-01 - Intro.flac"]
    # one edition only: nothing else fits the file, so every filter claims it
    conn.execute("DELETE FROM track WHERE release_id = ?", (ed_b,))
    for kw in (dict(scope="all"), dict(release_id=ed_a)):
        skipped = []
        assert plan_missing(conn, skipped=skipped, **kw) == []
        assert [p for _, p in skipped] == ["Neil Young/Harvest/01 - Intro.flac"]


def test_a_duration_match_that_fits_two_tracks_claims_none():
    conn, a, rel, r1, r2 = two_discs()
    conn.execute("UPDATE track SET duration_s = 100 WHERE disc = 1")
    conn.execute("UPDATE track SET duration_s = 101 WHERE disc = 2")
    add_file(conn, "Neil Young/Harvest/Intro.flac", a)  # no track number in the name: only the duration rule applies
    conn.execute("UPDATE file SET tag_title = 'Intro', duration_s = 100.5")
    skipped = []
    assert [i.disc for i in plan_missing(conn, scope="all", skipped=skipped)] == [1, 2]
    assert skipped == []
    conn.execute("UPDATE track SET duration_s = 110 WHERE disc = 2")  # now it fits disc 1 only
    skipped = []
    assert [i.disc for i in plan_missing(conn, scope="all", skipped=skipped)] == [2]
    assert [(i.disc, p) for i, p in skipped] == [(1, "Neil Young/Harvest/Intro.flac")]


# ---- one representative per cluster ----

def no_planned_path_is_an_existing_file(conn, items):
    files = {r[0] for r in conn.execute("SELECT path FROM file WHERE recording_id IS NULL")}
    assert not files & {i.target_rel for i in items}


def test_an_unavailable_canonical_row_still_decides_which_file_owns_the_cluster():
    conn, a, rel = seed()
    conn.execute("UPDATE release SET num_volumes = 2")
    canon, member = (add_recording(conn, a, "Foo") for _ in range(2))
    conn.execute("UPDATE recording SET canon_id = ? WHERE id IN (?, ?)", (canon, canon, member))
    gone = add_track(conn, rel, canon, 2, "Foo", 1, disc=2)
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (gone,))
    add_track(conn, rel, member, 2, "Foo", 2, disc=1)
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)
    skipped = []
    items = plan_missing(conn, scope="all", skipped=skipped)
    assert items == [] and [p for _, p in skipped] == ["Neil Young/Harvest/02 - Foo.flac"]
    no_planned_path_is_an_existing_file(conn, items)


def test_the_owned_and_planned_split_does_not_depend_on_scope_or_release_filter():
    conn, a, harvest = seed()
    live = add_release(conn, a, "Live Sessions", 101, date="1970-01-01")  # earlier than Harvest, nothing owned
    foo = add_recording(conn, a, "Foo")
    add_track(conn, harvest, foo, 2, "Foo", 1, dur=100)
    add_track(conn, live, foo, 5, "Foo", 2, dur=100)
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 100")
    for kw in (dict(scope="partial"), dict(scope="all"), dict(release_id=harvest), dict(release_id=live)):
        skipped = []
        items = plan_missing(conn, skipped=skipped, **kw)
        assert items == [] and [p for _, p in skipped] == ["Neil Young/Harvest/02 - Foo.flac"], kw
        no_planned_path_is_an_existing_file(conn, items)


# ---- ambiguity is per artist ----

def two_intros(same_group):
    conn, a, _ = seed()
    old = add_release(conn, a, "Harvest", 101, date="1972-02-01")
    new = add_release(conn, a, "Harvest (Deluxe Edition)", 102, date="2009-01-01")
    if same_group:
        conn.execute("UPDATE release SET group_key = 'album|harvest' WHERE id IN (?, ?)", (old, new))
    add_track(conn, old, add_recording(conn, a, "Intro"), 1, "Intro", 1, dur=100)
    add_track(conn, new, add_recording(conn, a, "Intro"), 1, "Intro", 2, dur=101)
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Intro', duration_s = 100")
    return conn


@pytest.mark.parametrize("same_group", [False, True])
def test_one_file_that_fits_a_track_in_each_of_two_releases_claims_neither(same_group):
    conn = two_intros(same_group)
    skipped = []
    assert [i.tidal_track_id for i in plan_missing(conn, scope="all", skipped=skipped)] == [1, 2]
    assert skipped == []


# ---- a file claims a cluster through any of its non-variant rows ----

def cluster_of_two(conn, a):
    canon, member = (add_recording(conn, a, "Foo") for _ in range(2))
    conn.execute("UPDATE recording SET canon_id = ? WHERE id IN (?, ?)", (canon, canon, member))
    return canon, member


def test_an_unavailable_representative_on_another_edition_does_not_decide_the_claim():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe)", 101, date="2009-01-01")
    conn.execute("UPDATE release SET num_volumes = 2 WHERE id = ?", (deluxe,))
    canon, member = cluster_of_two(conn, a)
    gone = add_track(conn, harvest, canon, 2, "Foo", 1, dur=200)  # the representative: 1-disc, other length, unavailable
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (gone,))
    add_track(conn, deluxe, member, 2, "Foo", 2, dur=250, disc=1)
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)
    skipped = []
    items = plan_missing(conn, scope="all", skipped=skipped)
    assert items == [] and [p for _, p in skipped] == ["Neil Young/Harvest/02 - Foo.flac"]
    no_planned_path_is_an_existing_file(conn, items)


def test_s2a_an_unavailable_one_disc_canonical_row_owns_a_file_that_only_its_length_fits():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe)", 101, date="2009-01-01")
    conn.execute("UPDATE release SET num_volumes = 2 WHERE id = ?", (deluxe,))
    canon, member = cluster_of_two(conn, a)
    gone = add_track(conn, harvest, canon, 2, "Foo", 1, dur=200)  # canonical, 1-disc, unavailable
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (gone,))
    add_track(conn, deluxe, member, 2, "Foo", 2, dur=250, disc=2)  # the only one that can be fetched
    add_file(conn, "Neil Young/Harvest/Foo.flac", a)  # no track number in the name or the tags
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 200")
    skipped = []
    items = plan_missing(conn, scope="all", skipped=skipped)
    assert items == [] and [p for _, p in skipped] == ["Neil Young/Harvest/Foo.flac"]  # not `D2-02 - Foo.flac`
    no_planned_path_is_an_existing_file(conn, items)


def test_a_file_matching_only_a_members_duration_owns_the_cluster():
    conn, a, rel = seed()
    canon, member = cluster_of_two(conn, a)
    add_track(conn, rel, canon, 2, "Foo", 1, dur=200)
    add_track(conn, rel, member, 3, "Foo", 2, dur=203)  # 206 is within 3 s of this one only
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 206")
    skipped = []
    items = plan_missing(conn, scope="all", skipped=skipped)
    assert items == [] and [p for _, p in skipped] == ["Neil Young/Harvest/02 - Foo.flac"]
    no_planned_path_is_an_existing_file(conn, items)


def test_a_file_that_fits_a_members_row_and_another_cluster_claims_neither():
    conn, a, rel = seed()
    canon, member = cluster_of_two(conn, a)
    add_track(conn, rel, canon, 2, "Foo", 1, dur=200)
    add_track(conn, rel, member, 3, "Foo", 2, dur=203)
    add_track(conn, rel, add_recording(conn, a, "Foo", dur=207), 4, "Foo", 3, dur=207)  # a distinct cluster
    add_file(conn, "Neil Young/Harvest/Foo.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 205")  # fits 203 and 207, not 200
    skipped = []
    assert [i.tidal_track_id for i in plan_missing(conn, scope="all", skipped=skipped)] == [1, 3]
    assert skipped == []


def test_known_gap_an_appears_on_folder_file_is_not_recognised_as_an_own_cluster_but_no_filter_changes_that():
    # KNOWN GAP (docs/M2_SPEC.md, Open): the file in `Live Sessions` fits the own `Intro`, but the default folder rule
    # only knows own releases' folders, so it owns nothing and Harvest's Intro is planned (a second copy on execute).
    # Asserted as it is today, not as the wanted result; what is pinned is that every scope and filter agree.
    conn, a, harvest = seed()
    live = add_release(conn, a, "Live Sessions", 101, role="appears_on")  # another edition of the same recording
    intro = add_recording(conn, a, "Intro")
    add_track(conn, harvest, intro, 1, "Intro", 1, dur=100)
    add_track(conn, live, intro, 3, "Intro", 2, dur=100)
    add_track(conn, harvest, add_recording(conn, a, "Outro"), 2, "Outro", 3)
    add_file(conn, "Neil Young/Harvest/02 - Outro.flac", a, conn.execute("SELECT id FROM recording WHERE title = 'Outro'").fetchone()[0])
    add_file(conn, "Neil Young/Live Sessions/03 - Intro.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Intro', duration_s = 100 WHERE recording_id IS NULL")
    for kw in (dict(scope="all"), dict(scope="partial"), dict(release_id=harvest), dict(release_id=live)):
        skipped = []
        assert [i.target_rel for i in plan_missing(conn, skipped=skipped, **kw)] == ["Neil Young/Harvest/01 - Intro.flac"], kw
        assert skipped == [], kw


def test_a_symbol_only_title_matches_the_same_raw_title():
    conn, a, rel = seed()
    conn.execute("UPDATE release SET num_volumes = 2")
    add_track(conn, rel, add_recording(conn, a, "Intro"), 1, "Intro", 1, disc=1)
    add_track(conn, rel, add_recording(conn, a, "?"), 2, "?", 2, disc=2)
    add_file(conn, "Neil Young/Harvest/02 - ?.flac", a)  # normalises to '' like the title, but the raw titles are equal
    skipped = []
    assert titles(plan_missing(conn, scope="all", skipped=skipped)) == ["Intro"]
    assert [(i.title, p) for i, p in skipped] == [("?", "Neil Young/Harvest/02 - ?.flac")]


def test_an_empty_normalised_title_never_matches_by_title():
    conn, a, rel = seed()
    conn.execute("UPDATE release SET num_volumes = 2")
    add_track(conn, rel, add_recording(conn, a, "Intro"), 1, "Intro", 1, disc=1)
    add_track(conn, rel, add_recording(conn, a, "?"), 2, "?", 2, disc=2)
    add_file(conn, "Neil Young/Harvest/02 - ....flac", a)  # both normalise to ''
    skipped = []
    assert titles(plan_missing(conn, scope="all", skipped=skipped)) == ["Intro", "?"]
    assert skipped == []


# ---- a file in an appears-on-only release folder ----

def foo_on_live_sessions():
    """Foo exists only on an appears-on release; the owned unlinked file sits in that release's folder."""
    conn, a, harvest = seed()
    live = add_release(conn, a, "Live Sessions", 101, role="appears_on")
    add_track(conn, harvest, add_recording(conn, a, "Outro"), 1, "Outro", 1, dur=100)
    add_track(conn, live, add_recording(conn, a, "Foo", dur=200), 3, "Foo", 2, dur=200)
    add_file(conn, "Neil Young/Live Sessions/Foo.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 200")
    return conn, live


def test_a_file_in_an_appears_on_only_release_folder_claims_its_cluster_under_release():
    conn, live = foo_on_live_sessions()
    skipped = []
    assert plan_missing(conn, release_id=live, skipped=skipped) == []
    assert [p for _, p in skipped] == ["Neil Young/Live Sessions/Foo.flac"]
    assert plan_missing(conn) == []  # the default scope never plans an appears-on track (and Harvest has nothing owned)
    assert titles(plan_missing(conn, scope="all")) == ["Outro"]


@pytest.mark.parametrize("same_recording", [True, False])
def test_an_appears_on_folder_file_gives_the_same_split_whatever_the_scope_or_release(same_recording):
    conn, a, harvest = seed()
    live = add_release(conn, a, "Live Sessions", 102, role="appears_on", date="1970-01-01")  # sorts before Harvest
    intro = add_recording(conn, a, "Intro", dur=100)
    add_track(conn, harvest, intro, 1, "Intro", 1, dur=100)
    add_track(conn, live, intro if same_recording else add_recording(conn, a, "Intro", dur=100), 3, "Intro", 2, dur=100)
    outro = add_recording(conn, a, "Outro")
    add_track(conn, harvest, outro, 2, "Outro", 3)
    add_file(conn, "Neil Young/Harvest/02 - Outro.flac", a, outro)  # Harvest is partly owned, so the default scope plans it
    add_file(conn, "Neil Young/Live Sessions/03 - Intro.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Intro', duration_s = 100 WHERE recording_id IS NULL")
    seen = {}  # tidal track id -> what each run says about it
    for name, kw in (("partial", dict()), ("all", dict(scope="all")), ("harvest", dict(release_id=harvest)),
                     ("live", dict(release_id=live))):
        skipped = []
        items = plan_missing(conn, skipped=skipped, **kw)
        no_planned_path_is_an_existing_file(conn, items)
        for i in items:
            seen.setdefault(i.tidal_track_id, {})[name] = ("planned", i.target_rel)
        for i, p in skipped:
            seen.setdefault(i.tidal_track_id, {})[name] = ("owned", p)
    assert seen, "something must be planned or owned"
    for tid, runs in seen.items():
        assert len(set(runs.values())) == 1, (tid, runs)
    # the file never counts as owning the Harvest Intro (known gap, see below), so that one is planned in every run
    assert {v for runs in seen.values() for v in runs.values()} >= {("planned", "Neil Young/Harvest/01 - Intro.flac")}


def test_an_appears_on_only_track_does_not_take_a_file_that_an_own_track_claims():
    conn, a, harvest = seed()
    other = add_release(conn, a, "Harvest", 101, role="appears_on", date="2010-01-01")  # same folder name as the own one
    add_track(conn, harvest, add_recording(conn, a, "Foo", dur=200), 2, "Foo", 1, dur=200)
    add_track(conn, other, add_recording(conn, a, "Foo", dur=200), 9, "Foo", 2, dur=200)  # another recording, same length
    add_file(conn, "Neil Young/Harvest/Foo.flac", a)
    conn.execute("UPDATE file SET tag_title = 'Foo', duration_s = 200")
    skipped = []
    assert plan_missing(conn, scope="all", skipped=skipped) == []  # the own Foo owns it
    assert [p for _, p in skipped] == ["Neil Young/Harvest/Foo.flac"]
    skipped = []
    assert [i.tidal_track_id for i in plan_missing(conn, release_id=other, skipped=skipped)] == [2]
    assert skipped == []


def test_a_variant_on_an_own_release_does_not_hide_the_non_variant_row_on_the_requested_appears_on_release():
    conn, a, _ = seed()
    live = add_release(conn, a, "Live at X", 101)
    hits = add_release(conn, a, "Greatest Hits", 102, role="appears_on")
    gold = add_recording(conn, a, "Heart of Gold")
    add_track(conn, hits, gold, 1, "Heart of Gold", 301)
    add_track(conn, live, add_recording(conn, a, "Heart of Gold (live)", variant="live", canon_id=gold), 1,
              "Heart of Gold (live)", 302)
    assert [(i.tidal_track_id, i.target_rel) for i in plan_missing(conn, release_id=hits)] == [
        (301, "Neil Young/Greatest Hits/01 - Heart of Gold.flac")]
    # intended: appears-on rows are not loaded by default, so the cluster is variant-only in a default run
    assert plan_missing(conn) == []
    assert plan_missing(conn, scope="all") == []
    assert plan_missing(conn, release_id=live) == []  # only the variant row is in scope
    assert [i.tidal_track_id for i in plan_missing(conn, include_variants=True, scope="all")] == [302]  # variant-only


def test_a_mixed_cluster_plans_its_non_variant_row_with_include_variants_too():
    conn, a, _ = seed()
    live = add_release(conn, a, "Live at X", 101)
    hits = add_release(conn, a, "Greatest Hits", 102, role="appears_on")
    gold = add_recording(conn, a, "Heart of Gold")
    add_track(conn, hits, gold, 1, "Heart of Gold", 301)
    add_track(conn, live, add_recording(conn, a, "Heart of Gold (live)", variant="live", canon_id=gold), 1,
              "Heart of Gold (live)", 302)
    assert [i.tidal_track_id for i in plan_missing(conn, release_id=hits, include_variants=True)] == [301]


# ---- a target path that a linked file already holds ----

def test_an_identical_title_track_whose_target_path_is_a_linked_file_is_planned_so_the_executor_reports_a_conflict():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe Edition)", 101, date="2009-01-01")
    conn.execute("UPDATE release SET group_key = 'album|harvest' WHERE id IN (?, ?)", (harvest, deluxe))
    gold = add_recording(conn, a, "Heart of Gold", dur=188)
    add_track(conn, harvest, gold, 5, "Heart of Gold", 1, dur=188)
    add_track(conn, deluxe, add_recording(conn, a, "Heart of Gold", dur=188), 5, "Heart of Gold", 2, dur=188)
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a, gold)  # linked to the Harvest recording only
    conn.execute("UPDATE file SET tag_title = 'Heart of Gold', duration_s = 188")
    for kw in (dict(scope="all"), dict(release_id=deluxe)):
        skipped = []
        items = plan_missing(conn, skipped=skipped, **kw)
        assert [(i.tidal_track_id, i.target_rel) for i in items] == [(2, "Neil Young/Harvest/05 - Heart of Gold.flac")], kw
        assert skipped == [], kw


def test_an_unrelated_linked_file_at_the_target_path_leaves_the_track_planned_not_skipped():
    conn, a, harvest = seed()
    add_track(conn, harvest, add_recording(conn, a, "Heart of Gold", dur=188), 5, "Heart of Gold", 1, dur=188)
    other = add_recording(conn, a, "Something Else", dur=300)
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a, other)
    conn.execute("UPDATE file SET tag_title = 'Something Else', duration_s = 300")
    skipped = []
    items = plan_missing(conn, scope="all", skipped=skipped)
    assert [(i.tidal_track_id, i.target_rel) for i in items] == [(1, "Neil Young/Harvest/05 - Heart of Gold.flac")]
    assert skipped == []
