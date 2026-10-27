import pytest

from cadence.fetch.plan import (Collision, R_APPEARS_ON, R_ARTIST_UNMATCHED, R_HIDDEN, R_NO_TIDAL_ID, R_NOT_IN_RELEASE, R_PARTIAL, R_UNAVAILABLE,
                                R_VARIANT, plan_missing, strip_markers)
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
    assert titles(plan_missing(conn, scope="all").items) == ["Three"]


def test_variants_only_with_include_variants():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    add_track(conn, rel, add_recording(conn, a, "Song (Instrumental)", variant="instrumental"), 2,
              "Song (Instrumental)", 2)
    assert titles(plan_missing(conn, scope="all").items) == ["Song"]
    assert titles(plan_missing(conn, include_variants=True, scope="all").items) == ["Song", "Song (Instrumental)"]


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
    [item] = plan_missing(conn, scope="all").items
    assert (item.tidal_track_id, item.recording_id, item.release, item.tidal_release_id) == (502, remaster, "Harvest", 102)
    assert item.target_rel == "Neil Young/Harvest/05 - Heart of Gold.flac"
    assert plan_missing(conn, ext=".m4a", scope="all").items[0].target_rel.endswith(".m4a")


def test_item_fields_and_unusable_tracks():
    conn, a, rel = seed()
    rec = add_recording(conn, a, "Old Man", isrc="USRE17200012", dur=204)
    add_track(conn, rel, rec, 4, "Old Man", 7, dur=203, disc=2, version="Live")
    add_track(conn, rel, add_recording(conn, a, "No Tidal id"), 5, "No Tidal id", None)
    unavailable = add_track(conn, rel, add_recording(conn, a, "Gone"), 6, "Gone", 8)
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (unavailable,))
    [i] = plan_missing(conn, scope="all").items
    assert (i.artist, i.release, i.disc, i.number, i.title, i.tidal_track_id, i.duration_s, i.isrc, i.dest) == \
        ("Neil Young", "Harvest", 2, 4, "Old Man (Live)", 7, 203, "USRE17200012", "server")
    assert i.target_rel == "Neil Young/Harvest/D2-04 - Old Man (Live).flac"  # the release has discs 1 and 2


def test_artists_without_a_resolved_tidal_match_are_skipped():
    conn = ledger()
    for name, match in (("Pending", "pending"), ("Ambiguous", "ambiguous"), ("Manual", "manual")):
        a = add_artist(conn, name, tidal_id={"pending": 11, "ambiguous": 12, "manual": 13}[match], match=match)
        add_track(conn, add_release(conn, a, f"{name} LP", a), add_recording(conn, a, f"{name} Song"), 1,
                  f"{name} Song", 100 + a)
    assert titles(plan_missing(conn, scope="all").items) == ["Manual Song"]


def test_an_artist_not_matched_to_tidal_has_each_unowned_cluster_skipped_with_a_reason():
    conn = ledger()
    a = add_artist(conn, "Ambiguous", tidal_id=12, match="ambiguous")
    rel = add_release(conn, a, "Amb LP", 12)
    add_track(conn, rel, add_recording(conn, a, "Amb One"), 1, "Amb One", 101)
    owned = add_recording(conn, a, "Amb Two")
    add_track(conn, rel, owned, 2, "Amb Two", 102)
    add_file(conn, "Ambiguous/Amb LP/02 - Amb Two.flac", a, owned)
    plan = plan_missing(conn, scope="all")
    assert plan.items == [] and plan.owned == 1
    assert [(s.title, s.tidal_track_id, s.reason) for s in plan.skipped] == [("Amb One", 101, R_ARTIST_UNMATCHED)]
    assert R_ARTIST_UNMATCHED == "artist not matched to Tidal (set it with cadence artist set-tidal)"


