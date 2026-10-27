from cadence.identity.grouping import RecordingItem, cluster_recordings, release_group_key, strip_album_prefix


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


# ---------------------------------------------------------------- album-prefixed titles

def alb(id, norm, dur=241, albums=(("g:night", "night"),), isrc=None, date="2000-01-01", variant=None, year=2000):
    """Recording sitting on album releases; `albums` = (album group key, release norm_title[, release year]) tuples."""
    albums = [a if len(a) == 3 else (*a, year) for a in albums]
    return RecordingItem(id, norm, dur, isrc, date, variant, frozenset(a[0] for a in albums), False, None, None,
                         frozenset(albums))


def test_album_prefixed_title_merges_with_the_plain_title_on_the_same_album_group():
    both = (("g:snw", "saturday night wrist"), ("g:coll", "the studio album collection"))
    prefixed = alb(12542, "saturday night wrist rats rats rats", albums=both, isrc="USRE11600124")
    plain = alb(12677, "rats rats rats", albums=(("g:snw", "saturday night wrist"),), isrc="USMV20600293")
    assert cluster_recordings([prefixed, plain]) == [[12542, 12677]]
    assert cluster_recordings([plain, prefixed]) == [[12542, 12677]]
    assert cluster_recordings([prefixed._replace(albums=frozenset())._replace(groups=frozenset()), plain]) \
        == [[12542], [12677]]                                  # without album data there is no new path


def test_album_prefixed_title_needs_durations_within_one_second():
    a = alb(1, "night song", albums=(("g:night", "night"),), isrc="A")
    for dur, merged in ((241, True), (242, True), (243, False), (246, False), (240, True)):
        b = alb(2, "song", dur=dur, isrc="B")
        assert (cluster_recordings([a._replace(duration_s=241), b]) == [[1, 2]]) is merged, dur
    assert cluster_recordings([a, alb(2, "song", dur=None, isrc="B")]) == [[1], [2]]    # unknown duration never merges


def test_album_prefix_tolerance_is_measured_from_the_first_member_not_chained():
    third = alb(3, "night deluxe song", 243, (("g:night", "night deluxe"),))      # a third title, so only the new path
    items = [alb(1, "night song", 241), alb(2, "song", 242), third]
    assert cluster_recordings(items) == [[1, 2], [3]]          # 243 - 241 > 1: new head, not chained through 242


def test_same_stripped_title_on_different_album_groups_does_not_merge():
    a = alb(1, "night song", albums=(("g:night", "night"),))
    b = alb(2, "song", albums=(("g:other", "other"),))
    assert cluster_recordings([a, b]) == [[1], [2]]
    c = alb(3, "other song", albums=(("g:other", "other"),))          # prefixed on both, still different groups
    assert cluster_recordings([a, c]) == [[1], [3]]


def test_giant_steps_alternate_take_does_not_merge_with_an_unrelated_alternate_take():
    a = alb(1, "giant steps alternate take 8", 227, (("g:giant steps", "giant steps"),))
    b = alb(2, "blue train alternate take 8", 227, (("g:blue train", "blue train"),))
    c = alb(3, "alternate take 8", 227, (("g:blue train", "blue train"),))
    assert cluster_recordings([a, c]) == [[1], [3]]
    assert cluster_recordings([a, b, c]) == [[1], [2, 3]]       # only the one that shares the group (blue train)


def test_prefixed_and_plain_titles_with_different_stripped_forms_do_not_merge():
    assert cluster_recordings([alb(1, "night song"), alb(2, "other")]) == [[1], [2]]
    assert cluster_recordings([alb(1, "night song"), alb(2, "song two")]) == [[1], [2]]
    assert cluster_recordings([alb(1, "night song"), alb(2, "night other")]) == [[1], [2]]


def test_prefix_must_be_a_release_the_recording_is_on_and_end_with_a_space():
    on_other = alb(1, "night song", albums=(("g:night", "other"),))                  # its release is not "night"
    assert cluster_recordings([on_other, alb(2, "song")]) == [[1], [2]]
    assert cluster_recordings([alb(1, "nightsong"), alb(2, "song")]) == [[1], [2]]    # no space after the prefix
    assert cluster_recordings([alb(1, "night"), alb(2, "other")]) == [[1], [2]]       # a bare album title is not stripped
    assert cluster_recordings([alb(1, "night night"), alb(2, "night")]) == [[1, 2]]   # "night" left, equal to the plain one


def test_longest_matching_album_prefix_wins_and_a_second_album_does_not_hurt():
    two = (("g:night", "night"), ("g:night", "night live"))
    assert cluster_recordings([alb(1, "night live song", albums=two), alb(2, "song")]) == [[1, 2]]
    assert cluster_recordings([alb(1, "night live song", albums=two), alb(2, "live song")]) == [[1], [2]]
    other = (("g:night", "night"), ("g:other", "other"))     # "night" is no title in the shared group g:other
    assert cluster_recordings([alb(1, "night song", albums=other), alb(2, "song", albums=(("g:other", "other"),))]) \
        == [[1], [2]]
    assert cluster_recordings([alb(1, "night song", albums=other), alb(2, "song", albums=(("g:night", "night"),))]) \
        == [[1, 2]]                                          # shared group g:night: the prefix is its release's


