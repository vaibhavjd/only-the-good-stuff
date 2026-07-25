#!/usr/bin/env python3
"""
Movie Finder - personal weekly digest + static discovery app (v2).

Zero-dependency (Python standard library only). Finds genuinely good movies AND
TV shows (Indian regional + international) using PER-CATEGORY IMDb vote baselines
(language x kind strata), so a title is judged against the vote norms of its own
category rather than one flat bar. Optionally maps each pick to the Indian OTT
platform that streams it (TMDB) and tags its true original language (TMDB), then
emits the static app (out/index.html + out/data.js) and a weekly digest as HTML +
Markdown (optionally pushed to Telegram).

Data source: IMDb non-commercial datasets (https://datasets.imdbws.com) - free,
for personal / non-commercial use only.
Attribution: Information courtesy of IMDb (https://www.imdb.com). Used with permission.

IMPORTANT ON LANGUAGE: IMDb's free datasets have NO reliable original-language
field for Indian films - original titles are romanized, and akas language codes are
dominated by dub tracks (e.g. Oppenheimer carries hi/ta/te/kn/bn/mr dub codes). So
without a TMDB key, language buckets are COARSE and best-effort. Add a free TMDB key
(config.json) to accurately tag each shown film's language via original_language.

This is Phase 0 of the Movie Finder blueprint: a personal tool to validate the
premise. It is NOT licensed for a public/commercial product as-is.

Usage:
    python movie_finder.py run --open   # fetch (if stale) + build + score + digest
    python movie_finder.py fetch        # download/refresh the IMDb datasets
    python movie_finder.py build        # load datasets into SQLite + score
    python movie_finder.py digest       # rebuild the digest from the current DB
    python movie_finder.py send         # rebuild + push to Telegram (if configured)

Common flags:  --refresh  --years N  --threshold 6.5  --no-tmdb  --open
"""

import argparse
import bisect
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import html as html_lib
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT_DIR = os.path.join(HERE, "out")
DB_PATH = os.path.join(HERE, "moviefinder.db")
CONFIG_PATH = os.path.join(HERE, "config.json")

IMDB_BASE = "https://datasets.imdbws.com/"
IMDB_FILES = {
    "basics": "title.basics.tsv.gz",
    "ratings": "title.ratings.tsv.gz",
    "akas": "title.akas.tsv.gz",
}

INDIAN_LANG_PRIORITY = ["ml", "ta", "te", "kn", "bn", "mr", "hi", "pa", "gu", "or", "as"]
INDIAN_LANGS = set(INDIAN_LANG_PRIORITY)
LANG_NAMES = {
    "ml": "Malayalam", "ta": "Tamil", "te": "Telugu", "kn": "Kannada",
    "bn": "Bengali", "mr": "Marathi", "hi": "Hindi", "pa": "Punjabi",
    "gu": "Gujarati", "or": "Odia", "as": "Assamese",
    "other_in": "Indian (other/unclassified)", "intl": "International",
}
KIND_BY_TYPE = {"movie": "m", "tvSeries": "s", "tvMiniSeries": "s"}
KIND_LABEL = {"m": "movies", "s": "series"}
COUNTRY_NAMES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "KR": "South Korea",
    "JP": "Japan", "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "CA": "Canada", "AU": "Australia", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan",
    "TR": "Turkey", "IR": "Iran", "BR": "Brazil", "MX": "Mexico", "RU": "Russia",
    "TH": "Thailand", "ID": "Indonesia", "PK": "Pakistan", "BD": "Bangladesh",
    "LK": "Sri Lanka", "NP": "Nepal", "AR": "Argentina", "DK": "Denmark",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "NL": "Netherlands",
    "BE": "Belgium", "PL": "Poland", "IE": "Ireland", "NZ": "New Zealand",
    "ZA": "South Africa", "EG": "Egypt", "IL": "Israel",
}
KNOWN_INTL_LANGS = {"en", "ko", "ja", "es", "fr", "it", "de", "zh", "cn",
                    "ru", "pt", "tr", "fa", "th", "sv", "da", "nl", "pl"}
WESTERN_REGIONS = {"US", "GB", "CA", "AU", "NZ", "IE"}

DEFAULTS = {
    "rating_threshold": 5.5,   # Gate 2: weighted rating must clear this (also the app's min-rating floor)
    "recent_years": 2,         # digest covers releases from the last N years (incl. current)
    "vote_floor_min": 500,     # clamp for Gate 1 vote floor (P35 of stratum)
    "vote_floor_max": 10000,
    "prior_min": 1500,         # clamp for the Bayesian prior m (P60 of stratum)
    "prior_max": 25000,
    "ref_window_years": 10,    # baselines computed over the last N years
    "ref_vote_cutoff": 100,    # noise cutoff for the reference set
    "min_ref_titles": 40,      # below this, a bucket rolls up to the GLOBAL baseline
    "per_lang_cap": 30,        # max titles shown per Indian language in the digest
    "intl_cap": 50,            # max international titles shown
    "enrich_limit": 60,        # (no-key/display path) top titles to look up on TMDB
    "pool_floor": 150,         # (TMDB gate) min IMDb votes to enter the candidate pool
    "pool_cap": 500,           # (TMDB gate) max pool titles to enrich per run
    # Caps are a SAFETY VALVE, not the filter. They used to bind hard (6000 movies vs ~27k
    # qualifying), so `ORDER BY rating DESC LIMIT` set the real floor at ~6.6 while the UI
    # claimed 5.5. Keep them well above the qualifying counts so rating_threshold is the gate.
    "browse_vote_floor": 2000, # (catalog) min IMDb votes for a movie to enter the app catalog
    "browse_cap": 40000,       # (catalog) headroom over ~27k qualifying movies
    "shows_vote_floor": 1000,  # (catalog) min IMDb votes for a TV show to enter the app catalog
    "shows_cap": 20000,        # (catalog) headroom over ~12k qualifying shows
    "browse_workers": 8,       # concurrent TMDB lookups when building the catalog
    "sentiment_cap": 1500,     # (sentiment) how many top-voted films to fetch reviews for
    "sentiment_reviews": 25,   # (sentiment) max reviews analyzed per film
    "supabase_url": "",        # optional: cloud profiles + Google sign-in (https://<ref>.supabase.co)
    "supabase_anon_key": "",   # optional: Supabase anon/publishable key (public by design, guarded by RLS)
    "tmdb_api_key": "",        # optional (v3 key)
    "tmdb_bearer": "",         # optional (v4 read access token) - takes precedence
    "telegram_bot_token": "",  # optional
    "telegram_chat_id": "",    # optional
}

NULL = "\\N"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg.update({k: (v.strip() if isinstance(v, str) else v)
                        for k, v in raw.items()
                        if not k.startswith("_") and v not in (None, "")})
        except Exception as e:
            log(f"warning: could not read config.json ({e}); using defaults")
    return cfg


def safe_int(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def safe_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def percentile(sorted_list, p):
    if not sorted_list:
        return 0
    if len(sorted_list) == 1:
        return sorted_list[0]
    k = (len(sorted_list) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_list) - 1)
    return sorted_list[f] + (sorted_list[c] - sorted_list[f]) * (k - f)


def pct_rank(sorted_list, v):
    if not sorted_list:
        return 0.0
    return 100.0 * bisect.bisect_right(sorted_list, v) / len(sorted_list)


def trimmed_mean(xs, frac=0.05):
    if not xs:
        return 0.0
    s = sorted(xs)
    cut = int(len(s) * frac)
    core = s[cut:len(s) - cut] if len(s) - 2 * cut > 0 else s
    return sum(core) / len(core)


def fmt_votes(n):
    return f"{n:,}"


# --------------------------------------------------------------------------- #
# 1. fetch
# --------------------------------------------------------------------------- #
def fetch(refresh=False, max_age_h=24):
    os.makedirs(DATA_DIR, exist_ok=True)
    for fname in IMDB_FILES.values():
        dest = os.path.join(DATA_DIR, fname)
        if not refresh and os.path.exists(dest):
            age_h = (time.time() - os.path.getmtime(dest)) / 3600
            if age_h < max_age_h:
                mb = os.path.getsize(dest) // (1024 * 1024)
                log(f"cached {fname} ({mb} MB, {age_h:.1f}h old)")
                continue
        url = IMDB_BASE + fname
        log(f"downloading {fname} ...")
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "MovieFinderPhase0/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            done = last = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total and done - last > (40 << 20):
                    last = done
                    log(f"  {fname}: {done // (1024*1024)}/{total // (1024*1024)} MB")
        os.replace(tmp, dest)
        log(f"done {fname} ({os.path.getsize(dest) // (1024*1024)} MB)")


