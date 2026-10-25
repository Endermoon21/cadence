"""Regression cases for the three extra matcher tiers (prefix-strip, variant-head, wider tolerance on the
claimed album). Synthetic candidates only; titles go through norm_title the way the catalog stores them."""
from cadence.identity.matcher import FileFacts, MatchResult, RecCandidate, match_file
from cadence.identity.normalize import norm_title


def cand(rid, title, dur, releases=(), isrc=None):
    return RecCandidate(rid, isrc, norm_title(title), dur, frozenset(norm_title(r) for r in releases))


# --- prefix-strip: "<album> - <title>" ------------------------------------------------------------------

def test_prefix_strip_deftones_adrenaline():
    f = FileFacts("7 Words", 224.0, album="Adrenaline")
    r = match_file(f, [cand(1, "Adrenaline - 7 Words", 224, ["Adrenaline"])])
    assert r == MatchResult(1, "fuzzy", 0.85)


def test_prefix_strip_needs_the_claimed_album_on_the_candidate():
    f = FileFacts("7 Words", 224.0, album="Adrenaline")
    assert match_file(f, [cand(1, "Adrenaline - 7 Words", 224, ["White Pony"])]) is None


def test_prefix_strip_still_needs_duration_within_tolerance():
    f = FileFacts("7 Words", 224.0, album="Adrenaline")
    assert match_file(f, [cand(1, "Adrenaline - 7 Words", 228, ["Adrenaline"])]) is None


def test_prefix_strip_tag_side_prefix():
    f = FileFacts("The Original Album - Moondance", 270.0, album="Moondance")
    assert match_file(f, [cand(1, "Moondance", 270, ["Moondance"])]).recording_id == 1
    f2 = FileFacts("Adrenaline - 7 Words", 224.0, album="Adrenaline")
    assert match_file(f2, [cand(2, "7 Words", 224, ["Adrenaline"])]).recording_id == 2


def test_prefix_strip_love_does_not_alias_love_me_do():
    f = FileFacts("Love", 150.0, album="Love")
    assert match_file(f, [cand(1, "Love Me Do", 150, ["Love"])]) is None


def test_prefix_strip_never_overrides_an_exact_title_match():
    # exact title 2 s away is found by the existing tier; the closer prefixed candidate must not replace it
    f = FileFacts("7 Words", 224.0, album="Adrenaline")
    cands = [cand(1, "Adrenaline - 7 Words", 224, ["Adrenaline"]), cand(2, "7 Words", 226, ["Adrenaline"])]
    assert match_file(f, cands) == MatchResult(2, "fuzzy", 0.9)


# --- variant-head: "[live]" vs "[live ...]" ---------------------------------------------------------------

def test_variant_head_live_bundles():
    f = FileFacts("Bundles (live, Nottingham University, 1975-10-11)", 207.8, album="Bundles")
    r = match_file(f, [cand(1, "Bundles (Live, Nottingham University, 1975)", 208, ["Bundles"])])
    assert r == MatchResult(1, "fuzzy", 0.7)


def test_variant_head_plain_live_tag_vs_detailed_label():
    f = FileFacts("Song (Live)", 200.0)
    assert match_file(f, [cand(1, "Song - Live at the Forum 1983", 200)]).recording_id == 1


def test_variant_head_live_album_title_marks_untagged_tracks_live():
    f = FileFacts("Song", 200.0, album="Live at Reading")
    r = match_file(f, [cand(1, "Song (Live at Reading 1992)", 200, ["Live at Reading"])])
    assert r.recording_id == 1


def test_variant_head_live_album_riot_van():
    f = FileFacts("Riot Van", 237.8, album="Live in Texas: 7 June 2006")
    r = match_file(f, [cand(1, "Riot Van (Live in Texas)", 238, ["Live in Texas"])])
    assert r == MatchResult(1, "fuzzy", 0.7)


def test_live_album_detection_both_sides():
    for album in ("Live in Texas: 7 June 2006", "Live From the Troubadour", "I Might Be Wrong: Live Recordings",
                  "Live at Leeds", "Some Album (Live)", "Some Album [Live]", "Sessions Live Sessions"):
        f = FileFacts("Riot Van", 237.8, album=album)
        assert match_file(f, [cand(1, "Riot Van (Live)", 238)]).recording_id == 1, album
    for album in ("Live Through This", "Live and Let Die", "Alive in Texas", "Olive at Home", "Livejournal"):
        f = FileFacts("Riot Van", 237.8, album=album)
        assert match_file(f, [cand(1, "Riot Van (Live)", 238)]) is None, album


