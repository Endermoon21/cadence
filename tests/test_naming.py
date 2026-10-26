from cadence.ledger.naming import album_dir, track_file


def test_todays_layout():
    assert album_dir("Neil Young", "Decade") == "Neil Young/Decade"
    assert track_file(3, "Deep Forbidden Lake", ".flac") == "03 - Deep Forbidden Lake.flac"
    assert track_file(12, "Hey", "m4a") == "12 - Hey.m4a"


def test_only_exfat_illegal_characters_and_trailing_dots_or_spaces_change():
    assert album_dir("Aphex Twin", "Richard D. James Album") == "Aphex Twin/Richard D. James Album"
    assert album_dir("Daft Punk", "TRON: Legacy") == "Daft Punk/TRON - Legacy"
    assert album_dir("AC/DC", "Who Made Who?") == "AC+DC/Who Made Who"
    assert album_dir("drezzdon", "forgiveness <>") == "drezzdon/forgiveness"
    assert album_dir("Someone.", "Album . ") == "Someone/Album"
    assert track_file(1, 'A "B" | C*', ".flac") == "01 - A B  C-.flac"
    assert track_file(2, "you,.", ".flac") == "02 - you,..flac"      # the extension follows, so it is legal