def test_scope_by_artist_and_release_and_destination():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "A"), 1, "A", 1)
    other = add_release(conn, a, "After the Gold Rush", 200, date="1970-01-01")
    add_track(conn, other, add_recording(conn, a, "B"), 1, "B", 2)
    b = add_artist(conn, "Someone Else", tidal_id=2)
    add_track(conn, add_release(conn, b, "Elsewhere", 300), add_recording(conn, b, "C"), 1, "C", 3)
    assert titles(plan_missing(conn, artist_id=a, scope="all").items) == ["B", "A"]  # ordered by release date
    assert titles(plan_missing(conn, release_id=other).items) == ["B"]
    conn.execute("INSERT INTO want(artist_id, dest) VALUES (?, 'both')", (a,))
    conn.execute("INSERT INTO want(release_id, dest) VALUES (?, 'fiio')", (other,))
    assert {i.title: i.dest for i in plan_missing(conn, scope="all").items} == {"A": "both", "B": "fiio", "C": "server"}


# ---- scope, folder naming ----

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
    assert [i.title for i in plan_missing(conn).items] == ["Cowgirl in the Sand"]
    assert [i.title for i in plan_missing(conn, scope="partial").items] == ["Cowgirl in the Sand"]
    assert sorted(i.title for i in plan_missing(conn, scope="all").items) == ["Cowgirl in the Sand", "Nobody Home"]
    [explicit] = plan_missing(conn, release_id=conn.execute("SELECT id FROM release WHERE title LIKE 'Lonely%'").fetchone()[0]).items
    assert explicit.title == "Nobody Home"  # asking for a release by id is explicit


def test_the_album_folder_comes_from_the_release_title_not_from_library_folders():
    conn, a = scoped()
    [i] = plan_missing(conn).items
    assert (i.album_dir, i.release) == ("Neil Young/Decade", "Decade")
    assert i.target_rel == "Neil Young/Decade/02 - Cowgirl in the Sand.flac"  # not "Decade (2017 Remaster)"
    # the spelling of the folder the owned file sits in is not read: the planner does not look at paths
    conn.execute("UPDATE file SET path = 'Neil Young/DECADE/01 - Sugar Mountain.flac'")
    assert plan_missing(conn).items[0].album_dir == "Neil Young/Decade"


def test_a_new_album_gets_the_cleaned_sanitised_name():
    conn, a = scoped()
    lonely = [i for i in plan_missing(conn, scope="all").items if i.title == "Nobody Home"][0]
    assert lonely.release == "Lonely Album: Part 1"            # tag keeps the real name
    assert lonely.album_dir == "Neil Young/Lonely Album - Part 1"  # folder is exFAT-safe, edition marker gone
    assert lonely.target_rel == "Neil Young/Lonely Album - Part 1/03 - Nobody Home.flac"


# ---- discs ----

def two_discs():
    conn, a, rel = seed()
    r1, r2 = (add_recording(conn, a, "Intro") for _ in range(2))
    add_track(conn, rel, r1, 1, "Intro", 1, disc=1)
    add_track(conn, rel, r2, 1, "Intro", 2, disc=2)
    return conn, a, rel, r1, r2


def test_same_numbered_same_titled_tracks_on_two_discs_get_distinct_targets():
    conn, *_ = two_discs()
    items = plan_missing(conn, scope="all").items
    assert [(i.disc, i.target_rel) for i in items] == [(1, "Neil Young/Harvest/D1-01 - Intro.flac"),
                                                       (2, "Neil Young/Harvest/D2-01 - Intro.flac")]
    assert plan_missing(conn, ext=".m4a", scope="all").items[1].target_rel == "Neil Young/Harvest/D2-01 - Intro.m4a"


def test_single_disc_names_are_unchanged():
    conn, a, rel = seed()
    for n in (1, 2):
        add_track(conn, rel, add_recording(conn, a, f"Song {n}"), n, f"Song {n}", n)
    assert [i.target_rel for i in plan_missing(conn, scope="all").items] == ["Neil Young/Harvest/01 - Song 1.flac",
                                                                       "Neil Young/Harvest/02 - Song 2.flac"]


def test_disc_count_covers_tracks_that_are_not_fetchable_or_not_missing():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, r1)  # disc 1 is owned under the old single-disc name
    conn.execute("UPDATE track SET tidal_id = NULL WHERE disc = 1")
    # the owned cluster is not planned; disc 2 gets its own disc-prefixed name
    assert [i.target_rel for i in plan_missing(conn, scope="all").items] == ["Neil Young/Harvest/D2-01 - Intro.flac"]