def test_variant_scope_is_kept_for_prefixed_titles():
    a = alb(1, "night song [live]", variant="live")
    assert cluster_recordings([a, alb(2, "song [live]", variant="live")]) == [[1, 2]]
    assert cluster_recordings([a, alb(2, "song [live]")]) == [[1], [2]]                  # variant None vs live
    assert cluster_recordings([a, alb(2, "song [live at wembley]", variant="live at wembley")]) == [[1], [2]]


def test_same_isrc_and_title_in_one_group_still_merges_and_plain_titles_are_untouched():
    assert cluster_recordings([alb(1, "song", 200, isrc="X"), alb(2, "song", 230, isrc="x")]) == [[1, 2]]
    assert cluster_recordings([alb(1, "night song", 200, isrc="X"), alb(2, "night song", 230, isrc="x")]) == [[1, 2]]
    assert cluster_recordings([alb(1, "song")]) == [[1]]
    assert cluster_recordings([alb(1, "song", 200), alb(2, "song", 203)]) == [[1, 2]]   # old 3 s rule not narrowed
    assert cluster_recordings([alb(1, "song", 200, (("g:a", "a"),)), alb(2, "song", 203, (("g:b", "b"),))]) == [[1, 2]]


def test_prefix_path_does_not_change_clusters_of_plain_titles_in_the_same_bucket():
    items = [alb(1, "song", 200), alb(2, "song", 203), alb(3, "song", 204), alb(4, "song", 207)]
    assert cluster_recordings(items) == cluster_recordings([i._replace(albums=frozenset()) for i in items]) \
        == [[1, 2], [3, 4]]


def test_canonical_choice_is_unchanged_for_prefixed_merges():
    old = alb(5, "night song", date="1980-01-01")
    rem = alb(9, "song", date="2018-01-01")._replace(remaster=True, remaster_year=2018, remaster_date="2018-01-01")
    assert cluster_recordings([old, rem]) == [[9, 5]]
    assert cluster_recordings([alb(7, "night song", date="2001-01-01"), alb(3, "song", date="2001-01-01")]) == [[3, 7]]


# ---------------------------------------------------------------- fixes after review

def test_fix_a_prefix_must_be_the_title_of_a_release_in_the_shared_group():
    alpha, beta, hits = ("g:alpha", "alpha"), ("g:beta", "beta"), ("g:hits", "greatest hits")
    a = alb(1, "alpha intro", albums=(alpha, hits))          # "Alpha - Intro" on Alpha and on Greatest Hits
    b = alb(2, "intro", albums=(beta, hits))                 # "Intro" on Beta and on Greatest Hits
    assert cluster_recordings([a, b]) == [[1], [2]]          # shared group is Greatest Hits, whose title is not "alpha"
    assert cluster_recordings([b, a]) == [[1], [2]]
    assert cluster_recordings([a, alb(3, "intro", albums=(alpha,))]) == [[1, 3]]    # shared group Alpha: it is Alpha's


def test_fix_b_prefix_path_never_changes_a_cluster_the_old_rule_formed():
    items = [alb(1, "song", 200), alb(2, "song", 203), alb(3, "song", 204)]       # old rule: [1, 2] and [3]
    q = alb(4, "night song", 201)
    assert cluster_recordings(items) == [[1, 2], [3]]
    assert cluster_recordings(items + [q]) == [[1, 2], [3], [4]]    # Q may not join, re-head or split them
    assert cluster_recordings(items + [q._replace(duration_s=204)]) == [[1, 2], [3, 4]]   # C is a singleton: may pair


def test_fix_b_two_singletons_merge_and_prefixed_pairs_work_too():
    assert cluster_recordings([alb(1, "night song", 241), alb(2, "night deluxe song", 241,
                                                              (("g:night", "night deluxe"),))]) == [[1, 2]]
    old_pair = [alb(1, "song", 241, isrc="X"), alb(2, "song", 241, isrc="x")]        # one cluster under the old rule
    assert cluster_recordings(old_pair + [alb(3, "night song", 241)]) == [[1, 2], [3]]


def test_fix_c_no_chaining_beyond_one_second():
    a, b, c = alb(1, "night song", 241), alb(2, "song", 242), alb(3, "song", 245)
    clusters = cluster_recordings([a, b, c])
    assert not any({1, 2, 3} <= set(ids) for ids in clusters)
    assert [3] not in [ids for ids in clusters if 1 in ids] and not any({1, 3} <= set(ids) for ids in clusters)
    assert clusters == [[1], [2, 3]]                         # B and C are one cluster under the old rule: A stays out


def test_fix_c_a_new_cluster_has_exactly_two_members():
    p = alb(1, "night song", 241)
    x = alb(2, "song", 242)
    y = alb(3, "night deluxe song", 242, (("g:night", "night deluxe"),))             # a third title that strips to "song"
    clusters = cluster_recordings([p, x, y])
    assert clusters == [[1, 2], [3]]                         # P-X (closest, lowest id), Y left alone
    assert cluster_recordings([y, x, p]) == clusters


