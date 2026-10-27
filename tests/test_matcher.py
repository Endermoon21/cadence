from cadence.identity.matcher import AmbiguousMatch, FileFacts, MatchResult, RecCandidate, match_file


def cand(rid, title="song", dur=200, isrc=None, releases=(), cluster=None):
    return RecCandidate(rid, isrc, title, dur, frozenset(releases), cluster)


def test_isrc_match_ignores_title_and_duration():
    f = FileFacts("Totally Different", None, isrc=" usabc1234567 ")
    r = match_file(f, [cand(1, isrc="XX0000000001"), cand(2, "song", 999, isrc="USABC1234567")])
    assert r == MatchResult(2, "isrc", 1.0)


def test_isrc_beats_fuzzy():
    f = FileFacts("Song", 200.0, isrc="USABC1234567")
    r = match_file(f, [cand(1, "song", 200), cand(2, "other", 10, isrc="USABC1234567")])
    assert (r.recording_id, r.method) == (2, "isrc")


def test_fuzzy_within_tolerance():
    r = match_file(FileFacts("Song", 201.5), [cand(1, dur=200)])
    assert r == MatchResult(1, "fuzzy", 0.8)


def test_fuzzy_at_exact_tolerance_boundary():
    assert match_file(FileFacts("Song", 202.0), [cand(1, dur=200)]) is not None


def test_outside_tolerance_is_none():
    assert match_file(FileFacts("Song", 203.0), [cand(1, dur=200)]) is None


def test_custom_tolerance():
    assert match_file(FileFacts("Song", 203.0), [cand(1, dur=200)], tolerance_s=5) is not None


def test_title_only_never_matches():
    assert match_file(FileFacts("Song", None), [cand(1, dur=200)]) is None
    assert match_file(FileFacts("Song", 200.0), [cand(1, dur=None)]) is None
    assert match_file(FileFacts("Song", 200.0), [cand(1, "other title", 200)]) is None


def test_duration_none_does_not_fuzzy_match_but_isrc_still_works():
    assert match_file(FileFacts("Song", None), [cand(1)]) is None
    assert match_file(FileFacts("Song", None, isrc="A1"), [cand(1, isrc="a1")]).method == "isrc"


def test_variant_titles_are_distinct():
    cands = [cand(1, "song [instrumental]", 200)]
    assert match_file(FileFacts("Song", 200.0), cands) is None
    assert match_file(FileFacts("Song (Instrumental)", 200.0), cands).recording_id == 1


def test_album_tie_break_beats_closer_duration():
    cands = [cand(1, dur=200, releases={"other album"}), cand(2, dur=201, releases={"my album"})]
    r = match_file(FileFacts("Song", 200.0, album="My Album (Deluxe Edition)"), cands)
    assert r == MatchResult(2, "fuzzy", 0.9)


def test_duration_tie_break_then_lowest_id():
    cands = [cand(5, dur=201), cand(3, dur=199), cand(4, dur=200)]
    assert match_file(FileFacts("Song", 200.0), cands).recording_id == 4
    same = [cand(9, dur=201), cand(7, dur=199), cand(8, dur=201, releases={"other album"})]
    assert match_file(FileFacts("Song", 200.0), same) == AmbiguousMatch((7, 8, 9), 0.8)   # never guess on a tie


def test_tie_survives_float_rounding_noise_in_durations():
    cands = [cand(1, dur=200.1 + 0.2), cand(2, dur=200.3)]   # same 0.3s offset, different float rounding
    assert match_file(FileFacts("Song", 200.0), cands) == AmbiguousMatch((1, 2), 0.8)


def test_tie_is_only_within_the_winning_rank():
    cands = [cand(1, dur=199), cand(2, dur=201), cand(3, dur=200.5)]
    assert match_file(FileFacts("Song", 200.0), cands) == MatchResult(3, "fuzzy", 0.8)
    album = [cand(1, dur=199, releases={"my album"}), cand(2, dur=201, releases={"my album"}), cand(3, dur=200)]
    assert match_file(FileFacts("Song", 200.0, album="My Album"), album) == AmbiguousMatch((1, 2), 0.9)


def test_tie_in_a_later_tier_is_ambiguous_too():
    cands = [cand(1, "adrenaline 7 words", 223, releases={"adrenaline"}),
             cand(2, "adrenaline 7 words", 225, releases={"adrenaline"})]
    assert match_file(FileFacts("7 Words", 224.0, album="Adrenaline"), cands) == AmbiguousMatch((1, 2), 0.85)


def test_no_candidates():
    assert match_file(FileFacts("Song", 200.0), []) is None


def test_reissues_in_one_cluster_are_not_a_tie_and_link_to_the_closest_one_lowest_id_first():
    cands = [cand(5, releases={"alpha"}, cluster=3), cand(7, releases={"alpha"}, cluster=3)]
    assert match_file(FileFacts("Song", 200.0, album="Alpha"), cands) == MatchResult(5, "fuzzy", 0.9)


def test_a_same_cluster_tie_returns_a_tied_recording_not_the_canonical_outside_the_tolerance():
    cands = [cand(5, dur=203, cluster=5), cand(9, dur=200, cluster=5), cand(11, dur=200, cluster=5)]
    assert match_file(FileFacts("Song", 200.0), cands) == MatchResult(9, "fuzzy", 0.8)


def test_the_same_normalised_isrc_in_two_clusters_is_ambiguous():
    cands = [cand(1, "other", 10, isrc="USABC1234567"), cand(2, "other", 10, isrc="usabc1234567 ")]
    assert match_file(FileFacts("Song", 200.0, isrc="USABC1234567"), cands) == AmbiguousMatch((1, 2), 1.0)


def test_the_same_normalised_isrc_on_reissues_of_one_cluster_still_links():
    cands = [cand(1, isrc="USABC1234567", cluster=3), cand(2, isrc="usabc1234567 ", cluster=3)]
    assert match_file(FileFacts("Song", 200.0, isrc="USABC1234567"), cands) == MatchResult(1, "isrc", 1.0)


def test_a_tie_between_two_clusters_is_still_ambiguous_with_one_id_per_cluster():
    cands = [cand(5, cluster=3), cand(7, cluster=3), cand(9, cluster=9)]
    assert match_file(FileFacts("Song", 200.0), cands) == AmbiguousMatch((5, 9), 0.8)
