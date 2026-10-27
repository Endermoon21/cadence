"""Mechanical check of the core fetch-plan and link rules over many random small ledgers (tests/ledger_gen.py).

Every check is a pure function of (seed, ledger, plan); the oracle below recomputes clusters, ownership and
fetchability with its own SQL and never calls the planner. A test loops over all seeds, collects every violation and
fails with the seeds plus the plan (planned / owned / skipped) of the first few. All randomness is
`random.Random(seed)`, so a failing seed reproduces: `from tests.ledger_gen import build; led = build(SEED)`.

Plan invariants run per artist for scope in {partial, all} x release_id in {None, every release of the artist} x
include_variants in {False, True}. (Every release, not only role 'own': naming an appears-on or hidden release with
--release is the path the planner's own skip reasons point the user to.) I2, I3, I4, I6 and I7 also run on the
all-artists plan (artist_id=None). Plan.owned is a count, not a set: the owned *set* is the oracle's, and I1/I5 compare
the planner's count with it.
"""
from __future__ import annotations

import functools
from collections import Counter
from dataclasses import dataclass

import pytest

from cadence.catalog.link import link_files
from cadence.fetch import plan as planmod
from cadence.fetch.plan import plan_missing
from tests.ledger_gen import GenLedger, build

SEEDS = range(300)
KNOWN_REASONS = {getattr(planmod, n) for n in dir(planmod) if n.startswith("R_")}
SHOW = 3  # seeds shown in full in a failure message


@dataclass(frozen=True)
class Combo:
    artist_id: int | None
    release_id: int | None
    scope: str
    include_variants: bool

    def __str__(self):
        return (f"artist_id={self.artist_id} release_id={self.release_id} scope={self.scope!r} "
                f"include_variants={self.include_variants}")


def combos(led: GenLedger):
    for scope in ("partial", "all"):
        for variants in (False, True):
            yield Combo(None, None, scope, variants)
            for a in led.artists:
                for rel in [None, *led.releases[a]]:
                    yield Combo(a, rel, scope, variants)


def run_plans(led: GenLedger) -> dict[Combo, planmod.Plan]:
    return {c: plan_missing(led.conn, c.artist_id, c.release_id, c.include_variants, scope=c.scope)
            for c in combos(led)}


@functools.lru_cache(maxsize=None)
def world(seed: int):
    led = build(seed)
    return led, run_plans(led)


# ---- oracle: own SQL, independent of cadence/fetch/plan.py ----

def cluster_of(led: GenLedger) -> dict[int, int]:
    return {r[0]: r[1] for r in led.conn.execute("SELECT id, COALESCE(canon_id, id) FROM recording")}


def owned_clusters(led: GenLedger) -> set[int]:
    cid = cluster_of(led)
    return {cid[r[0]] for r in led.conn.execute("SELECT recording_id FROM file WHERE recording_id IS NOT NULL")}


def universe(led: GenLedger, artist_id: int, release_id: int | None = None) -> set[int]:
    """Recording clusters on the artist's releases (or on release_id alone when it is given)."""
    cid = cluster_of(led)
    sql = "SELECT t.recording_id FROM track t JOIN release rel ON rel.id = t.release_id WHERE rel.artist_id = ?"
    args = [artist_id]
    if release_id is not None:
        sql += " AND rel.id = ?"
        args.append(release_id)
    return {cid[r[0]] for r in led.conn.execute(sql, args)}


def fetchable(led: GenLedger, artist_id: int, release_id: int | None) -> set[int]:
    """FETCHABLE = a cluster of the filtered universe with at least one track that has a tidal id and available = 1
    and sits on an own, not hidden release of the artist, or on the release asked for with --release. (The generator
    only writes available 0 or 1, never NULL.)"""
    cid = cluster_of(led)
    in_filter = universe(led, artist_id, release_id)
    rows = led.conn.execute(
        "SELECT t.recording_id FROM track t JOIN release rel ON rel.id = t.release_id WHERE rel.artist_id = ?1 "
        "AND t.tidal_id IS NOT NULL AND t.available = 1 "
        "AND ((rel.role = 'own' AND rel.hidden_reason IS NULL) OR rel.id = ?2)", (artist_id, release_id))
    return {cid[r[0]] for r in rows} & in_filter