def test_null_num_volumes_with_a_disc_two_row_is_multi_disc_as_before():
    conn, a, rel, r1, r2 = two_discs()
    assert conn.execute("SELECT num_volumes FROM release").fetchone()[0] is None
    assert {i.discs for i in plan_missing(conn, scope="all").items} == {2}
    conn.execute("UPDATE track SET available = 0 WHERE disc = 2")  # unavailable and unlinked rows count too
    assert [i.target_rel for i in plan_missing(conn, scope="all").items] == ["Neil Young/Harvest/D1-01 - Intro.flac"]
    conn.execute("UPDATE track SET available = NULL, tidal_id = NULL WHERE disc = 2")
    assert [i.target_rel for i in plan_missing(conn, scope="all").items] == ["Neil Young/Harvest/D1-01 - Intro.flac"]


def test_volume_count_one_with_a_real_disc_two_row_is_still_multi_disc_so_names_do_not_collide():
    conn, a, rel, r1, r2 = two_discs()
    conn.execute("UPDATE release SET num_volumes = 1")
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all").items] == \
        [(2, "Neil Young/Harvest/D1-01 - Intro.flac"), (2, "Neil Young/Harvest/D2-01 - Intro.flac")]


def test_volume_count_one_and_only_disc_one_rows_keeps_single_disc_names():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    for n in (1, None, 0):  # NULL and < 1 are not usable counts
        conn.execute("UPDATE release SET num_volumes = ?", (n,))
        assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all").items] == \
            [(1, "Neil Young/Harvest/01 - Song.flac")]
    conn.execute("UPDATE release SET num_volumes = 2")  # Tidal says two discs even though only disc 1 has rows
    assert [i.discs for i in plan_missing(conn, scope="all").items] == [2]


@pytest.mark.parametrize("volumes", [None, 0, -1])
def test_unusable_volume_counts_give_the_same_disc_count_as_null(volumes):
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    conn.execute("UPDATE release SET num_volumes = ?", (volumes,))
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all").items] == \
        [(1, "Neil Young/Harvest/01 - Song.flac")]
    add_track(conn, rel, add_recording(conn, a, "Bonus"), 1, "Bonus", 2, disc=2)  # a real disc-2 row still prefixes
    assert [(i.discs, i.target_rel) for i in plan_missing(conn, scope="all").items] == \
        [(2, "Neil Young/Harvest/D1-01 - Song.flac"), (2, "Neil Young/Harvest/D2-01 - Bonus.flac")]


def test_the_disc_floor_is_per_release_so_a_sibling_with_a_disc_two_row_does_not_prefix_another_release():
    conn, a, x = seed()
    add_track(conn, x, add_recording(conn, a, "X1"), 1, "X1", 1)
    add_track(conn, x, add_recording(conn, a, "X2"), 1, "X2", 2, disc=2)
    y = add_release(conn, a, "After the Gold Rush", 200, date="1970-01-01")  # num_volumes NULL, disc 1 only
    add_track(conn, y, add_recording(conn, a, "Y1"), 1, "Y1", 3)
    assert {i.title: (i.discs, i.target_rel) for i in plan_missing(conn, scope="all").items} == {
        "X1": (2, "Neil Young/Harvest/D1-01 - X1.flac"), "X2": (2, "Neil Young/Harvest/D2-01 - X2.flac"),
        "Y1": (1, "Neil Young/After the Gold Rush/01 - Y1.flac")}


def test_a_linked_file_owns_its_own_cluster_only_so_disc_two_can_repeat_a_title_and_number():
    conn, a, rel, r1, r2 = two_discs()
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, r1)  # owns disc 1's Intro only (its cluster)
    assert [i.target_rel for i in plan_missing(conn, scope="all").items] == ["Neil Young/Harvest/D2-01 - Intro.flac"]