# ---------------------------------------------------------------- narrowing: year, 60 s floor, variant residue

def test_narrow_1_release_years_must_match():
    prefixed = alb(1, "hello goodbye", 200, (("g:hello", "hello", 1990),))
    plain = lambda year: alb(2, "goodbye", 200, (("g:hello", "hello deluxe", year),))
    assert cluster_recordings([prefixed, plain(1991)]) == [[1], [2]]      # 'Hello' 1990 vs 'Hello (Deluxe)' 1991
    assert cluster_recordings([plain(1991), prefixed]) == [[1], [2]]
    assert cluster_recordings([prefixed, plain(1990)]) == [[1, 2]]        # same year: merges
    assert cluster_recordings([prefixed, plain(None)]) == [[1], [2]]      # no date on the plain album: no merge
    undated = alb(1, "hello goodbye", 200, (("g:hello", "hello", None),))
    assert cluster_recordings([undated, plain(1990)]) == [[1], [2]]       # no date on the prefix album: no merge
    assert cluster_recordings([undated, plain(None)]) == [[1], [2]]       # None never equals None


def test_narrow_1_year_is_compared_with_the_prefix_release_and_any_album_of_the_plain_one_in_the_group():
    prefixed = alb(1, "hello goodbye", 200, (("g:hello", "hello", 1990), ("g:hello", "hello deluxe", 1991)))
    plain = alb(2, "goodbye", 200, (("g:hello", "hello deluxe", 1991),))
    assert cluster_recordings([prefixed, plain]) == [[1], [2]]            # the prefix is 'hello' (1990), not the 1991 deluxe
    both = alb(3, "goodbye", 200, (("g:hello", "hello deluxe", 1991), ("g:hello", "hello", 1990)))
    assert cluster_recordings([prefixed, both]) == [[1, 3]]        # the plain one is also on the 1990 release
    two_prefixed = [alb(4, "hello goodbye", 200, (("g:hello", "hello", 1990),)),
                    alb(5, "hello deluxe goodbye", 200, (("g:hello", "hello deluxe", 1991),))]
    assert cluster_recordings(two_prefixed) == [[4], [5]]                 # both prefixed: their prefix releases differ
    assert cluster_recordings([two_prefixed[0], two_prefixed[1]._replace(
        albums=frozenset({("g:hello", "hello deluxe", 1990)}))]) == [[4, 5]]


def test_narrow_2_both_durations_must_be_at_least_60_seconds():
    for dur, merged in ((12, False), (59, False), (60, True), (61, True)):
        pair = [alb(1, "night intro", dur, isrc="A"), alb(2, "intro", dur, isrc="B")]
        assert (cluster_recordings(pair) == [[1, 2]]) is merged, dur
    assert cluster_recordings([alb(1, "night intro", 12), alb(2, "intro", 12)]) == [[1], [2]]
    assert cluster_recordings([alb(1, "night intro", 61), alb(2, "intro", 61)]) == [[1, 2]]
    assert cluster_recordings([alb(1, "night intro", 60), alb(2, "intro", 61)]) == [[1, 2]]
    assert cluster_recordings([alb(1, "night intro", 59), alb(2, "intro", 60)]) == [[1], [2]]   # one under the floor
    assert cluster_recordings([alb(1, "night intro", 12, isrc="X"), alb(2, "night intro", 12, isrc="x")]) == [[1, 2]]
    assert cluster_recordings([alb(1, "intro", 12), alb(2, "intro", 16)]) == [[1], [2]]   # the old rules are not touched
    assert cluster_recordings([alb(1, "intro", 12), alb(2, "intro", 14)]) == [[1, 2]]     # plain titles within 3 s: old rule, no floor


def test_narrow_3_a_variant_label_alone_is_not_a_title_after_the_prefix():
    night = (("g:night", "night"),)
    prefixed = alb(1, "night [live]", albums=night, variant="live")           # album 'Night' holds 'Night (Live)'
    assert strip_album_prefix("night [live]", {"night"}) == "night [live]"     # residue is only '[live]': not stripped
    assert strip_album_prefix("night song [live]", {"night"}) == "song [live]"   # a real title before the label: stripped
    assert strip_album_prefix("night  [live]", {"night"}) == "night  [live]"
    for norm in (" [live]", "[live]"):                                          # '(Live)' alone, as stored / hand-written
        assert cluster_recordings([prefixed, alb(2, norm, albums=night, variant="live")]) == [[1], [2]], norm
    deluxe = alb(3, "night deluxe [live]", albums=(("g:night", "night deluxe"),), variant="live")
    assert cluster_recordings([prefixed, deluxe]) == [[1], [3]]               # both would shrink to '[live]'
    song = alb(4, "night song [live]", albums=night, variant="live")
    assert cluster_recordings([song, alb(5, "song [live]", albums=night, variant="live")]) == [[4, 5]]