def test_variant_head_live_through_this_studio_file_does_not_link_to_live_take():
    f = FileFacts("Doll Parts", 211.0, album="Live Through This")
    assert match_file(f, [cand(1, "Doll Parts (Live at Reading 1994)", 211, ["Live Through This"])]) is None


def test_variant_head_studio_file_never_links_to_live_take():
    for dur in (281, 250):  # same length and different length
        f = FileFacts("Heart-Shaped Box", 281.2, album="In Utero")
        assert match_file(f, [cand(1, "Heart-Shaped Box (Live)", dur, ["In Utero"])]) is None
        assert match_file(FileFacts("Heart-Shaped Box", 281.2),
                          [cand(2, "Heart-Shaped Box - Live at Reading", dur)]) is None


def test_variant_head_needs_same_head_and_base():
    f = FileFacts("Song (Live)", 200.0)
    assert match_file(f, [cand(1, "Song (Acoustic)", 200)]) is None
    assert match_file(f, [cand(2, "Other (Live)", 200)]) is None
    assert match_file(f, [cand(3, "Song", 200)]) is None  # live file never links to the studio take


def test_variant_head_still_needs_duration_within_tolerance():
    f = FileFacts("Song (Live)", 200.0)
    assert match_file(f, [cand(1, "Song (Live at the Forum)", 210)]) is None


# --- wider tolerance (5 s) only on the claimed album, only if nothing within 2 s --------------------------

def test_tolerance_5s_on_claimed_album():
    f = FileFacts("Heart-Shaped Box", 281.2, album="In Utero")
    assert match_file(f, [cand(1, "Heart-Shaped Box", 279, ["In Utero"])]) == MatchResult(1, "fuzzy", 0.7)


def test_tolerance_5s_boundary_and_beyond():
    f = FileFacts("Heart-Shaped Box", 284.0, album="In Utero")
    assert match_file(f, [cand(1, "Heart-Shaped Box", 279, ["In Utero"])]) is not None  # delta 5.0
    assert match_file(f, [cand(1, "Heart-Shaped Box", 278, ["In Utero"])]) is None      # delta 6.0


def test_tolerance_2_2s_on_a_different_album_is_not_matched():
    # Documents the existing rule: off the claimed album (or with no album tag) the 2 s tolerance stands,
    # so a 2.2 s delta is rejected.
    c = cand(1, "Heart-Shaped Box", 279, ["Nevermind"])
    assert match_file(FileFacts("Heart-Shaped Box", 281.2, album="In Utero"), [c]) is None
    assert match_file(FileFacts("Heart-Shaped Box", 281.2), [c]) is None


def test_tolerance_within_2s_wins_over_claimed_album_at_5s():
    f = FileFacts("Heart-Shaped Box", 281.0, album="In Utero")
    cands = [cand(1, "Heart-Shaped Box", 277, ["In Utero"]), cand(2, "Heart-Shaped Box", 280, ["Nevermind"])]
    assert match_file(f, cands) == MatchResult(2, "fuzzy", 0.8)


# --- existing behaviour is unchanged ---------------------------------------------------------------------

def test_existing_two_second_rule_unchanged():
    assert match_file(FileFacts("Song", 202.0), [cand(1, "Song", 200)]) == MatchResult(1, "fuzzy", 0.8)
    assert match_file(FileFacts("Song", 203.0), [cand(1, "Song", 200)]) is None
    assert match_file(FileFacts("Song", 202.5, album="A"), [cand(1, "Song", 200, ["B"])]) is None


def test_isrc_still_first():
    f = FileFacts("7 Words", 224.0, isrc="USABC1234567", album="Adrenaline")
    cands = [cand(1, "Adrenaline - 7 Words", 224, ["Adrenaline"]), cand(2, "Other", 10, isrc="usabc1234567")]
    assert match_file(f, cands) == MatchResult(2, "isrc", 1.0)