# ---- failure reporting ----

def dump(led: GenLedger, plan: planmod.Plan) -> str:
    cid = cluster_of(led)
    out = [f"  planned ({len(plan.items)}):"]
    out += [f"    cluster {cid[i.recording_id]} rec {i.recording_id} tidal {i.tidal_track_id} rel {i.release_id} "
            f"{i.disc}-{i.number} {i.title!r} -> {i.target_rel}" for i in plan.items]
    oc = sorted(owned_clusters(led))
    out.append(f"  owned: plan.owned={plan.owned}; oracle owned clusters (all artists)={oc}")
    out.append(f"  skipped ({len(plan.skipped)}):")
    out += [f"    cluster {cid[s.recording_id]} rec {s.recording_id} tidal {s.tidal_track_id} {s.release!r} "
            f"{s.disc}-{s.number} {s.title!r}  ({s.reason})" for s in plan.skipped]
    files = led.conn.execute("SELECT id, path, recording_id, match FROM file ORDER BY id").fetchall()
    out.append("  files: " + "; ".join(f"#{f['id']} {f['path']!r} rec={f['recording_id']} {f['match']}"
                                       for f in files))
    return "\n".join(out)


def check_plans(name: str, rule: str, check, *, per_artist_only: bool = False):
    """check(led, combo, plan) -> list of violation strings. Fails with every seed that has one."""
    failures = []
    for seed in SEEDS:
        led, plans = world(seed)
        for c, p in plans.items():
            if per_artist_only and c.artist_id is None and c.release_id is None:
                continue
            for v in check(led, c, p):
                failures.append((seed, c, p, v))
    report(name, rule, failures)


def report(name: str, rule: str, failures: list):
    if not failures:
        return
    seeds = sorted({f[0] for f in failures})
    msg = [f"{name} violated: {rule}", f"{len(failures)} violation(s) on {len(seeds)}/{len(SEEDS)} seeds: {seeds}"]
    shown = set()
    for seed, c, p, v in failures:
        if seed in shown or len(shown) >= SHOW:
            continue
        shown.add(seed)
        msg.append(f"--- seed {seed}: {c}\n  {v}")
        if p is not None:
            msg.append(dump(world(seed)[0], p))
    pytest.fail("\n".join(msg), pytrace=False)


# ---- plan-level invariants ----

def buckets(led, plan):
    cid = cluster_of(led)
    return (Counter(cid[i.recording_id] for i in plan.items), Counter(cid[s.recording_id] for s in plan.skipped))


def test_I1_partition_of_fetchable_clusters():
    """Every fetchable cluster of the filtered universe is in exactly one of planned / owned / skipped."""
    def check(led, c, p):
        planned, skipped = buckets(led, p)
        owned = owned_clusters(led)
        fetch = fetchable(led, c.artist_id, c.release_id)
        out = []
        for k in sorted(fetch):
            where = ["planned"] * planned[k] + ["skipped"] * skipped[k] + (["owned"] if k in owned else [])
            if len(where) != 1:
                out.append(f"fetchable cluster {k} is in {where or 'NO bucket (silently dropped)'}")
        out += [f"planned cluster {k} is not fetchable" for k in sorted(planned) if k not in fetch]
        return out
    check_plans("I1", "fetchable cluster in exactly one of planned / owned / skipped", check, per_artist_only=True)