# --------------------------------------------------------------------------- #
# 2. build (load into SQLite)
# --------------------------------------------------------------------------- #
def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def create_schema(con):
    con.executescript(
        """
        DROP TABLE IF EXISTS titles;
        DROP TABLE IF EXISTS ratings;
        DROP TABLE IF EXISTS title_lang;
        DROP TABLE IF EXISTS baselines;
        DROP TABLE IF EXISTS scores;
        CREATE TABLE titles (
            tconst TEXT PRIMARY KEY, primary_title TEXT, original_title TEXT,
            kind TEXT, start_year INTEGER, end_year INTEGER, runtime_min INTEGER, genres TEXT
        );
        CREATE TABLE ratings (tconst TEXT PRIMARY KEY, avg_rating REAL, num_votes INTEGER);
        CREATE TABLE title_lang (tconst TEXT PRIMARY KEY, language_bucket TEXT, market TEXT);
        CREATE TABLE baselines (
            bucket TEXT, kind TEXT, ref_count INTEGER, v_min INTEGER,
            m INTEGER, c REAL, p90 INTEGER, rolled INTEGER,
            PRIMARY KEY (bucket, kind)
        );
        CREATE TABLE scores (
            tconst TEXT PRIMARY KEY, bucket TEXT, kind TEXT, market TEXT, wr REAL,
            eligible INTEGER, confidence TEXT, vote_percentile REAL
        );
        CREATE TABLE IF NOT EXISTS digest_history (tconst TEXT PRIMARY KEY, first_surfaced TEXT);
        """
    )


def iter_tsv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        f.readline()  # header
        for line in f:
            yield line.rstrip("\n").split("\t")


def load_basics(con):
    path = os.path.join(DATA_DIR, IMDB_FILES["basics"])
    log("loading titles (movies + tv series) ...")
    cur = con.cursor()
    batch = []
    scanned = 0
    kept = Counter()
    for row in iter_tsv(path):
        if len(row) < 9:
            continue
        scanned += 1
        kind = KIND_BY_TYPE.get(row[1])
        if kind is None or row[4] == "1":  # keep features + series, drop adult
            continue
        batch.append((row[0], row[2], row[3], kind,
                      safe_int(row[5]) if row[5] != NULL else None,
                      safe_int(row[6]) if row[6] != NULL else None,
                      safe_int(row[7]) if row[7] != NULL else None,
                      "" if row[8] == NULL else row[8]))
        kept[kind] += 1
        if len(batch) >= 50000:
            cur.executemany("INSERT OR REPLACE INTO titles VALUES(?,?,?,?,?,?,?,?)", batch)
            batch.clear()
        if scanned % 2000000 == 0:
            log(f"  basics scanned {scanned:,}")
    if batch:
        cur.executemany("INSERT OR REPLACE INTO titles VALUES(?,?,?,?,?,?,?,?)", batch)
    con.commit()
    log(f"  kept: {kept['m']:,} movies + {kept['s']:,} series")


def load_ratings(con):
    path = os.path.join(DATA_DIR, IMDB_FILES["ratings"])
    log("loading ratings ...")
    cur = con.cursor()
    batch = []
    n = 0
    for row in iter_tsv(path):
        if len(row) < 3:
            continue
        ar = safe_float(row[1])
        nv = safe_int(row[2])
        if ar is None or nv is None:
            continue
        batch.append((row[0], ar, nv))
        n += 1
        if len(batch) >= 50000:
            cur.executemany("INSERT OR REPLACE INTO ratings VALUES(?,?,?)", batch)
            batch.clear()
    if batch:
        cur.executemany("INSERT OR REPLACE INTO ratings VALUES(?,?,?)", batch)
    con.commit()
    log(f"  ratings loaded: {n:,}")


def classify(langs, regions, orig_lang):
    """Best-effort IMDb-only language bucket.

    IMDb's free datasets have no reliable original-language field for Indian films:
    original titles are romanized, and akas language codes are dominated by DUB tracks
    (Hollywood films carry hi/ta/te/kn/bn/mr dub codes). So this is deliberately coarse,
    and it errs toward calling globally-distributed films 'international'. Accurate
    per-language tagging needs TMDB original_language (free key). See module docstring.
    """
    western = bool(regions & WESTERN_REGIONS)
    if orig_lang in INDIAN_LANGS:
        return orig_lang, "indian"
    if orig_lang in KNOWN_INTL_LANGS:
        return "intl", "international"
    # English + western distribution => international, even if Indian DUB codes exist.
    # This strips the ~23k Hollywood-dub false positives out of the Indian buckets.
    if "en" in langs and western:
        return "intl", "international"
    indian_codes = [l for l in langs if l in INDIAN_LANGS]
    if indian_codes:
        best = max(indian_codes, key=lambda l: (langs[l], -INDIAN_LANG_PRIORITY.index(l)))
        return best, "indian"
    if "IN" in regions and not western:
        return "other_in", "indian"
    return "intl", "international"


def load_akas(con):
    log("indexing title ids for language bucketing ...")
    title_ids = set(r[0] for r in con.execute("SELECT tconst FROM titles"))
    log(f"  {len(title_ids):,} title ids in memory")
    path = os.path.join(DATA_DIR, IMDB_FILES["akas"])
    log("loading akas (streaming, grouped by title) ...")
    cur = con.cursor()
    batch = []
    scanned = 0
    cur_id = None
    langs = Counter()
    regions = set()
    orig_lang = None

    def flush(tid):
        if tid is None or tid not in title_ids:
            return
        b, m = classify(langs, regions, orig_lang)
        batch.append((tid, b, m))

    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        f.readline()
        for line in f:
            scanned += 1
            p = line.rstrip("\n").split("\t")
            if len(p) < 8:
                continue
            tid = p[0]
            if tid != cur_id:
                flush(cur_id)
                if len(batch) >= 50000:
                    cur.executemany("INSERT OR REPLACE INTO title_lang VALUES(?,?,?)", batch)
                    batch.clear()
                cur_id = tid
                langs = Counter()
                regions = set()
                orig_lang = None
            if tid in title_ids:
                region, language, is_orig = p[3], p[4], p[7]
                if region and region != NULL:
                    regions.add(region)
                if language and language != NULL:
                    langs[language] += 1
                    if is_orig == "1" and orig_lang is None:
                        orig_lang = language
            if scanned % 10000000 == 0:
                log(f"  akas scanned {scanned:,}")
        flush(cur_id)
    if batch:
        cur.executemany("INSERT OR REPLACE INTO title_lang VALUES(?,?,?)", batch)
    con.commit()
    log(f"  akas scanned total: {scanned:,}")