def test_a_variant_on_an_own_release_does_not_hide_the_non_variant_row_on_the_requested_appears_on_release():
    conn, a, _ = seed()
    live = add_release(conn, a, "Live at X", 101)
    hits = add_release(conn, a, "Greatest Hits", 102, role="appears_on")
    gold = add_recording(conn, a, "Heart of Gold")
    add_track(conn, hits, gold, 1, "Heart of Gold", 301)
    add_track(conn, live, add_recording(conn, a, "Heart of Gold (live)", variant="live", canon_id=gold), 1,
              "Heart of Gold (live)", 302)
    assert [(i.tidal_track_id, i.target_rel) for i in plan_missing(conn, release_id=hits).items] == [
        (301, "Neil Young/Greatest Hits/01 - Heart of Gold.flac")]
    # intended: appears-on rows are not loaded by default, so the cluster is variant-only in a default run
    assert plan_missing(conn).items == []
    assert plan_missing(conn, scope="all").items == []
    assert plan_missing(conn, release_id=live).items == []  # only the variant row is in scope
    assert [i.tidal_track_id for i in plan_missing(conn, include_variants=True, scope="all").items] == [302]  # variant-only


def test_a_mixed_cluster_plans_its_non_variant_row_with_include_variants_too():
    conn, a, _ = seed()
    live = add_release(conn, a, "Live at X", 101)
    hits = add_release(conn, a, "Greatest Hits", 102, role="appears_on")
    gold = add_recording(conn, a, "Heart of Gold")
    add_track(conn, hits, gold, 1, "Heart of Gold", 301)
    add_track(conn, live, add_recording(conn, a, "Heart of Gold (live)", variant="live", canon_id=gold), 1,
              "Heart of Gold (live)", 302)
    assert [i.tidal_track_id for i in plan_missing(conn, release_id=hits, include_variants=True).items] == [301]


# ---- a target path that a linked file already holds ----

# ---- a target path that a linked file already holds ----

def test_an_identical_title_track_whose_target_path_is_a_linked_file_is_planned_so_the_executor_reports_a_conflict():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe Edition)", 101, date="2009-01-01")
    conn.execute("UPDATE release SET group_key = 'album|harvest' WHERE id IN (?, ?)", (harvest, deluxe))
    gold = add_recording(conn, a, "Heart of Gold", dur=188)
    add_track(conn, harvest, gold, 5, "Heart of Gold", 1, dur=188)
    add_track(conn, deluxe, add_recording(conn, a, "Heart of Gold", dur=188), 5, "Heart of Gold", 2, dur=188)
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a, gold)  # linked to the Harvest recording only
    for kw in (dict(scope="all"), dict(release_id=deluxe)):
        plan = plan_missing(conn, **kw)
        assert [(i.tidal_track_id, i.target_rel) for i in plan.items] == [(2, "Neil Young/Harvest/05 - Heart of Gold.flac")], kw
        assert plan.owned == 1, kw


def test_an_unrelated_linked_file_at_the_target_path_leaves_the_track_planned_not_skipped():
    conn, a, harvest = seed()
    add_track(conn, harvest, add_recording(conn, a, "Heart of Gold", dur=188), 5, "Heart of Gold", 1, dur=188)
    other = add_recording(conn, a, "Something Else", dur=300)
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a, other)
    plan = plan_missing(conn, scope="all")
    assert [(i.tidal_track_id, i.target_rel) for i in plan.items] == [(1, "Neil Young/Harvest/05 - Heart of Gold.flac")]
    assert plan.skipped == []


# ---- ownership comes from the ledger's links and nothing else ----

def test_a_track_with_a_linked_file_is_owned_and_not_planned():
    conn, a, rel = seed()
    one, two = add_recording(conn, a, "One"), add_recording(conn, a, "Two")
    add_track(conn, rel, one, 1, "One", 1)
    add_track(conn, rel, two, 2, "Two", 2)
    add_file(conn, "Neil Young/Harvest/01 - One.flac", a, one)
    plan = plan_missing(conn, scope="all")
    assert titles(plan.items) == ["Two"] and (plan.owned, plan.skipped) == (1, [])


