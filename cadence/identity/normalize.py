"""Name and title normalisation used for identity matching."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Applied *before* NFKC: NFKC turns U+00B4 into "space + combining acute".
_PRE = {ord(c): "'" for c in "’‘ʼ´`"}
_PRE.update({ord("“"): '"', ord("”"): '"', ord("–"): "-", ord("—"): "-"})

# Only strip Latin/Greek/Cyrillic-style combining diacritics. A blanket "drop Mn" would
# also strip kana dakuten (U+3099/U+309A) and Indic signs, which are part of the letter.
_DIACRITIC_RANGES = ((0x0300, 0x036F), (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF),
                     (0x20D0, 0x20FF), (0xFE20, 0xFE2F))

VARIANT_WORDS = (
    "instrumental", "remix", "mix", "live", "demo", "acoustic", "edit", "extended",
    "slowed", "sped up", "reverb", "acapella", "a cappella", "12inch", "12 inch",
    "7inch", "rework", "bootleg", "stripped", "piano version", "orchestral",
    "session", "nightcore",
)
_VARIANT_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in VARIANT_WORDS) + r")\b")
_FEAT_RE = re.compile(r"^(?:feat|ft|featuring)\b")
_DELUXE_RE = re.compile(r"\b(?:deluxe|expanded|anniversary|special edition)\b")
_SUFFIX_KEYWORD_RE = re.compile(
    r"\b(?:remaster\w*|live|remix\w*|mix|edit|version|demo|instrumental|acoustic|mono|stereo)\b")
_GROUP_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_YEAR_RE = re.compile(r"^\d{4}$")

_NEUTRAL_EXACT = {
    "explicit": "explicit", "explicit version": "explicit",
    "clean": "clean", "clean version": "clean",
    "album version": "neutral", "original mix": "neutral", "original version": "neutral",
    "single version": "neutral", "mono": "neutral", "stereo": "neutral",
    "mono version": "neutral", "stereo version": "neutral", "bonus track": "neutral",
}


def _is_diacritic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DIACRITIC_RANGES)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s.translate(_PRE)).casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not _is_diacritic(c))
    s = unicodedata.normalize("NFC", s)  # recompose kana / hangul
    s = s.replace("'", "").replace("&", " and ")
    s = "".join(c if c.isalnum() else " " for c in s)
    return " ".join(s.split())


def norm_artist(name: str) -> str:
    n = fold(name)
    return n[4:] if n.startswith("the ") else n


def is_various(name: str) -> bool:
    return fold(name) in {"various artists", "various", "va"}


@dataclass(frozen=True)
class TitleParts:
    base: str
    variant: str | None
    flags: frozenset[str]


def _neutral_flag(g: str) -> str | None:
    """Flag name if the folded group is a neutral (identity-irrelevant) annotation."""
    tokens = g.split()
    if any(t.startswith("remaster") for t in tokens):
        rest = [t for t in tokens if not t.startswith("remaster")]
        if all(_YEAR_RE.match(t) or t in {"version", "digital"} for t in rest):
            return "remaster"
    if g in _NEUTRAL_EXACT:
        return _NEUTRAL_EXACT[g]
    if _DELUXE_RE.search(g) and not _VARIANT_RE.search(g):
        return "deluxe"
    return None


def _variant_label(g: str) -> str:
    words = ["12 inch" if t == "12inch" else t for t in g.split() if t not in {"version", "ver"}]
    return " ".join(words)


def _classify(group: str) -> tuple[str, str] | None:
    """('flag', name) | ('variant', label) | None (group stays in the base title)."""
    g = fold(group)
    if _FEAT_RE.match(g) or g.startswith("with "):
        return ("flag", "feat")
    flag = _neutral_flag(g)
    if flag:
        return ("flag", flag)
    if _VARIANT_RE.search(g):
        return ("variant", _variant_label(g))
    return None


def _unbalanced(s: str) -> bool:
    return s.count(")") > s.count("(") or s.count("]") > s.count("[")


def _peel_suffixes(title: str) -> tuple[str, list[str]]:
    """Split trailing ' - suffix' parts that contain a known keyword; returns (head, suffixes in order)."""
    suffixes: list[str] = []
    while " - " in title:
        head, _, tail = title.rpartition(" - ")
        if not head.strip() or _unbalanced(tail) or not _SUFFIX_KEYWORD_RE.search(fold(tail)):
            break
        suffixes.append(tail)
        title = head
    return title, suffixes[::-1]


def split_title(title: str, version: str | None = None) -> TitleParts:
    flags: set[str] = set()
    variants: list[str] = []
    kept_tail: list[str] = []

    def take(group: str) -> bool:
        c = _classify(group)
        if c is None:
            return False
        if c[0] == "flag":
            flags.add(c[1])
        else:
            variants.append(c[1])
        return True

    def repl(m: re.Match) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return " " if take(inner) else f" {inner} "

    text, suffixes = _peel_suffixes(title.translate(_PRE))
    text = _GROUP_RE.sub(repl, text)
    extra = list(suffixes)
    if version and f" {fold(version)} " not in f" {fold(title)} ":
        extra.append(version)
    for g in extra:
        if not take(g):
            kept_tail.append(g)
    return TitleParts(fold(" ".join([text, *kept_tail])),
                      " ".join(variants) or None, frozenset(flags))


def norm_title(title: str, version: str | None = None) -> str:
    p = split_title(title, version)
    return p.base if p.variant is None else f"{p.base} [{p.variant}]"
