from cadence.identity.grouping import RecordingItem, cluster_recordings, release_group_key


def key(title, type="album", version=None):
    return release_group_key(title, version, type)


# ---------------------------------------------------------------- release_group_key

def test_neutral_flags_do_not_change_the_group():
    base = key("On Through The Night")
    assert base == "album|on through the night"
    for title in ("On Through The Night (Remastered)", "On Through The Night (Remastered 2018)",
                  "On Through The Night (Deluxe Edition)", "On Through The Night (Expanded)",
                  "On Through The Night [Explicit]", "On Through The Night - 2018 Remaster",
                  "on through the night (Deluxe) [Remastered]"):
        assert key(title) == base, title
    assert key("On Through The Night", version="Remastered 2018") == base
    assert key("Pyromania") == key("Pyromania (Deluxe)") == key("Pyromania (Super Deluxe)")


def test_variants_stay_separate_groups():
    assert key("Hysteria") != key("Hysteria (Live)")
    assert key("Hysteria (Live)") == "album|hysteria [live]"
    assert key("Chandelier (Remixes)") != key("Chandelier")
    assert key("Hits Vegas (Live)") == key("Hits Vegas (Live) (Deluxe)")   # deluxe is neutral, live is not
    assert key("Heroes (2026 Mix)") != key("Heroes")


def test_type_never_merges():
    assert key("Hahaha", "single") == "single|hahaha"
    assert key("Hahaha", "single") != key("Hahaha", "album")
    assert key("Hahaha", "ep") != key("Hahaha", "album")


# ---------------------------------------------------------------- cluster_recordings

def rec(id, norm="song", dur=200, isrc=None, date="2000-01-01"):
    return RecordingItem(id, norm, dur, isrc, date)


def live(id, groups, dur=342, isrc=None, norm="song [live]", date="2021-05-07"):
    return RecordingItem(id, norm, dur, isrc, date, "live", frozenset(groups))


def test_same_length_remaster_isrcs_cluster_and_canonical_is_the_earliest():
    items = [rec(7, isrc="REM1", date="2018-06-01"), rec(3, isrc="ORIG1", date="1980-01-01"),
             rec(9, "other", 180, "X")]
    assert cluster_recordings(items) == [[3, 7], [9]]


def test_canonical_ties_go_to_lowest_id_and_missing_dates_lose():
    assert cluster_recordings([rec(5, date="2001-01-01"), rec(2, date="2001-01-01")]) == [[2, 5]]
    assert cluster_recordings([rec(1, date=None), rec(8, date="1999-01-01")]) == [[8, 1]]
    assert cluster_recordings([rec(4, date=None), rec(3, date=None)]) == [[3, 4]]


def test_161_vs_235_seconds_is_two_songs():
    assert cluster_recordings([rec(1, dur=161), rec(2, dur=235)]) == [[1], [2]]


def test_different_title_or_variant_never_clusters():
    assert cluster_recordings([rec(1, "song"), live(2, ["g"], 200), live(3, ["g"], 200, norm="song [live at wembley]")]) \
        == [[1], [2], [3]]


def test_tolerance_is_measured_from_the_first_member_not_chained():
    items = [rec(i, dur=d) for i, d in enumerate([200, 202, 203, 204, 207, 208], start=1)]
    assert cluster_recordings(items) == [[1, 2, 3], [4, 5], [6]]   # 204 starts a new cluster (204-200 > 3); 208-204 > 3
    assert cluster_recordings([rec(1, dur=203), rec(2, dur=200)]) == [[1, 2]]   # input order does not matter


def test_unknown_duration_is_a_singleton_unless_the_isrc_is_identical():
    assert cluster_recordings([rec(1, dur=None), rec(2, dur=None), rec(3, dur=200)]) == [[1], [2], [3]]
    assert cluster_recordings([rec(1, dur=None, isrc="ab1"), rec(2, "other", None, " AB1 "), rec(3, dur=200)]) \
        == [[1, 2], [3]]


def test_empty_and_stable_order():
    assert cluster_recordings([]) == []
    items = [rec(9, "z", 100), rec(4, "a", 100), rec(6, "a", 101)]
    assert cluster_recordings(items) == cluster_recordings(list(reversed(items))) == [[4, 6], [9]]


# ---------------------------------------------------------------- variant recordings

def test_live_recordings_on_different_release_groups_stay_separate():
    items = [live(1, ["album|2003.04.11"]), live(2, ["album|2003.06.25"]), live(3, ["album|2013.11.30"], 344)]
    assert cluster_recordings(items) == [[1], [2], [3]]


def test_live_recordings_on_editions_of_one_group_merge():
    items = [live(1, ["album|show"], date="2001-01-01"), live(2, ["album|show", "album|best of"], 343, date="2019-01-01")]
    assert cluster_recordings(items) == [[1, 2]]
    assert cluster_recordings([live(1, ["a|x"]), live(2, ["a|x"], 350)]) == [[1], [2]]      # still needs the 3 s rule
    other = [live(1, ["a|x", "a|y"]), live(2, ["a|y"]), live(3, ["a|z"])]                  # one shared group is enough
    assert cluster_recordings(other) == [[1, 2], [3]]


def test_live_recordings_with_a_shared_isrc_merge_whatever_the_groups():
    items = [live(1, ["a|x"], 300, "LIVE1"), live(2, ["a|y"], 342, "live1"), live(3, ["a|z"], 342)]
    assert cluster_recordings(items) == [[1, 2], [3]]


def test_variant_without_release_groups_is_a_singleton():
    assert cluster_recordings([live(1, []), live(2, [])]) == [[1], [2]]


def test_main_versions_ignore_release_groups():
    items = [RecordingItem(1, "song", 200, None, "1980-01-01", None, frozenset({"a|x"})),
             RecordingItem(2, "song", 201, None, "2010-01-01", None, frozenset({"a|y"})),
             RecordingItem(3, "song", 200, None, "2010-01-01")]
    assert cluster_recordings(items) == [[1, 2, 3]]