def test_a_linked_file_on_another_edition_owns_the_whole_cluster():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe Edition)", 101, date="2009-01-01")
    orig, remaster = add_recording(conn, a, "Heart of Gold"), add_recording(conn, a, "Heart of Gold (2009 Remaster)")
    conn.execute("UPDATE recording SET canon_id = ? WHERE id IN (?, ?)", (remaster, orig, remaster))
    add_track(conn, harvest, orig, 5, "Heart of Gold", 1)
    add_track(conn, deluxe, remaster, 5, "Heart of Gold", 2, version="2009 Remaster")
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a, orig)  # the file is the original's, on Harvest
    for kw in (dict(scope="all"), dict(scope="partial"), dict(release_id=harvest), dict(release_id=deluxe)):
        plan = plan_missing(conn, **kw)
        assert (plan.items, plan.owned, plan.skipped) == ([], 1, []), kw


def test_an_unlinked_file_owns_nothing_whatever_its_name_tags_duration_or_folder():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Heart of Gold", dur=188), 5, "Heart of Gold", 1, dur=188)
    add_file(conn, "Neil Young/Harvest/05 - Heart of Gold.flac", a)  # same name, folder, number, title and length
    conn.execute("UPDATE file SET tag_title = 'Heart of Gold', tag_track = '5', duration_s = 188")
    for kw in (dict(), dict(scope="all"), dict(release_id=rel)):
        plan = plan_missing(conn, **kw)
        # the unlinked file does not make the release "partial" either: the default scope has nothing to plan
        if not kw:
            assert (plan.items, plan.owned) == ([], 0) and [s.reason for s in plan.skipped] == [R_PARTIAL]
        else:
            assert [i.target_rel for i in plan.items] == ["Neil Young/Harvest/05 - Heart of Gold.flac"], kw
            assert (plan.owned, plan.skipped) == (0, []), kw


def test_ownership_does_not_depend_on_scope_or_release_filter():
    conn, a, harvest = seed()
    live = add_release(conn, a, "Live Sessions", 101, date="1970-01-01")
    foo = add_recording(conn, a, "Foo")
    add_track(conn, harvest, foo, 2, "Foo", 1)
    add_track(conn, live, foo, 5, "Foo", 2)
    add_file(conn, "Neil Young/Harvest/02 - Foo.flac", a, foo)
    for kw in (dict(scope="partial"), dict(scope="all"), dict(release_id=harvest), dict(release_id=live)):
        plan = plan_missing(conn, **kw)
        assert (plan.items, plan.owned, plan.skipped) == ([], 1, []), kw


def test_same_titled_tracks_of_other_clusters_are_planned_next_to_an_owned_one():
    conn, a, rel = seed()
    owned, two, three = (add_recording(conn, a, "Intro", dur=90) for _ in range(3))
    add_track(conn, rel, owned, 1, "Intro", 801, dur=90)
    add_track(conn, rel, two, 2, "Intro", 802, dur=90)
    add_track(conn, rel, three, 3, "Intro", 803, dur=90)
    add_file(conn, "Neil Young/Harvest/01 - Intro.flac", a, owned)
    plan = plan_missing(conn)
    assert [(i.number, i.title) for i in plan.items] == [(2, "Intro"), (3, "Intro")] and plan.owned == 1


# ---- every cluster ends up owned, planned or skipped with a reason ----

def every_reason():
    """One cluster for each way out: owned, planned, and each skip reason. Harvest is partial (one owned track)."""
    conn, a, harvest = seed()
    owned = add_recording(conn, a, "Owned")
    add_track(conn, harvest, owned, 1, "Owned", 1)
    add_file(conn, "Neil Young/Harvest/01 - Owned.flac", a, owned)
    add_track(conn, harvest, add_recording(conn, a, "Planned"), 2, "Planned", 2)
    add_track(conn, harvest, add_recording(conn, a, "Live Take", variant="live"), 3, "Live Take", 3)
    gone = add_track(conn, harvest, add_recording(conn, a, "Withdrawn"), 4, "Withdrawn", 4)
    conn.execute("UPDATE track SET available = 0 WHERE id = ?", (gone,))
    add_track(conn, harvest, add_recording(conn, a, "Not On Tidal"), 5, "Not On Tidal", None)
    hidden = add_release(conn, a, "Hidden LP", 200, date="2001-01-01")
    conn.execute("UPDATE release SET hidden_reason = 'user' WHERE id = ?", (hidden,))
    add_track(conn, hidden, add_recording(conn, a, "In Hiding"), 1, "In Hiding", 6)
    comp = add_release(conn, a, "Various Hits", 201, role="appears_on", type="compilation", date="2002-01-01")
    add_track(conn, comp, add_recording(conn, a, "Guest Spot"), 1, "Guest Spot", 7)
    lonely = add_release(conn, a, "Lonely Album", 202, date="2003-01-01")
    add_track(conn, lonely, add_recording(conn, a, "Nobody Home"), 1, "Nobody Home", 8)
    return conn, lonely