def build(con):
    create_schema(con)
    load_basics(con)
    load_ratings(con)
    load_akas(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_titles_year ON titles(start_year)")
    con.commit()


# --------------------------------------------------------------------------- #
# 3. compute baselines + scores  (the quality gate)
# --------------------------------------------------------------------------- #
def compute(con, cfg):
    cur_year = datetime.now().year
    ref_from = cur_year - cfg["ref_window_years"]
    log("loading rated titles for baseline computation ...")
    rows = con.execute(
        """
        SELECT t.tconst, t.kind, t.start_year, r.avg_rating, r.num_votes,
               COALESCE(l.language_bucket,'intl'), COALESCE(l.market,'international')
        FROM titles t JOIN ratings r ON r.tconst = t.tconst
        LEFT JOIN title_lang l ON l.tconst = t.tconst
        """
    ).fetchall()
    log(f"  {len(rows):,} rated titles")

    ref = defaultdict(list)  # (bucket, kind) -> [(votes, rating)]
    for tc, kind, sy, ar, nv, bucket, market in rows:
        if sy and sy >= ref_from and nv >= cfg["ref_vote_cutoff"]:
            ref[(bucket, kind)].append((nv, ar))

    def baseline_from(pairs):
        votes = sorted(p[0] for p in pairs)
        vmin = int(clamp(percentile(votes, 35), cfg["vote_floor_min"], cfg["vote_floor_max"]))
        m = int(clamp(percentile(votes, 60), cfg["prior_min"], cfg["prior_max"]))
        c_ratings = [r for (v, r) in pairs if v >= vmin] or [r for (_, r) in pairs]
        c = trimmed_mean(c_ratings, 0.05)
        return {"votes": votes, "vmin": vmin, "m": m, "c": round(c, 3),
                "p90": int(percentile(votes, 90)), "n": len(pairs)}

    gb = {}  # per-kind GLOBAL roll-up target
    for kind in ("m", "s"):
        pairs = [x for (b, k), lst in ref.items() if k == kind for x in lst]
        gb[kind] = baseline_from(pairs) if pairs else {
            "votes": [], "vmin": cfg["vote_floor_min"], "m": cfg["prior_min"],
            "c": 6.0, "p90": 0, "n": 0}
    bl = {}
    for (bucket, kind), pairs in ref.items():
        if len(pairs) >= cfg["min_ref_titles"]:
            d = baseline_from(pairs)
            d["rolled"] = 0
            bl[(bucket, kind)] = d
        else:
            bl[(bucket, kind)] = dict(gb[kind], n=len(pairs), rolled=1)
    for kind in ("m", "s"):
        bl[("GLOBAL", kind)] = dict(gb[kind], rolled=0)

    con.execute("DELETE FROM baselines")
    con.executemany(
        "INSERT OR REPLACE INTO baselines VALUES(?,?,?,?,?,?,?,?)",
        [(b, k, d["n"], d["vmin"], d["m"], d["c"], d["p90"], d.get("rolled", 0))
         for (b, k), d in bl.items()],
    )

    log("per-category baselines (last %dy, votes>=%d):" % (cfg["ref_window_years"], cfg["ref_vote_cutoff"]))
    print(f"    {'stratum':<34}{'ref#':>8}{'v_min':>8}{'m':>8}{'C':>7}{'P90':>9}   note")
    for (b, k), d in sorted(bl.items(), key=lambda kv: (kv[0][1], -kv[1]["n"])):
        if b == "GLOBAL":
            continue
        note = "rolled->GLOBAL" if d.get("rolled") else ""
        label = f"{LANG_NAMES.get(b, b)} ({KIND_LABEL[k]})"
        print(f"    {label:<34}{d['n']:>8}{d['vmin']:>8}{d['m']:>8}"
              f"{d['c']:>7.2f}{d['p90']:>9}   {note}")

    thr = cfg["rating_threshold"]
    score_rows = []
    for tc, kind, sy, ar, nv, bucket, market in rows:
        d = bl.get((bucket, kind)) or gb[kind]
        d_use = gb[kind] if d.get("rolled") else d
        v, R, m, C, vmin = nv, ar, d_use["m"], d_use["c"], d_use["vmin"]
        wr = (v / (v + m)) * R + (m / (v + m)) * C
        eligible = 1 if (v >= vmin and wr >= thr) else 0
        vp = pct_rank(d_use["votes"], v) if d_use["votes"] else 0.0
        conf = "confirmed" if (v >= m and sy and sy <= cur_year - 1) else "provisional"
        score_rows.append((tc, bucket, kind, market, round(wr, 2), eligible, conf, round(vp, 1)))

    con.execute("DELETE FROM scores")
    cur = con.cursor()
    for i in range(0, len(score_rows), 50000):
        cur.executemany("INSERT OR REPLACE INTO scores VALUES(?,?,?,?,?,?,?,?)",
                        score_rows[i:i + 50000])
    con.commit()
    elig = sum(1 for r in score_rows if r[5])
    log(f"  scored {len(score_rows):,} titles; {elig:,} pass the two-gate quality bar")


# --------------------------------------------------------------------------- #
# 4. TMDB enrichment (optional): accurate original_language + India availability
# --------------------------------------------------------------------------- #
def tmdb_get(path, params, cfg, _retry=2):
    params = dict(params)
    headers = {"User-Agent": "MovieFinderPhase0/1.0"}
    if cfg.get("tmdb_bearer"):
        headers["Authorization"] = "Bearer " + cfg["tmdb_bearer"]
    elif cfg.get("tmdb_api_key"):
        params["api_key"] = cfg["tmdb_api_key"]
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry > 0:   # rate limited - back off and retry
            time.sleep(1.5)
            return tmdb_get(path, params, cfg, _retry - 1)
        raise


def year_of(datestr):
    """'2019-04-12' -> 2019 (None if absent/malformed)."""
    return safe_int((datestr or "")[:4])


def _empty_rec3():
    return {"kind": None, "id": None, "lang": None, "votes": 0, "rating": 0.0,
            "overview": "", "poster": "", "countries": [], "offers": [],
            "y1": None, "y2": None, "status": ""}


def _tmdb_fetch_one3(tc, cfg):
    """v3 lookup: /find detects movie vs tv (authoritative kind), then ONE details
    call with watch/providers appended. Returns (tconst, rec) or (tconst, None) on error."""
    rec = _empty_rec3()
    try:
        found = tmdb_get(f"/find/{tc}", {"external_source": "imdb_id"}, cfg)
        mres = found.get("movie_results") or []
        tres = found.get("tv_results") or []
        if mres:
            kind, m0 = "m", mres[0]
        elif tres:
            kind, m0 = "s", tres[0]
        else:
            return tc, rec                       # no TMDB match - cache the empty rec
        rec["kind"], rec["id"] = kind, m0.get("id")
        base = "/movie" if kind == "m" else "/tv"
        det = tmdb_get(f"{base}/{rec['id']}", {"append_to_response": "watch/providers"}, cfg)
        rec["lang"] = det.get("original_language")
        rec["votes"] = det.get("vote_count", 0) or 0
        rec["rating"] = det.get("vote_average", 0.0) or 0.0
        rec["overview"] = det.get("overview") or ""
        rec["poster"] = det.get("poster_path") or ""
        if kind == "m":
            rec["countries"] = [c.get("iso_3166_1") for c in det.get("production_countries") or []
                                if c.get("iso_3166_1")]
        else:
            rec["countries"] = [c for c in det.get("origin_country") or [] if c]
            rec["y1"] = year_of(det.get("first_air_date"))
            rec["y2"] = year_of(det.get("last_air_date"))
            rec["status"] = det.get("status") or ""
        inr = ((det.get("watch/providers") or {}).get("results") or {}).get("IN") or {}
        for otype in ("flatrate", "free", "ads", "rent", "buy"):
            for p in inr.get(otype, []):
                rec["offers"].append({"type": otype, "name": p.get("provider_name", "?")})
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return tc, None
    return tc, rec


def enrich_tmdb3(con, cfg, tconsts, workers=8):
    """Look up each IMDb id on TMDB (movies AND tv) -> info{tconst -> rec}. rec carries
    kind/id/lang/votes/rating/overview/poster/countries/IN offers (+ tv air years/status).
    Concurrent (thread pool) for speed; results cached for the day in tmdb_cache3 (old
    cache tables are left untouched). Network happens in worker threads; all SQLite
    writes happen on this (main) thread as results arrive."""
    con.execute("""CREATE TABLE IF NOT EXISTS tmdb_cache3
        (tconst TEXT PRIMARY KEY, info_json TEXT, fetched TEXT)""")
    today = date.today().isoformat()
    info, to_fetch = {}, []
    for tc in tconsts:
        row = con.execute("SELECT info_json FROM tmdb_cache3 WHERE tconst=? AND fetched=?",
                          (tc, today)).fetchone()
        if row:
            info[tc] = json.loads(row[0])
        else:
            to_fetch.append(tc)
    if to_fetch:
        log(f"  enriching {len(to_fetch)} titles via TMDB ({workers} workers) ...")
        done = misses = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for tc, rec in ex.map(lambda t: _tmdb_fetch_one3(t, cfg), to_fetch):
                if rec is None:
                    rec = _empty_rec3()
                    misses += 1
                info[tc] = rec
                con.execute("INSERT OR REPLACE INTO tmdb_cache3 VALUES(?,?,?)",
                            (tc, json.dumps(rec), today))
                done += 1
                if done % 250 == 0:
                    con.commit()
                    log(f"    {done}/{len(to_fetch)} ...")
        con.commit()
        if misses:
            log(f"    ({misses} titles had no TMDB match)")
    return info


def lang_to_bucket(ol):
    """Map a TMDB original_language code to our (bucket, market)."""
    if ol in INDIAN_LANGS:
        return ol, "indian"
    return "intl", "international"


# --- TMDB-native per-language baselines (the category-relative vote gate) ------- #
TMDB_INDIAN_LANGS = ["ml", "ta", "te", "kn", "bn", "mr", "hi", "pa", "gu"]
TMDB_INTL_LANGS = ["en", "ja", "ko"]      # pooled into a single 'intl' baseline
TMDB_VMIN_CLAMP = (10, 5000)              # TMDB vote scale (far lower than IMDb)
TMDB_M_CLAMP = (30, 12000)


def discover_lang_stats(cfg, lang, years, max_pages, vote_floor, kind="m"):
    """Enumerate a language's titles from TMDB /discover/movie or /discover/tv,
    newest 'years' window, sorted by vote_count desc. Returns (votes[], ratings[])."""
    from_date = f"{datetime.now().year - years}-01-01"
    path = "/discover/movie" if kind == "m" else "/discover/tv"
    date_key = "primary_release_date.gte" if kind == "m" else "first_air_date.gte"
    votes, ratings = [], []
    for page in range(1, max_pages + 1):
        try:
            data = tmdb_get(path, {
                "with_original_language": lang, "sort_by": "vote_count.desc",
                date_key: from_date, "vote_count.gte": vote_floor,
                "include_adult": "false", "page": page}, cfg)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            log(f"  discover {lang}/{kind} page {page} failed: {e}")
            break
        results = data.get("results") or []
        for m in results:
            votes.append(m.get("vote_count", 0) or 0)
            ratings.append(m.get("vote_average", 0) or 0)
        if not results or page >= data.get("total_pages", 1):
            break
        time.sleep(0.2)
    return votes, ratings


def tmdb_baseline_from(votes, ratings, cfg):
    vs = sorted(v for v in votes if v > 0)
    if len(vs) < 8:
        return None
    vmin = int(clamp(percentile(vs, 35), *TMDB_VMIN_CLAMP))
    m = int(clamp(percentile(vs, 60), *TMDB_M_CLAMP))
    c_ratings = [r for v, r in zip(votes, ratings) if v >= vmin and r > 0] or [r for r in ratings if r > 0]
    return {"vmin": vmin, "m": m, "c": round(trimmed_mean(c_ratings, 0.05), 3),
            "p90": int(percentile(vs, 90)), "n": len(vs), "votes": vs}


def build_tmdb_baselines(con, cfg, years=10, max_pages=12, force=False):
    """Compute per-(language x kind) baselines on TMDB's own vote scale (authoritative
    language via original_language) from /discover/movie AND /discover/tv. Cached in
    tmdb_baselines2 for the day (the old tmdb_baselines table is left untouched).
    Returns {(bucket, kind) -> baseline}."""
    con.execute("""CREATE TABLE IF NOT EXISTS tmdb_baselines2
        (bucket TEXT, kind TEXT, v_min INT, m INT, c REAL, p90 INT, n INT, built TEXT,
         PRIMARY KEY (bucket, kind))""")
    today = date.today().isoformat()
    if not force:
        cached = con.execute("SELECT bucket, kind, v_min, m, c, p90, n FROM tmdb_baselines2 "
                             "WHERE built=?", (today,)).fetchall()
        if len(cached) >= 8:
            return {(b, k): {"vmin": v, "m": m, "c": c, "p90": p, "n": n}
                    for b, k, v, m, c, p, n in cached}

    log("building TMDB per-(language x kind) baselines via /discover ...")
    baselines = {}
    for kind in ("m", "s"):
        for lang in TMDB_INDIAN_LANGS:
            votes, ratings = discover_lang_stats(cfg, lang, years, max_pages,
                                                 vote_floor=8, kind=kind)
            bl = tmdb_baseline_from(votes, ratings, cfg)
            if bl:
                baselines[(lang, kind)] = bl
        # pooled international baseline (per kind)
        ivotes, iratings = [], []
        for lang in TMDB_INTL_LANGS:
            v, r = discover_lang_stats(cfg, lang, years, max_pages, vote_floor=50, kind=kind)
            ivotes += v
            iratings += r
        bl = tmdb_baseline_from(ivotes, iratings, cfg)
        if bl:
            baselines[("intl", kind)] = bl

    con.execute("DELETE FROM tmdb_baselines2")
    con.executemany("INSERT OR REPLACE INTO tmdb_baselines2 VALUES(?,?,?,?,?,?,?,?)",
                    [(b, k, d["vmin"], d["m"], d["c"], d["p90"], d["n"], today)
                     for (b, k), d in baselines.items()])
    con.commit()

    log("TMDB per-category baselines (last %dy):" % years)
    print(f"    {'stratum':<26}{'titles':>7}{'v_min(P35)':>12}{'m(P60)':>9}{'C':>7}{'P90':>9}")
    order = ["intl"] + TMDB_INDIAN_LANGS
    for kind in ("m", "s"):
        for b in order:
            if (b, kind) in baselines:
                d = baselines[(b, kind)]
                label = f"{LANG_NAMES.get(b, b)} ({KIND_LABEL[kind]})"
                print(f"    {label:<26}{d['n']:>7}{d['vmin']:>12}{d['m']:>9}"
                      f"{d['c']:>7.2f}{d['p90']:>9}")
    return {bk: {k: d[k] for k in ("vmin", "m", "c", "p90", "n")} for bk, d in baselines.items()}


def tmdb_selftest(cfg):
    """Verify a TMDB key works: one lookup of RRR (language + India availability)."""
    if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
        log("No TMDB key found. Copy config.example.json -> config.json and set tmdb_bearer "
            "(v4 Read Access Token) or tmdb_api_key (v3 key). Get one free at "
            "https://www.themoviedb.org/settings/api")
        return
    log("TMDB self-test: looking up RRR (tt8178634) ...")
    try:
        found = tmdb_get("/find/tt8178634", {"external_source": "imdb_id"}, cfg)
        res = found.get("movie_results") or []
        if not res:
            log("  connected, but got no movie_results (unexpected response shape).")
            return
        m0 = res[0]
        log(f"  OK: title='{m0.get('title')}'  original_language={m0.get('original_language')}"
            f"  tmdb_id={m0.get('id')}")
        prov = tmdb_get(f"/movie/{m0['id']}/watch/providers", {}, cfg)
        inr = (prov.get("results") or {}).get("IN") or {}
        names = [p["provider_name"] for k in ("flatrate", "rent", "buy") for p in inr.get(k, [])]
        log(f"  India availability: {', '.join(dict.fromkeys(names)) or '(none listed)'}")
        log("  TMDB key works. Now run:  python movie_finder.py run --open")
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
        log(f"  TMDB self-test FAILED: {e}")
        log("  Check the key value/type in config.json (bearer token vs v3 key).")


# --------------------------------------------------------------------------- #
# 5. digest (select, diff, render)
# --------------------------------------------------------------------------- #
def eff_bucket(r, override):
    ov = override.get(r[0])
    return ov if ov else (r[8], r[9])


def approx_pct(bl, v):
    """Approximate the percentile of vote-count v within a language's baseline,
    interpolating between the stored anchors (P35=vmin, P60=m, P90=p90)."""
    top = max(bl["p90"] * 2, bl["m"] * 3, bl["vmin"] + 2)
    anchors, xs = [(0, 0.0), (bl["vmin"], 35.0), (bl["m"], 60.0), (bl["p90"], 90.0), (top, 100.0)], []
    for vv, pp in anchors:
        if not xs or vv > xs[-1][0]:
            xs.append((vv, pp))
    if v <= xs[0][0]:
        return 0.0
    for i in range(len(xs) - 1):
        (v0, p0), (v1, p1) = xs[i], xs[i + 1]
        if v <= v1:
            return p0 + (p1 - p0) * (v - v0) / (v1 - v0) if v1 > v0 else p1
    return 100.0


def tmdb_gated_candidates(con, cfg):
    """The category-relative engine: take a broad pool of well-rated recent films,
    look up each film's true language + TMDB votes, and gate it against ITS OWN
    language's baseline (Malayalam vs Malayalam, Hollywood vs Hollywood)."""
    cur_year = datetime.now().year
    recent_from = cur_year - cfg["recent_years"] + 1
    baselines = build_tmdb_baselines(con, cfg)
    intl = baselines.get(("intl", "m")) or {"vmin": 200, "m": 800, "c": 7.0, "p90": 10000, "n": 0}
    pool = con.execute(
        """
        SELECT t.tconst, t.primary_title, t.original_title, t.start_year, t.genres,
               t.runtime_min, r.avg_rating, r.num_votes
        FROM titles t JOIN ratings r ON r.tconst = t.tconst
        WHERE t.kind = 'm' AND t.start_year >= ? AND r.avg_rating >= ? AND r.num_votes >= ?
        ORDER BY r.avg_rating DESC, r.num_votes DESC LIMIT ?
        """,
        (recent_from, cfg["rating_threshold"], cfg["pool_floor"], cfg["pool_cap"]),
    ).fetchall()
    log(f"TMDB per-language gate: enriching {len(pool)} candidate titles ...")
    info = enrich_tmdb3(con, cfg, [p[0] for p in pool], workers=cfg["browse_workers"])

    thr = cfg["rating_threshold"]
    rows, providers = [], {}
    for p in pool:
        tc = p[0]
        rec = info.get(tc) or {}
        lang = rec.get("lang")
        tv = rec.get("votes", 0) or 0
        tr = rec.get("rating", 0.0) or 0.0
        if not lang or tv <= 0:
            continue
        bucket, market = lang_to_bucket(lang)
        bl = baselines.get((bucket, "m"), intl)
        m, C, vmin = bl["m"], bl["c"], bl["vmin"]
        wr = (tv / (tv + m)) * tr + (m / (tv + m)) * C
        if tv < vmin or wr < thr:            # Gate 1 (category vote floor) + Gate 2 (damped)
            continue
        pct = approx_pct(bl, tv)
        conf = "confirmed" if (tv >= m and p[3] and p[3] <= cur_year - 1) else "provisional"
        providers[tc] = rec.get("offers", [])
        rows.append((tc, p[1], p[2], p[3], p[4], p[5], p[6], p[7],
                     bucket, market, round(wr, 2), conf, round(pct, 1)))
    rows.sort(key=lambda r: -r[10])
    meta = {"tmdb": True, "gate": "per-language", "verified": len(rows),
            "recent_from": recent_from, "cur_year": cur_year, "pool": len(pool)}
    return rows, providers, meta


def imdb_display_candidates(con, cfg):
    """No-key fallback: the coarse IMDb global gate (language buckets are approximate)."""
    cur_year = datetime.now().year
    recent_from = cur_year - cfg["recent_years"] + 1
    rows = con.execute(
        """
        SELECT s.tconst, t.primary_title, t.original_title, t.start_year, t.genres,
               t.runtime_min, r.avg_rating, r.num_votes, s.bucket, s.market,
               s.wr, s.confidence, s.vote_percentile
        FROM scores s
        JOIN titles t ON t.tconst = s.tconst
        JOIN ratings r ON r.tconst = s.tconst
        WHERE s.eligible = 1 AND s.kind = 'm' AND t.start_year >= ?
        ORDER BY s.wr DESC
        """,
        (recent_from,),
    ).fetchall()
    meta = {"tmdb": False, "gate": "global", "verified": 0,
            "recent_from": recent_from, "cur_year": cur_year, "pool": len(rows)}
    return rows, {}, meta


def build_digest(con, cfg, use_tmdb=True):
    have_key = bool(cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key"))
    if use_tmdb and have_key:
        rows, providers, meta_gate = tmdb_gated_candidates(con, cfg)
    else:
        rows, providers, meta_gate = imdb_display_candidates(con, cfg)
    override = {}  # buckets are already correct on each row

    seen = set(r[0] for r in con.execute("SELECT tconst FROM digest_history"))
    new_ids = set(r[0] for r in rows if r[0] not in seen)
    today = date.today().isoformat()
    con.executemany("INSERT OR IGNORE INTO digest_history VALUES(?,?)",
                    [(r[0], today) for r in rows])
    con.commit()

    meta = {"date": today, "total": len(rows), "new": len(new_ids),
            "first_run": len(seen) == 0, **meta_gate}
    os.makedirs(OUT_DIR, exist_ok=True)
    html_path = os.path.join(OUT_DIR, f"digest-{today}.html")
    md_path = os.path.join(OUT_DIR, f"digest-{today}.md")
    html_doc = render_html(rows, new_ids, providers, override, cfg, meta)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(rows, new_ids, providers, override, cfg, meta))
    with open(os.path.join(OUT_DIR, "digest-latest.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)

    print_console(rows, new_ids, providers, override, cfg, meta)
    log(f"digest written: {html_path}")
    log(f"           and: {md_path}")
    return html_path, rows, new_ids, providers, override, meta


def group_rows(rows, override, cfg):
    indian = defaultdict(list)
    intl = []
    for r in rows:
        bucket, market = eff_bucket(r, override)
        if market == "indian":
            indian[bucket].append(r)
        else:
            intl.append(r)
    lang_order = sorted(indian.keys(), key=lambda b: -len(indian[b]))
    indian_capped = [(b, indian[b][:cfg["per_lang_cap"]], len(indian[b])) for b in lang_order]
    return indian_capped, intl[:cfg["intl_cap"]], len(intl)


def significance_line(r, override):
    nv = r[7]
    vp = r[12]
    bucket, _ = eff_bucket(r, override)
    name = LANG_NAMES.get(bucket, bucket)
    if vp >= 50 and bucket not in ("intl",) or (vp >= 50):
        return f"more voted than {vp:.0f}% of {name} films (last 10y)"
    return f"{fmt_votes(nv)} votes"


def providers_text(tc, providers, meta):
    if not meta["tmdb"]:
        return "add a TMDB key for India availability"
    offs = providers.get(tc)
    if offs is None:
        return "availability lookup unavailable"
    if not offs:
        return "not on tracked Indian OTTs (theatre / rent only)"
    stream = [o["name"] for o in offs if o["type"] in ("flatrate", "free", "ads")]
    paid = [o["name"] for o in offs if o["type"] in ("rent", "buy")]
    if stream:
        return "Stream: " + ", ".join(dict.fromkeys(stream))
    if paid:
        return "Rent/Buy: " + ", ".join(dict.fromkeys(paid))
    return "not on tracked Indian OTTs"


def print_console(rows, new_ids, providers, override, cfg, meta):
    print()
    print("=" * 74)
    print(f"  MOVIE FINDER - weekly digest  {meta['date']}")
    print(f"  {meta['total']} genuinely good releases ({meta['recent_from']}-{meta['cur_year']})"
          f"  |  {meta['new']} new since last run")
    if not meta["tmdb"]:
        print("  (language tags are COARSE - add a free TMDB key for accurate per-language)")
    print("=" * 74)
    indian_capped, intl, intl_total = group_rows(rows, override, cfg)

    def line(r):
        tag = "NEW " if r[0] in new_ids else "    "
        title = (r[1] or r[2])[:40]
        return (f"  {tag}{r[10]:>4.1f}wr {r[6]:>3.1f}/10 {fmt_votes(r[7]):>9}v  "
                f"{title:<42} {providers_text(r[0], providers, meta)}")
    for bucket, items, total in indian_capped:
        print(f"\n  -- {LANG_NAMES.get(bucket, bucket)} ({total}) " + "-" * 4)
        for r in items[:12]:
            print(line(r))
    if intl:
        print(f"\n  -- International ({intl_total}) " + "-" * 4)
        for r in intl[:12]:
            print(line(r))
    print()


def render_md(rows, new_ids, providers, override, cfg, meta):
    out = [f"# Movie Finder - weekly digest ({meta['date']})\n",
           f"**{meta['total']} genuinely good releases** from {meta['recent_from']}-{meta['cur_year']}, "
           f"{meta['new']} new since last run. Judged {cfg['rating_threshold']}+ weighted rating "
           f"against per-category vote baselines.\n"]
    if not meta["tmdb"]:
        out.append("> Language tags are coarse (IMDb data limitation). Add a free TMDB key for "
                   "accurate per-language classification + India OTT availability.\n")
    indian_capped, intl, intl_total = group_rows(rows, override, cfg)

    def row_md(r):
        title = r[1] or r[2]
        star = " **NEW**" if r[0] in new_ids else ""
        return (f"- **{title}** ({r[3]}){star} - score {r[10]:.1f} · "
                f"IMDb {r[6]:.1f}/10 ({fmt_votes(r[7])} votes) · {significance_line(r, override)} · "
                f"{providers_text(r[0], providers, meta)} · "
                f"[IMDb](https://www.imdb.com/title/{r[0]}/)")

    if indian_capped:
        out.append("## Indian cinema\n")
        for bucket, items, total in indian_capped:
            out.append(f"### {LANG_NAMES.get(bucket, bucket)} ({total})\n")
            out.extend(row_md(r) for r in items)
            out.append("")
    if intl:
        out.append(f"## International ({intl_total})\n")
        out.extend(row_md(r) for r in intl)
        out.append("")
    out.append("\n---\n*Information courtesy of IMDb (https://www.imdb.com). Used with permission. "
               "Availability via TMDB/JustWatch when enabled.*\n")
    return "\n".join(out)


def render_html(rows, new_ids, providers, override, cfg, meta):
    esc = html_lib.escape
    indian_capped, intl, intl_total = group_rows(rows, override, cfg)

    def card(r):
        tc = r[0]
        title = esc(r[1] or r[2] or tc)
        orig = r[2] or ""
        subtitle = f" <span class='orig'>{esc(orig)}</span>" if orig and orig != r[1] else ""
        genres = esc((r[4] or "").replace(",", " · "))
        newbadge = "<span class='badge new'>NEW</span>" if tc in new_ids else ""
        confbadge = "" if r[11] == "confirmed" else "<span class='badge prov'>rating settling</span>"
        prov = esc(providers_text(tc, providers, meta))
        prov_cls = "prov-stream" if prov.startswith("Stream") else "prov-other"
        return f"""
        <div class="card">
          <div class="score" title="weighted rating vs its category">{r[10]:.1f}</div>
          <div class="body">
            <div class="titleline"><a href="https://www.imdb.com/title/{tc}/" target="_blank" rel="noopener">{title}</a>
              <span class="yr">{r[3] or ''}</span>{subtitle} {newbadge}{confbadge}</div>
            <div class="meta">IMDb {r[6]:.1f}/10 · {fmt_votes(r[7])} votes · {esc(significance_line(r, override))}</div>
            <div class="meta genres">{genres}</div>
            <div class="prov {prov_cls}">{prov}</div>
          </div>
        </div>"""

    sections = []
    if indian_capped:
        blocks = []
        for bucket, items, total in indian_capped:
            more = f" <span class='more'>+{total - len(items)} more</span>" if total > len(items) else ""
            blocks.append(f"<h3>{esc(LANG_NAMES.get(bucket, bucket))} <span class='count'>{total}</span>{more}</h3>"
                          + "".join(card(r) for r in items))
        sections.append("<section><div class='sec-label'>Indian cinema</div>" + "".join(blocks) + "</section>")
    if intl:
        more = f" <span class='more'>+{intl_total - len(intl)} more</span>" if intl_total > len(intl) else ""
        sections.append("<section><div class='sec-label'>International</div>"
                        f"<h3>World cinema <span class='count'>{intl_total}</span>{more}</h3>"
                        + "".join(card(r) for r in intl) + "</section>")

    firstrun = ("<div class='callout'>First run - everything is tagged NEW because there's no prior "
                "digest to compare against. Next week, only genuinely new arrivals get the NEW tag.</div>"
                if meta["first_run"] else "")
    if meta["tmdb"]:
        tmdb_note = ("<div class='callout'>Category-relative gate: every film is judged against "
                     "<b>its own language's</b> vote baseline (Malayalam vs Malayalam, Hollywood vs "
                     "Hollywood) using TMDB's true original-language, and mapped to where it streams in "
                     f"India. {meta['verified']} titles cleared their category bar.</div>")
    else:
        tmdb_note = ("<div class='callout warn'>Language tags are <b>coarse</b> - IMDb's free data can't "
                     "reliably separate Indian languages (dub tracks pollute it). Add a free TMDB key to "
                     "<code>config.json</code> for accurate per-language tagging <b>and</b> India OTT availability.</div>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Movie Finder - {meta['date']}</title>
<style>
:root {{ --bg:#f7f7f9; --card:#fff; --ink:#1b1d22; --muted:#5c616c; --faint:#8a8f9a;
  --line:#e3e4ea; --accent:#a3172e; --accent-soft:#fbeef0; --good:#1a7f4e; --warn:#9a6700; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#131418; --card:#1c1e24; --ink:#e8e8ec; --muted:#a6aab4; --faint:#7d818c;
  --line:#2d3038; --accent:#e56a7c; --accent-soft:#33202a; --good:#4cc38a; --warn:#e2b93b; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif; }}
.wrap {{ max-width:820px; margin:0 auto; padding:40px 20px 80px; }}
.eyebrow {{ font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:var(--accent); font-weight:600; margin:0 0 8px; }}
h1 {{ font-family:Georgia,serif; font-size:34px; margin:0 0 6px; }}
.sub {{ color:var(--muted); margin:0 0 16px; }}
.sub b {{ color:var(--ink); }}
.sec-label {{ font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); font-weight:600; margin:30px 0 4px; }}
h3 {{ font-family:Georgia,serif; font-size:21px; margin:18px 0 10px; border-bottom:1px solid var(--line); padding-bottom:6px; }}
h3 .count {{ font-family:"Segoe UI",sans-serif; font-size:13px; color:var(--faint); font-weight:400; }}
h3 .more {{ font-family:"Segoe UI",sans-serif; font-size:12px; color:var(--faint); font-weight:400; }}
.card {{ display:flex; gap:14px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:12px 14px; margin:0 0 8px; }}
.score {{ font-family:Georgia,serif; font-size:24px; font-weight:700; color:var(--accent);
  min-width:44px; text-align:center; font-variant-numeric:tabular-nums; padding-top:2px; }}
.body {{ flex:1; min-width:0; }}
.titleline {{ font-weight:650; font-size:16px; }}
.titleline a {{ color:var(--ink); text-decoration:none; }}
.titleline a:hover {{ color:var(--accent); }}
.yr {{ color:var(--faint); font-weight:400; font-size:14px; }}
.orig {{ color:var(--faint); font-weight:400; font-size:13px; font-style:italic; }}
.meta {{ font-size:13.5px; color:var(--muted); margin-top:2px; }}
.genres {{ color:var(--faint); font-size:12.5px; }}
.prov {{ font-size:13px; margin-top:4px; font-weight:600; }}
.prov-stream {{ color:var(--good); }}
.prov-other {{ color:var(--faint); font-weight:400; }}
.badge {{ display:inline-block; font-size:10.5px; font-weight:700; letter-spacing:.04em; border-radius:4px; padding:1px 6px; vertical-align:middle; margin-left:4px; }}
.badge.new {{ background:var(--accent); color:#fff; }}
.badge.prov {{ background:var(--accent-soft); color:var(--warn); font-weight:600; }}
.callout {{ background:var(--accent-soft); border-left:3px solid var(--accent); padding:12px 16px; border-radius:0 8px 8px 0; margin:14px 0; font-size:14px; }}
.callout.warn {{ border-color:var(--warn); }}
code {{ background:var(--line); padding:1px 5px; border-radius:4px; font-size:.9em; }}
footer {{ margin-top:44px; padding-top:16px; border-top:1px solid var(--line); font-size:12px; color:var(--faint); }}
</style></head><body><div class="wrap">
<p class="eyebrow">Weekly digest · {meta['date']}</p>
<h1>Movie Finder</h1>
<p class="sub"><b>{meta['total']}</b> genuinely good releases from {meta['recent_from']}-{meta['cur_year']} ·
<b>{meta['new']}</b> new since last run · judged {cfg['rating_threshold']}+ weighted rating against per-category vote baselines</p>
{firstrun}{tmdb_note}
{''.join(sections)}
<footer>Information courtesy of IMDb (https://www.imdb.com). Used with permission.
Availability via TMDB / JustWatch when enabled. Generated by Movie Finder Phase 0.</footer>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# 6. telegram
# --------------------------------------------------------------------------- #
def telegram_text(rows, new_ids, providers, meta, limit=15):
    lines = [f"*Movie Finder* - {meta['date']}",
             f"{meta['total']} good releases, {meta['new']} new\n"]
    for r in rows[:limit]:
        tag = "NEW " if r[0] in new_ids else ""
        lines.append(f"{tag}*{r[1] or r[2]}* ({r[3]}) - {r[10]:.1f} - "
                     f"{providers_text(r[0], providers, meta)}")
    return "\n".join(lines)


def send_telegram(cfg, text):
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not (token and chat):
        log("telegram not configured (set telegram_bot_token + telegram_chat_id in config.json)")
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "Markdown",
        "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
        log("telegram digest sent" if ok else "telegram send returned non-200")
        return ok
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log(f"telegram send failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# 7. review sentiment (TMDB reviews + a built-in analyzer) = the "real verdict"
# --------------------------------------------------------------------------- #
_TOK = re.compile(r"[a-z][a-z']+")
SENT_POS = {
    "brilliant", "excellent", "amazing", "masterpiece", "superb", "outstanding", "beautiful",
    "gripping", "engaging", "powerful", "stellar", "fantastic", "wonderful", "great", "good",
    "terrific", "impressive", "compelling", "moving", "heartfelt", "hilarious", "entertaining",
    "enjoyable", "solid", "strong", "remarkable", "memorable", "perfect", "loved", "love",
    "best", "gem", "refreshing", "flawless", "riveting", "spectacular", "breathtaking",
    "delightful", "praise", "praised", "worth", "nuanced", "clever", "thrilling", "fun",
    "epic", "classic", "phenomenal", "captivating", "poignant", "satisfying", "beautifully",
    "recommend", "recommended", "genius", "sublime", "wholesome", "delivers", "winner"}
SENT_NEG = {
    "terrible", "awful", "boring", "bad", "worst", "poor", "disappointing", "disappointment",
    "weak", "bland", "dull", "mess", "messy", "mediocre", "forgettable", "predictable",
    "cliche", "cliched", "overrated", "waste", "lazy", "flat", "slow", "dragging", "drag",
    "pointless", "ridiculous", "nonsense", "cringe", "worse", "lacks", "lacking", "fails",
    "failed", "fail", "uninspired", "tedious", "contrived", "shallow", "wooden", "annoying",
    "confusing", "disaster", "horrible", "unwatchable", "painful", "sloppy", "stupid",
    "avoid", "skip", "hollow", "underwhelming", "disappointed", "loud", "dragged", "lame"}
SENT_NEGATORS = {"not", "no", "never", "hardly", "barely", "without", "isnt", "wasnt",
                 "dont", "doesnt", "didnt", "cant", "wont", "nothing", "lacks", "lacking"}
SENT_ASPECTS = {
    "story": {"story", "plot", "screenplay", "script", "writing", "narrative", "storyline", "tale"},
    "acting": {"acting", "performance", "performances", "cast", "actor", "actress", "role", "portrayal"},
    "direction": {"direction", "director", "directed", "filmmaking", "vision", "execution", "making"},
    "pacing": {"pacing", "pace", "editing", "length", "runtime", "lengthy"},
    "music": {"music", "songs", "song", "soundtrack", "score", "bgm", "musical"},
    "visuals": {"visuals", "cinematography", "visual", "vfx", "cgi", "camera", "frames", "shots"},
    "ending": {"climax", "ending", "finale", "twist", "conclusion", "interval", "predictable"},
}
ASPECT_LABEL = {"story": "the story", "acting": "the acting", "direction": "the direction",
                "pacing": "the pacing", "music": "the music", "visuals": "the visuals",
                "ending": "the ending"}


def _toks(text):
    return _TOK.findall((text or "").lower())


def _score_window(toks, lo, hi):
    p = n = 0
    for j in range(lo, hi):
        w = toks[j]
        s = 1 if w in SENT_POS else (-1 if w in SENT_NEG else 0)
        if s:
            if any(toks[k] in SENT_NEGATORS for k in range(max(0, j - 2), j)):
                s = -s
            p, n = (p + 1, n) if s > 0 else (p, n + 1)
    return p, n


def _polarity(toks):
    p, n = _score_window(toks, 0, len(toks))
    return (p - n) / (p + n) if (p + n) else 0.0


def _aspect_polarities(toks):
    out = {}
    for asp, kws in SENT_ASPECTS.items():
        p = n = 0
        for i, w in enumerate(toks):
            if w in kws:
                dp, dn = _score_window(toks, max(0, i - 6), min(len(toks), i + 7))
                p, n = p + dp, n + dn
        if p + n:
            out[asp] = (p - n) / (p + n)
    return out


def _verdict_text(aspects):
    praised = sorted([(a, v) for a, v in aspects.items() if v > 0.2], key=lambda x: -x[1])
    panned = sorted([(a, v) for a, v in aspects.items() if v < -0.2], key=lambda x: x[1])
    parts = []
    if praised:
        parts.append("praise for " + ", ".join(ASPECT_LABEL[a] for a, _ in praised[:2]))
    if panned:
        parts.append("gripes about " + ", ".join(ASPECT_LABEL[a] for a, _ in panned[:2]))
    return "; ".join(parts) if parts else "mixed reactions"


def aggregate_sentiment(reviews, imdb_rating):
    text_scores, ratings, asp = [], [], defaultdict(list)
    for r in reviews:
        t = _toks(r.get("content"))
        if len(t) >= 5:
            text_scores.append(5 + 5 * _polarity(t))
            for a, v in _aspect_polarities(t).items():
                asp[a].append(v)
        rt = r.get("rating")
        if isinstance(rt, (int, float)) and rt > 0:
            ratings.append(float(rt))
    if not text_scores and not ratings:
        return None
    aspects = {a: round(sum(v) / len(v), 2) for a, v in asp.items() if len(v) >= 2}
    verdict = _verdict_text(aspects)
    # Numeric verdict: lean on reviewers' ACTUAL 0-10 ratings (calibrated); the lexicon
    # text score is only a small tone nudge (it systematically under-scores acclaimed films).
    if len(ratings) >= 2:
        base = sum(ratings) / len(ratings)
        if text_scores:
            base += max(-0.4, min(0.4, (sum(text_scores) / len(text_scores) - 6.5) * 0.15))
        nr = len(ratings)
        ts = (base * nr + 6.8 * 1.5) / (nr + 1.5)          # gentle shrink toward 6.8
        conf = "high" if nr >= 6 else ("medium" if nr >= 3 else "low")
    elif text_scores:
        ts = sum(text_scores) / len(text_scores)
        conf = "low"
    elif ratings:                          # a single star-rating, no usable review text
        ts = (ratings[0] + 6.8 * 1.5) / (1 + 1.5)
        conf = "low"
    else:
        return None
    return {"n": len(reviews), "ts": round(ts, 1), "aspects": aspects, "verdict": verdict,
            "div": round(ts - imdb_rating, 1) if imdb_rating else 0.0, "conf": conf}


def _fetch_reviews_one(tc, kind, tmdb_id, cfg, max_reviews):
    """Fetch TMDB user reviews for one title. kind/tmdb_id come from the enrichment
    cache when available (routes /movie vs /tv without a /find round-trip)."""
    try:
        if not tmdb_id:
            found = tmdb_get(f"/find/{tc}", {"external_source": "imdb_id"}, cfg)
            mres = found.get("movie_results") or []
            tres = found.get("tv_results") or []
            if mres:
                kind, tmdb_id = "m", mres[0]["id"]
            elif tres:
                kind, tmdb_id = "s", tres[0]["id"]
            else:
                return tc, []
        base = "/tv" if kind == "s" else "/movie"
        reviews = []
        for page in (1, 2):
            data = tmdb_get(f"{base}/{tmdb_id}/reviews", {"page": page}, cfg)
            for r in data.get("results", []):
                reviews.append({"content": r.get("content", ""),
                                "rating": (r.get("author_details") or {}).get("rating")})
            if page >= data.get("total_pages", 1) or len(reviews) >= max_reviews:
                break
        return tc, reviews[:max_reviews]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return tc, None


def build_sentiment(con, cfg):
    """Fetch TMDB user reviews for the most-voted catalog titles (movies + shows) and
    derive a 'viewer verdict' (True Sentiment + aspect breakdown + divergence)."""
    if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
        log("sentiment needs a TMDB key. Add one to config.json.")
        return 0, 0
    con.execute("""CREATE TABLE IF NOT EXISTS sentiment
        (tconst TEXT PRIMARY KEY, n INT, ts REAL, div REAL, conf TEXT, verdict TEXT, computed TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS tmdb_cache3
        (tconst TEXT PRIMARY KEY, info_json TEXT, fetched TEXT)""")
    today = date.today().isoformat()
    rows = sorted(catalog_rows(con, cfg), key=lambda r: -r[8])[:cfg["sentiment_cap"]]
    imdb_r = {r[0]: r[7] for r in rows}
    done = set(r[0] for r in con.execute("SELECT tconst FROM sentiment WHERE computed=?", (today,)))
    todo = [r[0] for r in rows if r[0] not in done]
    refs = {}  # tconst -> (kind, tmdb_id) from the enrichment cache (any day is fine)
    for tc in todo:
        row = con.execute("SELECT info_json FROM tmdb_cache3 WHERE tconst=?", (tc,)).fetchone()
        if row:
            rec = json.loads(row[0])
            if rec.get("id"):
                refs[tc] = (rec.get("kind"), rec.get("id"))
    log(f"sentiment: analyzing TMDB reviews for {len(todo)} titles ({cfg['browse_workers']} workers) ...")
    got = 0
    with ThreadPoolExecutor(max_workers=cfg["browse_workers"]) as ex:
        for i, (tc, reviews) in enumerate(
                ex.map(lambda t: _fetch_reviews_one(
                    t, refs.get(t, (None, None))[0], refs.get(t, (None, None))[1],
                    cfg, cfg["sentiment_reviews"]), todo)):
            agg = aggregate_sentiment(reviews or [], imdb_r.get(tc)) if reviews else None
            if agg:
                con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,?,?,?,?,?,?)",
                            (tc, agg["n"], agg["ts"], agg["div"], agg["conf"], agg["verdict"], today))
                got += 1
            else:
                con.execute("INSERT OR REPLACE INTO sentiment VALUES(?,0,NULL,NULL,NULL,NULL,?)",
                            (tc, today))
            if (i + 1) % 250 == 0:
                con.commit()
                log(f"    {i + 1}/{len(todo)} ({got} with reviews) ...")
    con.commit()
    total = con.execute("SELECT count(*) FROM sentiment WHERE n>0").fetchone()[0]
    log(f"sentiment: {got} of {len(todo)} newly analyzed had TMDB reviews; {total} titles have a verdict total")
    return got, len(todo)


# --------------------------------------------------------------------------- #
# 8. app catalog (movies + shows) -> out/data.js
# --------------------------------------------------------------------------- #
def catalog_rows(con, cfg):
    """Candidate rows for the app catalog: quality-gated movies + well-voted shows.
    Row: (tconst, primary_title, original_title, kind, start_year, end_year,
          genres, avg_rating, num_votes)."""
    cols = ("SELECT t.tconst, t.primary_title, t.original_title, t.kind, t.start_year, "
            "t.end_year, t.genres, r.avg_rating, r.num_votes "
            "FROM titles t JOIN ratings r ON r.tconst = t.tconst ")
    # Both kinds use the SAME gate and the same ordering. Shows used to order by num_votes,
    # so the two halves of the catalog had different effective floors (movies ~6.6 by rating
    # rank, shows 5.5 by votes). Rating-first also means that if a cap ever does bind, it
    # drops the weakest titles rather than the least popular ones.
    order = "ORDER BY r.avg_rating DESC, r.num_votes DESC LIMIT ?"
    movies = con.execute(
        cols + "WHERE t.kind='m' AND r.avg_rating >= ? AND r.num_votes >= ? " + order,
        (cfg["rating_threshold"], cfg["browse_vote_floor"], cfg["browse_cap"])).fetchall()
    shows = con.execute(
        cols + "WHERE t.kind='s' AND r.avg_rating >= ? AND r.num_votes >= ? " + order,
        (cfg["rating_threshold"], cfg["shows_vote_floor"], cfg["shows_cap"])).fetchall()
    for label, sel, cap in (("movies", movies, cfg["browse_cap"]), ("shows", shows, cfg["shows_cap"])):
        if len(sel) >= cap:
            log(f"WARNING: {label} hit the {cap} cap — rating_threshold is no longer the real "
                f"floor (lowest selected: {sel[-1][7]}). Raise the cap or the vote floor.")
    return movies + shows


def snippet(text, limit=260):
    """Collapse whitespace and truncate to <= limit chars, ending on a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def country_display(codes, limit=3):
    """ISO country codes -> up to `limit` unique display names (raw code fallback)."""
    out = []
    for code in codes:
        name = COUNTRY_NAMES.get(code, code)
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


CATALOG_FALLBACK_BL = {"m": {"vmin": 200, "m": 800, "c": 7.0, "p90": 10000, "n": 0},
                       "s": {"vmin": 50, "m": 300, "c": 7.0, "p90": 3000, "n": 0}}


def build_catalog(con, cfg):
    """Build the combined movies+shows catalog for the app: select, TMDB-enrich,
    compute per-(language x kind) vote percentiles + first_surfaced. Returns the
    list of MF_DATA title dicts (contract shape; 'se' is attached separately)."""
    if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
        log("catalog needs a TMDB key (language, storyline, availability). Add one to config.json.")
        return []
    con.execute("CREATE TABLE IF NOT EXISTS digest_history "
                "(tconst TEXT PRIMARY KEY, first_surfaced TEXT)")
    rows = catalog_rows(con, cfg)
    n_movies = sum(1 for r in rows if r[3] == "m")
    log(f"catalog: {n_movies} movie + {len(rows) - n_movies} show candidates; resolving TMDB details ...")
    info = enrich_tmdb3(con, cfg, [r[0] for r in rows], workers=cfg["browse_workers"])
    baselines = build_tmdb_baselines(con, cfg)

    films = []
    for tc, pt, ot, k_imdb, sy, ey, genres_s, ar, nv in rows:
        rec = info.get(tc)
        if not rec or not rec.get("lang"):
            continue
        kind = rec.get("kind") or k_imdb        # TMDB's movie/tv split is authoritative
        bucket, market = lang_to_bucket(rec["lang"])
        bl = (baselines.get((bucket, kind)) or baselines.get(("intl", kind))
              or CATALOG_FALLBACK_BL[kind])
        tv_votes = rec.get("votes", 0) or 0
        pct = round(approx_pct(bl, tv_votes)) if tv_votes > 0 else 0
        offers = rec.get("offers") or []
        film = {"t": tc, "k": kind, "n": pt or ot or tc,
                "y": sy or rec.get("y1") or 0}
        if not film["y"]:
            continue    # unknown-year titles break year filters/sorts; skip the junk
        if kind == "s":
            y2 = ey                              # IMDb end year; None/null = ongoing
            if y2 is None and rec.get("status") in ("Ended", "Canceled"):
                y2 = rec.get("y2")               # TMDB last-air year when IMDb lags
            film["y2"] = y2
        film.update({
            "l": bucket, "ln": LANG_NAMES.get(bucket, bucket), "mk": market,
            "r": round(ar, 1), "v": nv, "p": pct,
            "g": [g for g in (genres_s or "").split(",") if g]})
        if rec.get("rating"):
            film["tr"] = round(rec["rating"], 1)     # TMDB rating (vote_average)
        snip = snippet(rec.get("overview"))
        if snip:
            film["o"] = snip
        countries = country_display(rec.get("countries") or [])
        if countries:
            film["c"] = countries
        film["img"] = rec.get("poster") or ""
        film["s"] = sorted({o["name"] for o in offers if o["type"] in ("flatrate", "free", "ads")})
        film["b"] = sorted({o["name"] for o in offers if o["type"] in ("rent", "buy")})
        films.append(film)
    films.sort(key=lambda f: (-f["r"], -f["v"]))

    # first_surfaced: reuse digest_history semantics for ALL emitted titles -> fs/nw.
    # CI rebuilds the DB from scratch each run, so first seed history from the PREVIOUS
    # deploy's data.js (the workflow curls it to prev-data.js) - else every title would
    # be "NEW" every week.
    prev_path = os.path.join(HERE, "prev-data.js")
    if os.path.exists(prev_path):
        try:
            raw = open(prev_path, encoding="utf-8").read()
            payload = raw[raw.index("{"):raw.rindex("}") + 1]
            prev = json.loads(payload.replace("<\\/", "</"))
            seed = [(t["t"], t["fs"]) for t in prev.get("titles", []) if t.get("fs")]
            con.executemany("INSERT OR IGNORE INTO digest_history VALUES(?,?)", seed)
            log(f"seeded first_surfaced for {len(seed)} titles from previous deploy")
        except (ValueError, KeyError, OSError) as e:
            log(f"warning: could not parse prev-data.js ({e}); NEW badges may over-fire")
    today = date.today().isoformat()
    con.executemany("INSERT OR IGNORE INTO digest_history VALUES(?,?)",
                    [(f["t"], today) for f in films])
    con.commit()
    fs_map = dict(con.execute("SELECT tconst, first_surfaced FROM digest_history"))
    for f in films:
        fs = fs_map.get(f["t"]) or today
        f["fs"] = fs
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(fs)).days
        except ValueError:
            age = -1
        if 0 <= age <= 8:
            f["nw"] = 1
    n_shows = sum(1 for f in films if f["k"] == "s")
    log(f"catalog: {len(films) - n_shows} movies + {n_shows} shows made the cut")
    return films


def attach_sentiment(con, films):
    sent = {}
    try:
        for tc, ts, dv, conf, verdict in con.execute(
                "SELECT tconst, ts, div, conf, verdict FROM sentiment WHERE n>0"):
            sent[tc] = {"ts": ts, "div": dv, "v": verdict, "c": conf}
    except sqlite3.OperationalError:
        return  # sentiment engine not run yet
    for f in films:
        if f["t"] in sent:
            f["se"] = sent[f["t"]]


def write_data_js(films, cfg):
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"built": date.today().isoformat(),
               "threshold": cfg["rating_threshold"], "titles": films}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    path = os.path.join(OUT_DIR, "data.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.MF_DATA=" + data + ";\n")
    log(f"data payload written: {path} ({len(films)} titles)")


# --------------------------------------------------------------------------- #
# 9. app shell (templates/app.html -> out/index.html) + full site build
# --------------------------------------------------------------------------- #
REDIRECT_STUB = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=index.html">
<script>location.replace("index.html");</script>
<title>Movie Finder</title></head>
<body><p>Moved &mdash; <a href="index.html">continue to Movie Finder</a>.</p></body></html>
'''

PLACEHOLDER_TEMPLATE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Movie Finder</title></head>
<body style="font-family:'Segoe UI',system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px">
<h1>Movie Finder</h1>
<p>Built __BUILT__. The app template (<code>templates/app.html</code>) was missing from
this build, so this placeholder page was published instead. The catalog data itself is in
<a href="data.js">data.js</a>. Restore <code>templates/app.html</code> and re-run
<code>python movie_finder.py site</code>.</p>
</body></html>
'''


def build_app_shell(cfg):
    """out/index.html from templates/app.html (placeholder if missing, so the build
    never hard-fails) + redirect stubs for the retired v1 pages."""
    os.makedirs(OUT_DIR, exist_ok=True)
    tpl_path = os.path.join(HERE, "templates", "app.html")
    if os.path.exists(tpl_path):
        with open(tpl_path, "r", encoding="utf-8") as f:
            tpl = f.read()
    else:
        log("warning: templates/app.html is missing - publishing a placeholder index.html")
        tpl = PLACEHOLDER_TEMPLATE
    html_doc = (tpl.replace("__BUILT__", date.today().isoformat())
                   .replace("__THRESH__", str(cfg["rating_threshold"]))
                   .replace("__SUPA_URL__", cfg.get("supabase_url", ""))
                   .replace("__SUPA_KEY__", cfg.get("supabase_anon_key", "")))
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)
    for stub in ("browse.html", "digest-latest.html"):   # old family bookmarks
        with open(os.path.join(OUT_DIR, stub), "w", encoding="utf-8") as f:
            f.write(REDIRECT_STUB)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()   # serve files as-is on Pages
    log(f"app shell written: {os.path.join(OUT_DIR, 'index.html')} (+ redirect stubs)")


def build_app(con, cfg, with_sentiment=False):
    """Catalog -> data.js, then index.html + redirect stubs."""
    films = build_catalog(con, cfg)
    if with_sentiment and films:
        build_sentiment(con, cfg)               # after enrichment so tmdb_cache3 routes m/s
    attach_sentiment(con, films)
    write_data_js(films, cfg)
    build_app_shell(cfg)
    return films


def build_site(con, cfg, use_tmdb=True):
    """Full weekly build for publishing: digest (email/Telegram path) + the app.
    The digest HTML pages still get written, but out/digest-latest.html and
    out/browse.html end up as redirect stubs to index.html (the app IS the site)."""
    build_digest(con, cfg, use_tmdb=use_tmdb)   # dated digest files + Telegram text source
    build_app(con, cfg, with_sentiment=True)    # data.js + index.html + redirect stubs


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    try:  # keep the Windows console from crashing on non-ASCII titles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Movie Finder - weekly digest + static discovery app")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "fetch", "build", "digest", "send", "all",
                             "tmdb-selftest", "tmdb-baselines", "browse", "sentiment",
                             "landing", "site"])
    ap.add_argument("--refresh", action="store_true", help="force re-download of IMDb datasets")
    ap.add_argument("--years", type=int, help="cover releases from the last N years (default 2)")
    ap.add_argument("--threshold", type=float, help="weighted-rating gate (default 6.5)")
    ap.add_argument("--no-tmdb", action="store_true", help="skip TMDB enrichment")
    ap.add_argument("--open", action="store_true", help="open the HTML digest when done")
    args = ap.parse_args()

    cfg = load_config()
    if args.years:
        cfg["recent_years"] = args.years
    if args.threshold:
        cfg["rating_threshold"] = args.threshold
    use_tmdb = not args.no_tmdb

    cmd = args.command
    if cmd == "tmdb-selftest":
        tmdb_selftest(cfg)
        return
    if cmd == "tmdb-baselines":
        if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
            log("No TMDB key in config.json.")
            return
        con = connect()
        build_tmdb_baselines(con, cfg, force=True)
        con.close()
        return
    if cmd == "sentiment":
        con = connect()
        build_sentiment(con, cfg)
        con.close()
        return
    if cmd == "landing":                    # app shell only (index.html + stubs)
        build_app_shell(cfg)
        return
    if cmd == "site":                       # full weekly build for GitHub Pages
        fetch(refresh=args.refresh)
        con = connect()
        build(con)
        compute(con, cfg)
        build_site(con, cfg, use_tmdb=use_tmdb)
        con.close()
        if args.open:
            webbrowser.open("file:///" + os.path.join(OUT_DIR, "index.html").replace("\\", "/"))
        return
    if cmd == "browse":                     # rebuild the app (catalog + shell) only
        con = connect()
        build_app(con, cfg)
        con.close()
        if args.open:
            webbrowser.open("file:///" + os.path.join(OUT_DIR, "index.html").replace("\\", "/"))
        return
    if cmd in ("fetch", "run", "all"):
        fetch(refresh=args.refresh)
    if cmd in ("build", "run", "all"):
        con = connect()
        build(con)
        compute(con, cfg)
        con.close()
    if cmd == "digest":
        con = connect()
        compute(con, cfg)
        con.close()
    if cmd in ("digest", "run", "all", "send"):
        con = connect()
        html_path, rows, new_ids, providers, override, meta = build_digest(con, cfg, use_tmdb=use_tmdb)
        if cmd == "send" or cfg.get("telegram_bot_token"):
            send_telegram(cfg, telegram_text(rows, new_ids, providers, meta))
        con.close()
        if args.open:
            webbrowser.open("file:///" + html_path.replace("\\", "/"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
