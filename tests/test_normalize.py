import pytest

from cadence.identity.normalize import fold, is_various, norm_artist, norm_title, split_title

GOLDEN = [
    ("Chandelier (Instrumental Version)", None, "chandelier [instrumental]"),
    ("Chandelier", "Instrumental Version", "chandelier [instrumental]"),
    ("Jibby’s Theme", None, "jibbys theme"),
    ("Gum (Do You Want Some?)", None, "gum do you want some"),
    ("romantico(2022 Remaster)", None, "romantico"),
    ("Song (feat. X) [Remastered 2011]", None, "song"),
    ("Song - Remastered 2011", None, "song"),
    ("Pretty Dark (Demo)", None, "pretty dark [demo]"),
    ("We Appreciate Power (BloodPop® Remix)", None, "we appreciate power [bloodpop remix]"),
    ("Kiss Of Life (feat. Giovanca & Benny Sings)", None, "kiss of life"),
    ("D.T.F.N. (12inch Ver.)", None, "d t f n [12 inch]"),
    ("(Don't Fear) The Reaper", None, "dont fear the reaper"),
    ("Song (Live at Wembley)", None, "song [live at wembley]"),
    ("Song (Original Mix)", None, "song"),
]


@pytest.mark.parametrize("title,version,expected", GOLDEN)
def test_golden_norm_title(title, version, expected):
    assert norm_title(title, version) == expected


def test_apostrophe_variants_fold_equal():
    assert fold("Her's") == fold("Her’s") == "hers"


def test_other_apostrophe_glyphs():
    for ch in "‘ʼ´`":
        assert fold(f"Jibby{ch}s Theme") == "jibbys theme"


def test_case_insensitive_equal():
    assert norm_title("Roll The Dice") == norm_title("Roll the Dice")


def test_norm_artist():
    assert norm_artist("The Beatles") == "beatles"
    assert norm_artist("Earth, Wind & Fire") == "earth wind and fire"
    assert norm_artist("Beyoncé") == "beyonce"


def test_japanese_survives_fold():
    assert fold("宇多田ヒカル") == "宇多田ヒカル"
    # long-vowel mark and dakuten/handakuten must not be stripped as "diacritics"
    assert fold("キャリーぱみゅぱみゅ") == \
        "キャリーぱみゅぱみゅ"
    assert fold("がガ") == "がガ"
    assert fold("ｶﾞ") == "ガ"  # half-width ka + voiced mark -> NFKC


def test_japanese_title_with_variant():
    assert norm_title("夢のあと (Instrumental)") == "夢のあと [instrumental]"


def test_is_various():
    assert is_various("Various Artists")
    assert is_various("VA")
    assert is_various("various")
    assert not is_various("Vance")


def test_flags():
    assert split_title("Song (feat. X) [Remastered 2011]").flags == {"feat", "remaster"}
    assert split_title("Song (Explicit)").flags == {"explicit"}
    assert split_title("Song (Deluxe Edition)").flags == {"deluxe"}
    assert split_title("Song (Album Version)").flags == {"neutral"}


def test_split_title_parts():
    p = split_title("Song (Live) [Remix]")
    assert p.base == "song"
    assert p.variant == "live remix"


def test_version_field_not_duplicated_when_in_title():
    assert split_title("Chandelier (Instrumental Version)", "Instrumental Version").variant == "instrumental"


def test_trailing_suffix_needs_keyword():
    assert norm_title("Tick - Tock") == "tick tock"
    assert norm_title("Song - Live") == "song [live]"
    assert norm_title("Song - Radio Edit") == "song [radio edit]"


def test_deluxe_edition_is_not_an_edit():
    assert norm_title("Album Name (Deluxe Edition)") == "album name"


def test_unbalanced_suffix_not_peeled():
    # the dash is inside the parentheses: one group, not a trailing suffix
    assert norm_title("Song (Live - Remastered)") == "song [live remastered]"


# ---------------------------------------------------------------- edition wording (release titles only)

@pytest.mark.parametrize("title,expected", [
    ("Grace (Legacy Edition)", "grace"),
    ("Hysteria (Expanded Edition)", "hysteria"),
    ("Boss Horn (Rudy Van Gelder Edition)", "boss horn"),
    ("Blue Train (Collector's Edition)", "blue train"),
    ("Waltz For Debby (Original Jazz Classics Remasters)", "waltz for debby"),
    ("Favourite Worst Nightmare (Standard Version)", "favourite worst nightmare"),
    ("Aventine (Bonus Track Version)", "aventine"),
    ("Aventine [Bonus Tracks]", "aventine"),
    ("Blue Train (Remastered 2003/Rudy Van Gelder Edition)", "blue train"),
    ("Diamond Star Halos (With Commentary)", "diamond star halos"),   # a "with ..." group is dropped as before
])
def test_release_edition_words_are_neutral(title, expected):
    assert split_title(title, release=True).base == expected
    assert split_title(title, release=True).variant is None


def test_release_edition_flags():
    assert split_title("Grace (Legacy Edition)", release=True).flags == {"edition"}
    assert split_title("X (Original Jazz Classics Remasters)", release=True).flags == {"remaster"}
    assert split_title("X (Standard Version)", release=True).flags == {"neutral"}
    assert split_title("X", "Remastered 2003/Rudy Van Gelder Edition", release=True).base == "x"


@pytest.mark.parametrize("title,variant", [
    ("Song (Piano Version)", "piano"),
    ("Album (Live Edition)", "live edition"),
    ("Album (Remix Edition)", "remix edition"),
    ("Album (Live) (Legacy Edition)", "live"),
])
def test_variant_words_beat_edition_words(title, variant):
    assert split_title(title, release=True).variant == variant


def test_bare_version_groups_are_not_neutral():
    assert split_title("Song (Radio Version)", release=True).base == "song radio version"
    assert split_title("Song (Piano Version)", release=True).variant == "piano"
    assert split_title("Song (Single Version)", release=True).base == "song"       # existing rule


def test_track_titles_keep_edition_wording():
    # "Drumless Edition" and "Edition I / Pt. 1" identify different recordings; only release titles drop them
    assert norm_title("Touch (Drumless Edition)") == "touch drumless edition"
    assert norm_title("Ascension (Edition I / Pt. 1)") == "ascension edition i pt 1"
    assert norm_title("Grace (Legacy Edition)") == "grace legacy edition"
    assert split_title("Diamond Star Halos (With Commentary)").base == "diamond star halos"