def test_every_skipped_track_has_a_specific_non_empty_reason():
    conn, lonely = every_reason()
    plan = plan_missing(conn)
    assert titles(plan.items) == ["Planned"] and plan.owned == 1
    assert all(isinstance(s.reason, str) and s.reason.strip() for s in plan.skipped)
    assert {s.title: s.reason for s in plan.skipped} == {
        "Live Take": R_VARIANT, "Withdrawn": R_UNAVAILABLE, "Not On Tidal": R_NO_TIDAL_ID, "In Hiding": R_HIDDEN,
        "Guest Spot": R_APPEARS_ON, "Nobody Home": R_PARTIAL}
    assert {s.title: s.tidal_track_id for s in plan.skipped}["Not On Tidal"] is None
    assert {s.title: s.tidal_track_id for s in plan.skipped}["Withdrawn"] == 4
    assert plan.skip_counts()[0][1] == 1 and sum(n for _, n in plan.skip_counts()) == 6
    # scope all plans the lonely release; the rest stays skipped for the same reasons
    wide = plan_missing(conn, scope="all")
    assert titles(wide.items) == ["Planned", "Nobody Home"] and "Nobody Home" not in {s.title for s in wide.skipped}
    # with --include-variants the variant is planned
    assert "Live Take" in titles(plan_missing(conn, include_variants=True).items)


def test_a_release_filter_skips_everything_else_with_that_reason():
    conn, lonely = every_reason()
    plan = plan_missing(conn, release_id=lonely)
    assert titles(plan.items) == ["Nobody Home"] and plan.owned == 1
    assert {s.reason for s in plan.skipped} == {R_NOT_IN_RELEASE} and len(plan.skipped) == 6


@pytest.mark.parametrize("kw", [dict(), dict(scope="all"), dict(include_variants=True), dict(release_id="lonely")])
def test_planned_owned_and_skipped_cover_every_cluster_exactly_once(kw):
    conn, lonely = every_reason()
    kw = {k: (lonely if v == "lonely" else v) for k, v in kw.items()}
    clusters = conn.execute("SELECT COUNT(DISTINCT COALESCE(rec.canon_id, rec.id)) FROM track t "
                            "JOIN recording rec ON rec.id = t.recording_id").fetchone()[0]
    plan = plan_missing(conn, **kw)
    assert len(plan.items) + plan.owned + len(plan.skipped) == clusters
    assert len({i.recording_id for i in plan.items} | {s.recording_id for s in plan.skipped}) == \
        len(plan.items) + len(plan.skipped)


def test_a_cluster_on_several_editions_is_one_entry():
    conn, a, harvest = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe Edition)", 101, date="2009-01-01")
    rec = add_recording(conn, a, "Heart of Gold")
    add_track(conn, harvest, rec, 5, "Heart of Gold", 1)
    add_track(conn, deluxe, rec, 5, "Heart of Gold", 2)
    plan = plan_missing(conn, scope="all")
    assert (len(plan.items), plan.owned, len(plan.skipped)) == (1, 0, 0)


def test_the_summary_counts_planned_owned_and_each_skip_reason():
    conn, lonely = every_reason()
    assert plan_missing(conn).summary_lines() == [
        "plan: 1 planned, 1 owned, 6 skipped",
        f"       1 skipped: {R_APPEARS_ON}", f"       1 skipped: {R_HIDDEN}", f"       1 skipped: {R_NO_TIDAL_ID}",
        f"       1 skipped: {R_PARTIAL}", f"       1 skipped: {R_UNAVAILABLE}", f"       1 skipped: {R_VARIANT}"]