def test_I1b_partition_of_every_cluster_of_the_artist():
    """The planner's own promise (module docstring): every recording cluster of the artist, fetchable or not, lands in
    exactly one bucket, and the three bucket sizes add up to the artist's cluster count."""
    def check(led, c, p):
        a = c.artist_id if c.artist_id is not None else led.conn.execute(
            "SELECT artist_id FROM release WHERE id = ?", (c.release_id,)).fetchone()[0]
        uni = universe(led, a)
        planned, skipped = buckets(led, p)
        owned = owned_clusters(led) & uni
        out = []
        for k in sorted(uni):
            n = planned[k] + skipped[k] + (k in owned)
            if n != 1:
                out.append(f"cluster {k}: planned {planned[k]}x, skipped {skipped[k]}x, owned {k in owned}")
        stray = (set(planned) | set(skipped)) - uni
        if stray:
            out.append(f"clusters outside the artist's universe: {sorted(stray)}")
        total = len(p.items) + len(p.skipped) + p.owned
        if total != len(uni):
            out.append(f"planned {len(p.items)} + skipped {len(p.skipped)} + owned {p.owned} = {total}, "
                       f"but the artist has {len(uni)} clusters {sorted(uni)}")
        return out
    check_plans("I1b", "every cluster of the artist in exactly one bucket", check, per_artist_only=True)


def test_I2_every_skip_has_a_known_reason():
    def check(led, c, p):
        return [f"skip of rec {s.recording_id} {s.title!r} has reason {s.reason!r}" for s in p.skipped
                if not s.reason or (s.reason not in KNOWN_REASONS and not s.reason.startswith(planmod.R_SHARED + " "))]
    assert KNOWN_REASONS and all(KNOWN_REASONS)
    check_plans("I2", f"skip reason non-empty and one of {sorted(KNOWN_REASONS)}", check)


def test_I3_owned_is_never_planned():
    def check(led, c, p):
        cid, owned = cluster_of(led), owned_clusters(led)
        return [f"planned rec {i.recording_id} (cluster {cid[i.recording_id]}, tidal {i.tidal_track_id}) has a linked "
                f"file: {[tuple(r) for r in led.conn.execute('SELECT f.id, f.path FROM file f JOIN recording r ON r.id = f.recording_id WHERE COALESCE(r.canon_id, r.id) = ?', (cid[i.recording_id],))]}"
                for i in p.items if cid[i.recording_id] in owned]
    check_plans("I3", "no planned item's cluster has a file with recording_id set", check)


def test_I4_no_tidal_id_planned_twice():
    def check(led, c, p):
        return [f"tidal id {t} planned {n}x" for t, n in Counter(i.tidal_track_id for i in p.items).items() if n > 1]
    check_plans("I4", "no tidal id twice among planned items", check)


def test_I5_ownership_is_filter_independent():
    failures = []
    for seed in SEEDS:
        led, plans = world(seed)
        owned = owned_clusters(led)
        for a in led.artists:
            expect = len(owned & universe(led, a))
            seen = {c: p.owned for c, p in plans.items() if c.artist_id == a}
            for c, n in seen.items():
                if n != expect:
                    failures.append((seed, c, plans[c], f"owned={n} here, oracle owned set of artist {a} has {expect}; "
                                     f"owned counts across all combos of this artist: {sorted(set(seen.values()))}"))
    report("I5", "owned set of an artist identical across scope / release_id / include_variants", failures)


def test_I6_determinism():
    failures = []
    for seed in SEEDS:
        led, plans = world(seed)
        again = run_plans(build(seed))  # a second, separately built copy of the same ledger
        for c, p in plans.items():
            if p != again[c] or p != plan_missing(led.conn, c.artist_id, c.release_id, c.include_variants,
                                                  scope=c.scope):
                failures.append((seed, c, p, "second planning differs"))
    report("I6", "planning the same ledger twice gives identical output", failures)


