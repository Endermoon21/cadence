from cadence.identity.matcher import FileFacts, MatchResult, RecCandidate, match_file


def cand(rid, title="song", dur=200, isrc=None, releases=()):
    return RecCandidate(rid, isrc, title, dur, frozenset(releases))


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
    same = [cand(9, dur=201), cand(7, dur=199)]
    assert match_file(FileFacts("Song", 200.0), same).recording_id == 7


def test_no_candidates():
    assert match_file(FileFacts("Song", 200.0), []) is None