# ---- collisions: a planned target that is the path of a file linked to another recording ----

def twin_collision():
    """Visible 'Beta' track 1 'Song' (rec A) and hidden 'Beta' track 1 'Song' (rec B); the file is linked to B."""
    conn = ledger()
    a = add_artist(conn, "Artist 1")
    visible, hidden = add_release(conn, a, "Beta", 100), add_release(conn, a, "Beta", 101)
    conn.execute("UPDATE release SET hidden_reason = 'duplicate' WHERE id = ?", (hidden,))
    rec_a, rec_b = add_recording(conn, a, "Song"), add_recording(conn, a, "Song")
    add_track(conn, visible, rec_a, 1, "Song", 1)
    add_track(conn, hidden, rec_b, 1, "Song", 2)
    add_file(conn, "Artist 1/Beta/01 - Song.flac", a, rec_b)
    return conn, rec_a


def test_a_planned_target_held_by_a_file_linked_to_another_recording_is_planned_and_listed_as_a_collision():
    conn, rec_a = twin_collision()
    plan = plan_missing(conn, scope="all")
    path = "Artist 1/Beta/01 - Song.flac"
    assert [(i.tidal_track_id, i.recording_id, i.target_rel) for i in plan.items] == [(1, rec_a, path)]  # not skipped
    assert [(c.tidal_track_id, c.target, c.file_path) for c in plan.collisions] == [(1, path, path)]
    assert plan.summary_lines()[0] == "plan: 1 planned, 1 owned, 0 skipped, 1 collision"


def test_a_plan_without_a_path_collision_has_no_collisions():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "One"), 1, "One", 1)
    add_track(conn, rel, add_recording(conn, a, "Two"), 2, "Two", 2)
    add_file(conn, "Neil Young/Harvest/02 - Two.flac", a)  # unlinked: it owns nothing and is not a collision
    plan = plan_missing(conn, scope="all")
    assert titles(plan.items) == ["One", "Two"] and plan.collisions == []
    assert "collision" not in plan.summary_lines()[0]


def test_two_recordings_planned_to_one_target_path_are_listed_as_a_duplicate_target():
    conn, a, rel = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe)", 101)  # same album folder: the edition marker is stripped
    add_track(conn, rel, add_recording(conn, a, "Two"), 2, "Two", 1)
    add_track(conn, deluxe, add_recording(conn, a, "Two"), 2, "Two", 2)
    plan = plan_missing(conn, scope="all")
    path = "Neil Young/Harvest/02 - Two.flac"
    assert [(i.tidal_track_id, i.target_rel) for i in plan.items] == [(1, path), (2, path)]
    assert [(c.tidal_track_id, c.target, c.file_path, c.kind) for c in plan.collisions] == [(2, path, path, "duplicate")]
    assert "duplicate target" in plan.collisions[0].line() and "linked to another" not in plan.collisions[0].line()
    assert plan.summary_lines()[0] == "plan: 2 planned, 0 owned, 0 skipped, 1 collision"


# ---- a recording shared by two artists ----

def shared_recording(owner="Zed"):
    """'Aaa' and 'Zed' both carry one recording on their own releases; `owner` is the recording row's artist."""
    conn = ledger()
    aaa, zed = add_artist(conn, "Aaa", tidal_id=1), add_artist(conn, "Zed", tidal_id=2)
    rec = add_recording(conn, {"Aaa": aaa, "Zed": zed}[owner], "Duet")
    add_track(conn, add_release(conn, aaa, "Aaa LP", 10), rec, 1, "Duet", 100)
    add_track(conn, add_release(conn, zed, "Zed LP", 11), rec, 1, "Duet", 101)
    return conn, aaa, zed, rec


def test_a_shared_recording_with_a_file_linked_under_the_other_artist_is_owned_and_not_planned_or_skipped():
    conn, aaa, zed, rec = shared_recording()
    add_file(conn, "Zed/Zed LP/01 - Duet.flac", zed, rec)
    plan = plan_missing(conn, scope="all")
    assert plan.items == [] and plan.skipped == [] and plan.owned == 2  # once per artist that carries it