def test_I7_path_collisions_are_listed_in_the_plan():
    def check(led, c, p):
        linked = {r[0].casefold() for r in led.conn.execute("SELECT path FROM file WHERE recording_id IS NOT NULL")}
        expect = sorted((i.tidal_track_id, i.target_rel, "linked") for i in p.items if i.target_rel.casefold() in linked)
        seen, dups = {}, []
        for i in p.items:  # a later item with the target of an earlier one (case-insensitive) is a duplicate target
            if i.target_rel.casefold() in seen:
                dups.append((i.tidal_track_id, seen[i.target_rel.casefold()], "duplicate"))
            seen.setdefault(i.target_rel.casefold(), i.target_rel)
        expect = sorted([*expect, *dups], key=lambda e: (e[1], e[0], e[2]))
        listed = [(x.tidal_track_id, x.target, x.kind) for x in p.collisions]
        out = [f"planned tidal {t} target {path!r} ({kind}) is not in Plan.collisions"
               for t, path, kind in expect if (t, path, kind) not in listed]
        out += [f"Plan.collisions entry tidal {t} {path!r} ({kind}) matches no planned item with a linked or duplicate target"
                for t, path, kind in listed if (t, path, kind) not in expect]
        out += [f"Plan.collisions entry {x!r} file_path differs from its target" for x in p.collisions
                if x.file_path != x.target]
        if len(listed) != len(set(listed)) or listed != sorted(listed, key=lambda e: (e[1], e[0], e[2])):
            out.append(f"Plan.collisions is not unique and sorted by (target, tidal id, kind): {listed}")
        return out
    check_plans("I7", "Plan.collisions is exactly the planned items whose target_rel equals a linked file's path, "
                "plus each later planned item sharing a target with an earlier one", check)


# ---- link-level invariants ----

def link_runs(seed: int):
    """(before, after, ledger) for rematch False and True, each on its own copy of the seeded ledger."""
    for rematch in (False, True):
        led = build(seed)
        snap = lambda: {r["id"]: tuple(r) for r in led.conn.execute(  # noqa: E731
            "SELECT id, path, artist_id, recording_id, match, match_score FROM file ORDER BY id")}
        before = snap()
        link_files(led.conn, rematch=rematch)
        yield rematch, before, snap(), led


def report_link(name: str, rule: str, check):
    failures = []
    for seed in SEEDS:
        for rematch, before, after, led in link_runs(seed):
            failures += [(seed, f"rematch={rematch}", None, v) for v in check(led, before, after)]
    if failures:
        seeds = sorted({f[0] for f in failures})
        msg = [f"{name} violated: {rule}", f"{len(failures)} violation(s) on {len(seeds)}/{len(SEEDS)} seeds: {seeds}"]
        for seed, c, _, v in failures[:SHOW * 2]:
            msg.append(f"--- seed {seed} {c}: {v}")
        pytest.fail("\n".join(msg), pytrace=False)


def test_L1_ambiguous_never_linked():
    def check(led, before, after):
        rows = led.conn.execute("SELECT f.id, f.path, f.recording_id, f.match FROM link_review r "
                                "JOIN file f ON f.id = r.file_id").fetchall()
        return [f"file #{r['id']} {r['path']!r} has a link_review row but recording_id={r['recording_id']} "
                f"match={r['match']!r} (before link: {before[r['id']][3:]})"
                for r in rows if (r["recording_id"] is not None or r["match"] != "unmatched")
                and after[r["id"]][3:] != before[r["id"]][3:]]  # an existing link is kept, never made or changed
    report_link("L1", "file with a link_review row is unlinked (NULL / 'unmatched') or keeps the link it had before", check)


def test_L2_linked_means_a_candidate():
    def check(led, before, after):
        out = []
        for fid, row in after.items():
            if row[3:5] == before[fid][3:5] or row[3] is None:
                continue  # not (re)linked by this run
            ok = led.conn.execute(
                "SELECT 1 WHERE ?2 IN (SELECT id FROM recording WHERE artist_id = ?1 UNION "
                "SELECT t.recording_id FROM track t JOIN release r ON r.id = t.release_id WHERE r.artist_id = ?1)",
                (row[2], row[3])).fetchone()
            if not ok:
                out.append(f"file #{fid} {row[1]!r} of artist {row[2]} was linked to rec {row[3]} ({row[4]}), "
                           f"which is neither the artist's own nor credited to them")
        return out
    report_link("L2", "a file linked by link_files has a recording in the artist's own-or-credited set", check)


def test_L3_manual_never_touched():
    def check(led, before, after):
        return [f"manual file #{fid} changed (recording_id, match, match_score) {b[3:]} -> {after[fid][3:]}"
                for fid, b in before.items() if b[4] == "manual" and after[fid][3:] != b[3:]]
    report_link("L3", "match='manual' keeps recording_id and match", check)