def test_a_shared_recording_without_a_file_is_planned_once_under_the_recording_artist():
    conn, aaa, zed, rec = shared_recording("Zed")
    plan = plan_missing(conn, scope="all")
    assert [(i.artist, i.tidal_track_id) for i in plan.items] == [("Zed", 101)]
    assert [(s.artist, s.tidal_track_id, s.reason) for s in plan.skipped] == \
        [("Aaa", 100, "shared recording, planned under Zed")]
    assert plan_missing(conn, aaa, scope="all").items[0].artist == "Aaa"  # planned alone, the artist plans it itself


def test_a_shared_recording_whose_artist_is_not_in_the_plan_is_planned_under_the_first_artist():
    conn, aaa, zed, rec = shared_recording()
    conn.execute("UPDATE recording SET artist_id = ?", (add_artist(conn, "Nobody", tidal_id=3),))
    plan = plan_missing(conn, scope="all")
    assert [(i.artist, i.tidal_track_id) for i in plan.items] == [("Aaa", 100)]
    assert [(s.artist, s.reason) for s in plan.skipped] == [("Zed", "shared recording, planned under Aaa")]


# ---- collision annotations ----

def test_targets_differing_only_in_case_are_a_duplicate_and_the_printed_path_is_the_first_items():
    conn, a, rel = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe)", 101)
    add_track(conn, rel, add_recording(conn, a, "Song"), 2, "Song", 1)
    add_track(conn, deluxe, add_recording(conn, a, "song"), 2, "song", 2)
    plan = plan_missing(conn, scope="all")
    assert [i.target_rel for i in plan.items] == ["Neil Young/Harvest/02 - Song.flac", "Neil Young/Harvest/02 - song.flac"]
    assert [(c.tidal_track_id, c.target, c.file_path, c.kind) for c in plan.collisions] == \
        [(2, "Neil Young/Harvest/02 - Song.flac", "Neil Young/Harvest/02 - Song.flac", "duplicate")]


def test_an_item_with_both_a_linked_and_a_duplicate_collision_is_counted_once_in_the_summary():
    conn, a, rel = seed()
    deluxe = add_release(conn, a, "Harvest (Deluxe)", 101)
    add_track(conn, rel, add_recording(conn, a, "Two"), 2, "Two", 1)
    add_track(conn, deluxe, add_recording(conn, a, "Two"), 2, "Two", 2)
    other = add_recording(conn, a, "Elsewhere")
    add_file(conn, "Neil Young/Harvest/02 - Two.flac", a, other)  # a linked file holds the shared target
    plan = plan_missing(conn, scope="all")
    assert sorted((c.tidal_track_id, c.kind) for c in plan.collisions) == [(1, "linked"), (2, "duplicate"), (2, "linked")]
    assert plan.summary_lines()[0] == "plan: 2 planned, 0 owned, 0 skipped, 2 collisions"


def test_a_linked_file_path_differing_only_in_case_is_a_linked_collision():
    conn, a, rel = seed()
    add_track(conn, rel, add_recording(conn, a, "Song"), 1, "Song", 1)
    other = add_recording(conn, a, "Elsewhere")
    add_file(conn, "neil young/HARVEST/01 - song.flac", a, other)
    plan = plan_missing(conn, scope="all")
    path = "Neil Young/Harvest/01 - Song.flac"
    assert [(c.tidal_track_id, c.target, c.file_path, c.kind) for c in plan.collisions] == [(1, path, path, "linked")]
    assert plan.summary_lines()[0] == "plan: 1 planned, 0 owned, 0 skipped, 1 collision"


def test_a_collision_line_says_may_conflict_unless_the_item_is_fiio_only():
    c = Collision(7, "A/B/01 - C.flac", "A/B/01 - C.flac")
    assert c.line().endswith("(may conflict at execute)") and "will be a conflict" not in c.line()
    assert c.line(fiio=True).endswith("(fiio-only, checked in dry run and at execute)")
