#!/usr/bin/env python3
"""
Movie Finder - Phase 0 personal weekly digest.

Zero-dependency (Python standard library only). Finds genuinely good movies
(Indian regional + international) using PER-CATEGORY IMDb vote baselines, so a
film is judged against the vote norms of its own category rather than one flat bar.
Optionally maps each pick to the Indian OTT platform that streams it (TMDB) and
tags its true original language (TMDB), then renders a weekly digest as HTML +
Markdown (optionally pushes to Telegram).

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
KNOWN_INTL_LANGS = {"en", "ko", "ja", "es", "fr", "it", "de", "zh", "cn",
                    "ru", "pt", "tr", "fa", "th", "sv", "da", "nl", "pl"}
WESTERN_REGIONS = {"US", "GB", "CA", "AU", "NZ", "IE"}

DEFAULTS = {
    "rating_threshold": 6.5,   # Gate 2: weighted rating must clear this
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
    "browse_vote_floor": 2000, # (browse) min IMDb votes for the all-time browse catalog
    "browse_cap": 6000,        # (browse) max films in the browse catalog
    "browse_workers": 8,       # concurrent TMDB lookups when building the catalog
    "sentiment_cap": 1500,     # (sentiment) how many top-voted films to fetch reviews for
    "sentiment_reviews": 25,   # (sentiment) max reviews analyzed per film
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
            start_year INTEGER, runtime_min INTEGER, genres TEXT
        );
        CREATE TABLE ratings (tconst TEXT PRIMARY KEY, avg_rating REAL, num_votes INTEGER);
        CREATE TABLE title_lang (tconst TEXT PRIMARY KEY, language_bucket TEXT, market TEXT);
        CREATE TABLE baselines (
            bucket TEXT PRIMARY KEY, ref_count INTEGER, v_min INTEGER,
            m INTEGER, c REAL, p90 INTEGER, rolled INTEGER
        );
        CREATE TABLE scores (
            tconst TEXT PRIMARY KEY, bucket TEXT, market TEXT, wr REAL,
            eligible INTEGER, confidence TEXT, vote_percentile REAL
        );
        CREATE TABLE IF NOT EXISTS digest_history (tconst TEXT PRIMARY KEY, first_surfaced TEXT);
        CREATE TABLE IF NOT EXISTS tmdb_cache (
            tconst TEXT PRIMARY KEY, orig_lang TEXT, providers_json TEXT, fetched TEXT
        );
        """
    )


def iter_tsv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        f.readline()  # header
        for line in f:
            yield line.rstrip("\n").split("\t")


def load_basics(con):
    path = os.path.join(DATA_DIR, IMDB_FILES["basics"])
    log("loading titles (movies only) ...")
    cur = con.cursor()
    batch = []
    scanned = kept = 0
    for row in iter_tsv(path):
        if len(row) < 9:
            continue
        scanned += 1
        if row[1] != "movie" or row[4] == "1":  # keep features, drop adult
            continue
        batch.append((row[0], row[2], row[3],
                      safe_int(row[5]) if row[5] != NULL else None,
                      safe_int(row[7]) if row[7] != NULL else None,
                      "" if row[8] == NULL else row[8]))
        kept += 1
        if len(batch) >= 50000:
            cur.executemany("INSERT OR REPLACE INTO titles VALUES(?,?,?,?,?,?)", batch)
            batch.clear()
        if scanned % 2000000 == 0:
            log(f"  basics scanned {scanned:,}")
    if batch:
        cur.executemany("INSERT OR REPLACE INTO titles VALUES(?,?,?,?,?,?)", batch)
    con.commit()
    log(f"  movies kept: {kept:,}")


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
    log("indexing movie ids for language bucketing ...")
    movie_ids = set(r[0] for r in con.execute("SELECT tconst FROM titles"))
    log(f"  {len(movie_ids):,} movie ids in memory")
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
        if tid is None or tid not in movie_ids:
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
            if tid in movie_ids:
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
    log("loading rated movies for baseline computation ...")
    rows = con.execute(
        """
        SELECT t.tconst, t.start_year, r.avg_rating, r.num_votes,
               COALESCE(l.language_bucket,'intl'), COALESCE(l.market,'international')
        FROM titles t JOIN ratings r ON r.tconst = t.tconst
        LEFT JOIN title_lang l ON l.tconst = t.tconst
        """
    ).fetchall()
    log(f"  {len(rows):,} rated movies")

    ref = defaultdict(list)  # bucket -> [(votes, rating)]
    for tc, sy, ar, nv, bucket, market in rows:
        if sy and sy >= ref_from and nv >= cfg["ref_vote_cutoff"]:
            ref[bucket].append((nv, ar))
    ref_global = [x for lst in ref.values() for x in lst]

    def baseline_from(pairs):
        votes = sorted(p[0] for p in pairs)
        vmin = int(clamp(percentile(votes, 35), cfg["vote_floor_min"], cfg["vote_floor_max"]))
        m = int(clamp(percentile(votes, 60), cfg["prior_min"], cfg["prior_max"]))
        c_ratings = [r for (v, r) in pairs if v >= vmin] or [r for (_, r) in pairs]
        c = trimmed_mean(c_ratings, 0.05)
        return {"votes": votes, "vmin": vmin, "m": m, "c": round(c, 3),
                "p90": int(percentile(votes, 90)), "n": len(pairs)}

    gb = baseline_from(ref_global) if ref_global else {
        "votes": [], "vmin": cfg["vote_floor_min"], "m": cfg["prior_min"],
        "c": 6.0, "p90": 0, "n": 0}
    bl = {}
    for bucket, pairs in ref.items():
        if len(pairs) >= cfg["min_ref_titles"]:
            d = baseline_from(pairs)
            d["rolled"] = 0
            bl[bucket] = d
        else:
            bl[bucket] = dict(gb, n=len(pairs), rolled=1)
    bl["GLOBAL"] = dict(gb, rolled=0)

    con.execute("DELETE FROM baselines")
    con.executemany(
        "INSERT OR REPLACE INTO baselines VALUES(?,?,?,?,?,?,?)",
        [(b, d["n"], d["vmin"], d["m"], d["c"], d["p90"], d.get("rolled", 0))
         for b, d in bl.items()],
    )

    log("per-category baselines (last %dy, votes>=%d):" % (cfg["ref_window_years"], cfg["ref_vote_cutoff"]))
    print(f"    {'bucket':<26}{'ref#':>8}{'v_min':>8}{'m':>8}{'C':>7}{'P90':>9}   note")
    for b, d in sorted(bl.items(), key=lambda kv: -kv[1]["n"]):
        if b == "GLOBAL":
            continue
        note = "rolled->GLOBAL" if d.get("rolled") else ""
        print(f"    {LANG_NAMES.get(b, b):<26}{d['n']:>8}{d['vmin']:>8}{d['m']:>8}"
              f"{d['c']:>7.2f}{d['p90']:>9}   {note}")

    thr = cfg["rating_threshold"]
    score_rows = []
    for tc, sy, ar, nv, bucket, market in rows:
        d = bl.get(bucket) or gb
        d_use = gb if d.get("rolled") else d
        v, R, m, C, vmin = nv, ar, d_use["m"], d_use["c"], d_use["vmin"]
        wr = (v / (v + m)) * R + (m / (v + m)) * C
        eligible = 1 if (v >= vmin and wr >= thr) else 0
        vp = pct_rank(d_use["votes"], v) if d_use["votes"] else 0.0
        conf = "confirmed" if (v >= m and sy and sy <= cur_year - 1) else "provisional"
        score_rows.append((tc, bucket, market, round(wr, 2), eligible, conf, round(vp, 1)))

    con.execute("DELETE FROM scores")
    cur = con.cursor()
    for i in range(0, len(score_rows), 50000):
        cur.executemany("INSERT OR REPLACE INTO scores VALUES(?,?,?,?,?,?,?)",
                        score_rows[i:i + 50000])
    con.commit()
    elig = sum(1 for r in score_rows if r[4])
    log(f"  scored {len(score_rows):,} movies; {elig:,} pass the two-gate quality bar")


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


def _tmdb_fetch_one(tc, cfg, with_providers):
    rec = {"lang": None, "votes": 0, "rating": 0.0, "offers": []}
    try:
        found = tmdb_get(f"/find/{tc}", {"external_source": "imdb_id"}, cfg)
        results = found.get("movie_results") or []
        if results:
            m0 = results[0]
            rec["lang"] = m0.get("original_language")
            rec["votes"] = m0.get("vote_count", 0) or 0
            rec["rating"] = m0.get("vote_average", 0.0) or 0.0
            if with_providers:
                prov = tmdb_get(f"/movie/{m0['id']}/watch/providers", {}, cfg)
                inr = (prov.get("results") or {}).get("IN") or {}
                for otype in ("flatrate", "free", "ads", "rent", "buy"):
                    for p in inr.get(otype, []):
                        rec["offers"].append({"type": otype, "name": p.get("provider_name", "?")})
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return tc, None
    return tc, rec


def enrich_tmdb(con, cfg, tconsts, with_providers=True, workers=8):
    """Look up each IMDb id on TMDB -> info{tconst -> {lang, votes, rating, offers}}.
    Concurrent (thread pool) for speed; results cached for the day. Network happens in
    worker threads; all SQLite writes happen on this (main) thread as results arrive."""
    con.execute("""CREATE TABLE IF NOT EXISTS tmdb_cache2
        (tconst TEXT PRIMARY KEY, info_json TEXT, has_prov INT, fetched TEXT)""")
    today = date.today().isoformat()
    info, to_fetch = {}, []
    for tc in tconsts:
        row = con.execute("SELECT info_json, has_prov FROM tmdb_cache2 WHERE tconst=? AND fetched=?",
                          (tc, today)).fetchone()
        if row and (row[1] or not with_providers):
            info[tc] = json.loads(row[0])
        else:
            to_fetch.append(tc)
    if to_fetch:
        log(f"  enriching {len(to_fetch)} titles via TMDB ({workers} workers) ...")
        done = misses = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for tc, rec in ex.map(lambda t: _tmdb_fetch_one(t, cfg, with_providers), to_fetch):
                if rec is None:
                    rec = {"lang": None, "votes": 0, "rating": 0.0, "offers": []}
                    misses += 1
                info[tc] = rec
                con.execute("INSERT OR REPLACE INTO tmdb_cache2 VALUES(?,?,?,?)",
                            (tc, json.dumps(rec), 1 if with_providers else 0, today))
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


def discover_lang_stats(cfg, lang, years, max_pages, vote_floor):
    """Enumerate a language's films from TMDB /discover, newest 'years' window,
    sorted by vote_count desc. Returns (votes[], ratings[])."""
    from_date = f"{datetime.now().year - years}-01-01"
    votes, ratings = [], []
    for page in range(1, max_pages + 1):
        try:
            data = tmdb_get("/discover/movie", {
                "with_original_language": lang, "sort_by": "vote_count.desc",
                "primary_release_date.gte": from_date, "vote_count.gte": vote_floor,
                "include_adult": "false", "page": page}, cfg)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            log(f"  discover {lang} page {page} failed: {e}")
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
    """Compute per-language baselines on TMDB's own vote scale (authoritative
    language via original_language). Cached in tmdb_baselines for the day."""
    con.execute("""CREATE TABLE IF NOT EXISTS tmdb_baselines
        (bucket TEXT PRIMARY KEY, v_min INT, m INT, c REAL, p90 INT, n INT, built TEXT)""")
    today = date.today().isoformat()
    if not force:
        cached = con.execute("SELECT bucket, v_min, m, c, p90, n FROM tmdb_baselines WHERE built=?",
                             (today,)).fetchall()
        if len(cached) >= 5:
            return {b: {"vmin": v, "m": m, "c": c, "p90": p, "n": n}
                    for b, v, m, c, p, n in cached}

    log("building TMDB per-language baselines via /discover ...")
    baselines = {}
    for lang in TMDB_INDIAN_LANGS:
        votes, ratings = discover_lang_stats(cfg, lang, years, max_pages, vote_floor=8)
        bl = tmdb_baseline_from(votes, ratings, cfg)
        if bl:
            baselines[lang] = bl
    # pooled international baseline
    ivotes, iratings = [], []
    for lang in TMDB_INTL_LANGS:
        v, r = discover_lang_stats(cfg, lang, years, max_pages, vote_floor=50)
        ivotes += v
        iratings += r
    bl = tmdb_baseline_from(ivotes, iratings, cfg)
    if bl:
        baselines["intl"] = bl

    con.execute("DELETE FROM tmdb_baselines")
    con.executemany("INSERT OR REPLACE INTO tmdb_baselines VALUES(?,?,?,?,?,?,?)",
                    [(b, d["vmin"], d["m"], d["c"], d["p90"], d["n"], today)
                     for b, d in baselines.items()])
    con.commit()

    log("TMDB per-category baselines (last %dy):" % years)
    print(f"    {'bucket':<16}{'films':>7}{'v_min(P35)':>12}{'m(P60)':>9}{'C':>7}{'P90':>9}")
    order = ["intl"] + TMDB_INDIAN_LANGS
    for b in order:
        if b in baselines:
            d = baselines[b]
            print(f"    {LANG_NAMES.get(b, b):<16}{d['n']:>7}{d['vmin']:>12}{d['m']:>9}"
                  f"{d['c']:>7.2f}{d['p90']:>9}")
    return {b: {k: d[k] for k in ("vmin", "m", "c", "p90", "n")} for b, d in baselines.items()}


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
    intl = baselines.get("intl") or {"vmin": 200, "m": 800, "c": 7.0, "p90": 10000, "n": 0}
    pool = con.execute(
        """
        SELECT t.tconst, t.primary_title, t.original_title, t.start_year, t.genres,
               t.runtime_min, r.avg_rating, r.num_votes
        FROM titles t JOIN ratings r ON r.tconst = t.tconst
        WHERE t.start_year >= ? AND r.avg_rating >= ? AND r.num_votes >= ?
        ORDER BY r.avg_rating DESC, r.num_votes DESC LIMIT ?
        """,
        (recent_from, cfg["rating_threshold"], cfg["pool_floor"], cfg["pool_cap"]),
    ).fetchall()
    log(f"TMDB per-language gate: enriching {len(pool)} candidate titles ...")
    info = enrich_tmdb(con, cfg, [p[0] for p in pool], with_providers=True)

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
        bl = baselines.get(bucket, intl)
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
        WHERE s.eligible = 1 AND t.start_year >= ?
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
    else:
        ts = sum(text_scores) / len(text_scores)
        conf = "low"
    return {"n": len(reviews), "ts": round(ts, 1), "aspects": aspects, "verdict": verdict,
            "div": round(ts - imdb_rating, 1) if imdb_rating else 0.0, "conf": conf}


def _fetch_reviews_one(tc, cfg, max_reviews):
    try:
        found = tmdb_get(f"/find/{tc}", {"external_source": "imdb_id"}, cfg)
        res = found.get("movie_results") or []
        if not res:
            return tc, []
        mid = res[0]["id"]
        reviews = []
        for page in (1, 2):
            data = tmdb_get(f"/movie/{mid}/reviews", {"page": page}, cfg)
            for r in data.get("results", []):
                reviews.append({"content": r.get("content", ""),
                                "rating": (r.get("author_details") or {}).get("rating")})
            if page >= data.get("total_pages", 1) or len(reviews) >= max_reviews:
                break
        return tc, reviews[:max_reviews]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError):
        return tc, None


def build_sentiment(con, cfg):
    """Fetch TMDB user reviews for the most-voted catalog films and derive a
    'viewer verdict' (True Sentiment + aspect breakdown + divergence from the rating)."""
    if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
        log("sentiment needs a TMDB key. Add one to config.json.")
        return 0, 0
    con.execute("""CREATE TABLE IF NOT EXISTS sentiment
        (tconst TEXT PRIMARY KEY, n INT, ts REAL, div REAL, conf TEXT, verdict TEXT, computed TEXT)""")
    today = date.today().isoformat()
    rows = con.execute(
        "SELECT t.tconst, r.avg_rating FROM titles t JOIN ratings r ON r.tconst = t.tconst "
        "WHERE r.avg_rating >= ? AND r.num_votes >= ? ORDER BY r.num_votes DESC LIMIT ?",
        (cfg["rating_threshold"], cfg["browse_vote_floor"], cfg["sentiment_cap"])).fetchall()
    imdb_r = {tc: ar for tc, ar in rows}
    done = set(r[0] for r in con.execute("SELECT tconst FROM sentiment WHERE computed=?", (today,)))
    todo = [tc for tc, _ in rows if tc not in done]
    log(f"sentiment: analyzing TMDB reviews for {len(todo)} films ({cfg['browse_workers']} workers) ...")
    got = 0
    with ThreadPoolExecutor(max_workers=cfg["browse_workers"]) as ex:
        for i, (tc, reviews) in enumerate(
                ex.map(lambda t: _fetch_reviews_one(t, cfg, cfg["sentiment_reviews"]), todo)):
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
    log(f"sentiment: {got} of {len(todo)} newly analyzed had TMDB reviews; {total} films have a verdict total")
    return got, len(todo)


# --------------------------------------------------------------------------- #
# 8. interactive browse dashboard (all-time, filterable)
# --------------------------------------------------------------------------- #
def build_browse(con, cfg, all_time=True):
    """Build an interactive, filterable dashboard of the good-films catalog.
    Unlike the weekly digest (a tight per-language gate), browse is INCLUSIVE:
    every film rated >= threshold with enough votes, tagged with its true language
    and India availability, for the user to filter/search/sort themselves."""
    if not (cfg.get("tmdb_bearer") or cfg.get("tmdb_api_key")):
        log("browse needs a TMDB key (accurate language + availability). Add one to config.json.")
        return None, 0
    baselines = build_tmdb_baselines(con, cfg)
    intl = baselines.get("intl") or {"vmin": 200, "m": 800, "c": 7.0, "p90": 10000, "n": 0}
    cur_year = datetime.now().year
    params = [cfg["rating_threshold"], cfg["browse_vote_floor"]]
    year_sql = ""
    if not all_time:
        year_sql = "AND t.start_year >= ? "
        params.append(cur_year - cfg["recent_years"] + 1)
    params.append(cfg["browse_cap"])
    rows = con.execute(
        "SELECT t.tconst, t.primary_title, t.original_title, t.start_year, t.genres, "
        "r.avg_rating, r.num_votes "
        "FROM titles t JOIN ratings r ON r.tconst = t.tconst "
        "WHERE r.avg_rating >= ? AND r.num_votes >= ? " + year_sql +
        "ORDER BY r.avg_rating DESC, r.num_votes DESC LIMIT ?", params).fetchall()
    log(f"browse: {len(rows)} candidate films (all_time={all_time}); resolving language + availability ...")
    info = enrich_tmdb(con, cfg, [r[0] for r in rows], with_providers=True, workers=cfg["browse_workers"])

    sent = {}
    try:
        for tc, ts, div, conf, verdict in con.execute(
                "SELECT tconst, ts, div, conf, verdict FROM sentiment WHERE n>0"):
            sent[tc] = {"ts": ts, "div": div, "c": conf, "v": verdict}
    except sqlite3.OperationalError:
        pass  # sentiment engine not run yet

    films, plat_count, lang_count = [], Counter(), Counter()
    for r in rows:
        rec = info.get(r[0])
        if not rec or not rec.get("lang"):
            continue
        bucket, market = lang_to_bucket(rec["lang"])
        bl = baselines.get(bucket, intl)
        tv = rec.get("votes", 0) or 0
        pct = round(approx_pct(bl, tv)) if tv > 0 else 0
        offers = rec.get("offers", [])
        st = sorted({o["name"] for o in offers if o["type"] in ("flatrate", "free", "ads")})
        rb = sorted({o["name"] for o in offers if o["type"] in ("rent", "buy")})
        for p in st:
            plat_count[p] += 1
        lang_count[bucket] += 1
        genres = [g for g in (r[4] or "").split(",") if g]
        film = {"t": r[0], "n": r[1] or r[2], "y": r[3] or 0, "l": bucket,
                "ln": LANG_NAMES.get(bucket, bucket), "mk": market,
                "r": round(r[5], 1), "v": r[6], "p": pct, "s": st, "b": rb, "g": genres}
        if r[0] in sent:
            film["se"] = sent[r[0]]
        films.append(film)
    films.sort(key=lambda f: (-f["r"], -f["v"]))
    years = [f["y"] for f in films if f["y"]] or [cur_year]
    meta = {"count": len(films), "date": date.today().isoformat(), "all_time": all_time,
            "cur_year": cur_year, "year_min": min(years), "year_max": max(years),
            "threshold": cfg["rating_threshold"]}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "browse.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_browse_html(films, plat_count, lang_count, meta))
    log(f"browse dashboard written: {path} ({len(films)} films)")
    return path, len(films)


def render_browse_html(films, plat_count, lang_count, meta):
    esc = html_lib.escape
    data = json.dumps(films, ensure_ascii=False).replace("</", "<\\/")
    order = ["ml", "ta", "te", "kn", "bn", "mr", "hi", "pa", "gu", "or", "as", "other_in", "intl"]
    lang_chips = "".join(
        f'<button class="chip" data-k="lang" data-v="{b}">{esc(LANG_NAMES.get(b, b))}'
        f'<span class="n">{lang_count[b]}</span></button>'
        for b in order if b in lang_count)
    plat_chips = "".join(
        f'<button class="chip" data-k="plat" data-v="{esc(p)}">{esc(p)}'
        f'<span class="n">{c}</span></button>' for p, c in plat_count.most_common(16))
    scope = "all-time" if meta["all_time"] else f"{meta['cur_year'] - 1}-{meta['cur_year']}"
    return (BROWSE_TEMPLATE
            .replace("__DATA__", data)
            .replace("__LANG_CHIPS__", lang_chips)
            .replace("__PLAT_CHIPS__", plat_chips)
            .replace("__COUNT__", str(meta["count"]))
            .replace("__SCOPE__", scope)
            .replace("__DATE__", meta["date"])
            .replace("__YEARMIN__", str(meta["year_min"]))
            .replace("__YEARMAX__", str(meta["year_max"]))
            .replace("__THRESH__", str(meta["threshold"])))


BROWSE_TEMPLATE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Movie Finder - Browse</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--ink:#1b1d22;--muted:#5c616c;--faint:#8a8f9a;--line:#e4e5eb;
--accent:#a3172e;--accent-soft:#fbeef0;--good:#12452b;--good-bg:#e5f4ec;--chipbg:#eeeef2;}
@media (prefers-color-scheme:dark){:root{--bg:#121317;--card:#1c1e24;--ink:#e8e8ec;--muted:#a6aab4;
--faint:#787d88;--line:#2c2f37;--accent:#e56a7c;--accent-soft:#331f27;--good:#7ee0aa;--good-bg:#16311f;--chipbg:#262932;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 80px}
.eyebrow{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:Georgia,serif;font-size:30px;margin:6px 0 4px}
.sub{color:var(--muted);margin:0 0 12px;font-size:14px}
.sub b{color:var(--ink);font-variant-numeric:tabular-nums}
.pbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 16px;font-size:13px;color:var(--muted)}
.pbar select{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font-size:13px}
.pbar .mini{padding:6px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--muted);cursor:pointer;font-size:12.5px}
.pbar .mini:hover{color:var(--accent);border-color:var(--accent)}
.pbar .tastesum{color:var(--faint);font-size:12px}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:16px}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px}
.search{flex:1;min-width:200px;padding:9px 13px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);font-size:14px}
.select,.clearbtn{padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:var(--card);color:var(--ink);font-size:13px;cursor:pointer}
.rng{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.rng b{color:var(--ink);font-variant-numeric:tabular-nums;min-width:26px}
.rng input[type=range]{accent-color:var(--accent)}
.yr{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted)}
.ynum{width:64px;padding:7px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font-size:13px}
.chk{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);cursor:pointer;white-space:nowrap}
.chk input{accent-color:var(--accent)}
.clearbtn:hover{color:var(--accent);border-color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:8px}
.chip{border:1px solid var(--line);background:var(--chipbg);color:var(--muted);border-radius:999px;padding:5px 11px;font-size:12.5px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.12s}
.chip:hover{border-color:var(--accent);color:var(--ink)}
.chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.chip .n{font-size:10.5px;opacity:.7;font-variant-numeric:tabular-nums}
.chip.active .n{opacity:.85}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}
.card{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:11px;padding:12px 13px;transition:.12s}
.card:hover{border-color:var(--accent)}
.sc{font-family:Georgia,serif;font-size:22px;font-weight:700;color:var(--accent);min-width:40px;text-align:center;font-variant-numeric:tabular-nums;padding-top:1px}
.cbody{min-width:0;flex:1}
.ct{font-weight:650;font-size:15px;line-height:1.25}
.ct a{color:inherit;text-decoration:none}
.ct a:hover{color:var(--accent)}
.cy{color:var(--faint);font-weight:400;font-size:13px}
.match{display:inline-block;background:var(--accent);color:#fff;border-radius:5px;padding:0 6px;font-size:10.5px;font-weight:700;margin-left:4px;vertical-align:middle}
.match.lo{background:var(--chipbg);color:var(--muted)}
.cm{font-size:12.5px;color:var(--muted);margin:3px 0 6px;font-variant-numeric:tabular-nums}
.lchip{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:5px;padding:0 6px;font-size:11px;font-weight:600;margin-right:2px}
.cprov{display:flex;flex-wrap:wrap;gap:4px}
.pv{font-size:11px;border-radius:5px;padding:2px 7px;font-weight:600}
.pv.stream{background:var(--good-bg);color:var(--good)}
.pv.rent{background:var(--chipbg);color:var(--muted)}
.pv.none{background:transparent;color:var(--faint);font-weight:400;padding-left:0}
.acts{display:flex;gap:5px;margin-top:8px}
.act{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:7px;padding:3px 9px;font-size:12px;cursor:pointer;line-height:1.5}
.act:hover{border-color:var(--accent);color:var(--ink)}
.act.like.on{background:var(--good-bg);color:var(--good);border-color:var(--good)}
.act.dislike.on{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.act.seen.on{background:var(--chipbg);color:var(--ink);border-color:var(--faint)}
.act.save.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.sen{font-size:12px;color:var(--muted);margin:1px 0 6px}
.sen b{color:var(--ink)}
.sd{display:inline-block;border-radius:5px;padding:0 6px;font-size:10.5px;font-weight:700;margin-left:5px}
.sd.up{background:var(--good-bg);color:var(--good)}
.sd.down{background:var(--accent-soft);color:var(--accent)}
.note{color:var(--faint);font-size:12.5px;margin:14px 2px}
.empty{text-align:center;color:var(--muted);padding:60px 20px}
.empty a{color:var(--accent)}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);font-size:11.5px;color:var(--faint)}
</style></head><body><div class="wrap">
<div class="eyebrow">Movie Finder &middot; updated __DATE__</div>
<h1>Browse the good stuff</h1>
<p class="sub"><b id="count">__COUNT__</b> of __COUNT__ films &middot; __SCOPE__ &middot; __THRESH__+ IMDb, judged against each language's own vote baseline</p>
<div class="pbar">
  <span>Profile:</span>
  <select id="profile"></select>
  <button id="newp" class="mini">+ New profile</button>
  <span id="tastesum" class="tastesum"></span>
</div>
<div class="controls">
  <div class="row">
    <input id="q" class="search" type="search" placeholder="Search a title...">
    <select id="sort" class="select">
      <option value="rating">Sort: Rating</option>
      <option value="foryou">Sort: For You (personalized)</option>
      <option value="year">Sort: Newest</option>
      <option value="sig">Sort: Category significance</option>
      <option value="sen">Sort: Viewer sentiment</option>
      <option value="votes">Sort: Most rated</option>
    </select>
  </div>
  <div class="row">
    <label class="rng">Min rating <b id="minRv">__THRESH__</b>
      <input id="minR" type="range" min="__THRESH__" max="9.5" step="0.1" value="__THRESH__"></label>
    <label class="yr">Year <input id="ymin" type="number" class="ynum" value="__YEARMIN__">
      &ndash; <input id="ymax" type="number" class="ynum" value="__YEARMAX__"></label>
    <label class="chk"><input id="streamOnly" type="checkbox"> Streaming now</label>
    <label class="chk"><input id="hideIntl" type="checkbox"> Indian only</label>
    <label class="chk"><input id="savedOnly" type="checkbox"> Saved</label>
    <label class="chk"><input id="hideSeen" type="checkbox"> Hide seen</label>
    <label class="chk"><input id="reviewedOnly" type="checkbox"> Reviewed</label>
    <button id="clear" class="clearbtn">Clear</button>
  </div>
  <div class="chips" id="langChips">__LANG_CHIPS__</div>
  <div class="chips" id="platChips">__PLAT_CHIPS__</div>
</div>
<div id="note" class="note" hidden></div>
<div id="grid" class="grid"></div>
<div id="empty" class="empty" hidden>No films match these filters. <a href="#" id="reset">Clear filters</a></div>
<footer>Like / dislike / seen / save are stored privately in this browser (per profile) &mdash; nothing leaves your machine.
Ratings &amp; votes: IMDb (used with permission). Language &amp; availability: TMDB / JustWatch. Movie Finder Phase 0.</footer>
</div>
<script>
const FILMS=__DATA__;
const Y0=__YEARMIN__, Y1=__YEARMAX__, R0=__THRESH__;
const $=s=>document.querySelector(s);
const BYID={};FILMS.forEach(f=>BYID[f.t]=f);
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

/* ---- profiles + saved state (localStorage) ---- */
function lget(k,d){try{const v=localStorage.getItem(k);return v?JSON.parse(v):d;}catch(e){return d;}}
function lset(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
let PROFILES=lget("mf_profiles",["Me"]);
let ACTIVE=lget("mf_active","Me");
if(!PROFILES.includes(ACTIVE)){ACTIVE=PROFILES[0]||"Me";}
function dkey(){return "mf_data_"+ACTIVE;}
let U=lget(dkey(),{like:{},dislike:{},seen:{},save:{}});
function saveU(){lset(dkey(),U);}

/* ---- taste model (content-based over genre / language / decade) ---- */
let taste={g:{},l:{},d:{}};
function norm(o){let m=0;for(const k in o)m=Math.max(m,Math.abs(o[k]));if(m>0)for(const k in o)o[k]/=m;}
function computeTaste(){
  taste={g:{},l:{},d:{}};
  const bump=(f,w)=>{(f.g||[]).forEach(g=>taste.g[g]=(taste.g[g]||0)+w);
    taste.l[f.l]=(taste.l[f.l]||0)+w;const dc=Math.floor((f.y||0)/10)*10;taste.d[dc]=(taste.d[dc]||0)+w;};
  Object.keys(U.like).forEach(t=>BYID[t]&&bump(BYID[t],1));
  Object.keys(U.dislike).forEach(t=>BYID[t]&&bump(BYID[t],-1.2));
  norm(taste.g);norm(taste.l);norm(taste.d);
}
function hasTaste(){return Object.keys(U.like).length>0;}
function raw(f){let s=0;const g=f.g||[];
  if(g.length){let t=0;g.forEach(x=>t+=(taste.g[x]||0));s+=t/g.length;}
  s+=1.3*(taste.l[f.l]||0);const dc=Math.floor((f.y||0)/10)*10;s+=0.4*(taste.d[dc]||0);return s;}
function matchPct(f){if(!hasTaste())return null;return Math.round(100/(1+Math.exp(-1.5*raw(f))));}
function matchWhy(f){let best=null,bv=0;(f.g||[]).forEach(g=>{if((taste.g[g]||0)>bv){bv=taste.g[g];best=g;}});
  return best?("matches your taste for "+best):"based on your likes";}

const state={q:"",sort:"rating",minR:R0,ymin:Y0,ymax:Y1,streamOnly:false,hideIntl:false,savedOnly:false,hideSeen:false,reviewedOnly:false,langs:new Set(),plats:new Set()};

function badges(f){
  if(f.s.length) return f.s.map(p=>`<span class="pv stream">${esc(p)}</span>`).join("");
  if(f.b.length) return f.b.map(p=>`<span class="pv rent">${esc(p)}</span>`).join("");
  return `<span class="pv none">not on tracked Indian OTTs</span>`;
}
function ab(k,label,t){const on=U[k][t]?" on":"";return `<button class="act ${k}${on}" data-act="${k}" data-t="${t}">${label}</button>`;}
function senLine(f){
  if(!f.se)return "";
  const badge=f.se.div>=1.5?`<span class="sd up">viewers rate it higher</span>`:"";
  return `<div class="sen">TMDB viewers <b>${f.se.ts}</b>/10 &middot; ${esc(f.se.v)}${badge}</div>`;
}
function card(f){
  const sig=f.p>=50?`top ${Math.max(1,100-f.p)}% of ${esc(f.ln)}`:esc(f.ln);
  const m=matchPct(f);
  const mb=m!=null?`<span class="match ${m<50?"lo":""}" title="${esc(matchWhy(f))}">${m}% match</span>`:"";
  return `<div class="card">
    <div class="sc">${f.r.toFixed(1)}</div>
    <div class="cbody">
      <div class="ct"><a href="https://www.imdb.com/title/${f.t}/" target="_blank" rel="noopener">${esc(f.n)}</a> <span class="cy">${f.y||""}</span>${mb}</div>
      <div class="cm"><span class="lchip">${esc(f.ln)}</span>${f.v.toLocaleString()} votes &middot; ${sig}</div>
      ${senLine(f)}
      <div class="cprov">${badges(f)}</div>
      <div class="acts">${ab("like","Like",f.t)}${ab("dislike","Not for me",f.t)}${ab("seen","Seen",f.t)}${ab("save","Save",f.t)}</div>
    </div></div>`;
}
const SORT={rating:(a,b)=>b.r-a.r||b.v-a.v,foryou:(a,b)=>raw(b)-raw(a)||b.r-a.r,
  year:(a,b)=>b.y-a.y||b.r-a.r,sig:(a,b)=>b.p-a.p||b.r-a.r,
  sen:(a,b)=>((b.se?b.se.ts:-1)-(a.se?a.se.ts:-1))||b.r-a.r,votes:(a,b)=>b.v-a.v};
function apply(){
  const q=state.q.toLowerCase();
  let out=FILMS.filter(f=>f.r>=state.minR
    &&(!q||f.n.toLowerCase().includes(q))
    &&(f.y>=state.ymin&&f.y<=state.ymax)
    &&(!state.streamOnly||f.s.length>0)
    &&(!state.hideIntl||f.mk==="indian")
    &&(!state.savedOnly||U.save[f.t])
    &&(!state.hideSeen||!U.seen[f.t])
    &&(!state.reviewedOnly||f.se)
    &&(!state.langs.size||state.langs.has(f.l))
    &&(!state.plats.size||f.s.some(p=>state.plats.has(p))));
  out.sort(SORT[state.sort]);
  $("#count").textContent=out.length;
  $("#grid").innerHTML=out.slice(0,600).map(card).join("");
  $("#empty").hidden=out.length>0;
  const note=$("#note");
  if(out.length>600){note.hidden=false;note.textContent=`Showing the first 600 of ${out.length} - narrow with filters or search to see the rest.`;}
  else note.hidden=true;
}
function updateSummary(){
  const nl=Object.keys(U.like).length,ns=Object.keys(U.seen).length,nv=Object.keys(U.save).length;
  $("#tastesum").textContent=nl?`${nl} liked · ${ns} seen · ${nv} saved`:"Like a few films (buttons on each card) to get personalized picks";
}
function renderProfiles(){$("#profile").innerHTML=PROFILES.map(p=>`<option${p===ACTIVE?" selected":""}>${esc(p)}</option>`).join("");}
function setSort(v){state.sort=v;$("#sort").value=v;}
function switchProfile(){U=lget(dkey(),{like:{},dislike:{},seen:{},save:{}});computeTaste();updateSummary();
  setSort(hasTaste()?"foryou":"rating");apply();}

$("#grid").addEventListener("click",e=>{
  const b=e.target.closest(".act");if(!b)return;
  const k=b.dataset.act,t=b.dataset.t;
  if(U[k][t]){delete U[k][t];}else{U[k][t]=1;if(k==="like")delete U.dislike[t];if(k==="dislike")delete U.like[t];}
  saveU();computeTaste();updateSummary();apply();
});
$("#profile").addEventListener("change",e=>{ACTIVE=e.target.value;lset("mf_active",ACTIVE);switchProfile();});
$("#newp").addEventListener("click",()=>{const n=(prompt("Name this profile:")||"").trim();if(!n)return;
  if(!PROFILES.includes(n)){PROFILES.push(n);lset("mf_profiles",PROFILES);}
  ACTIVE=n;lset("mf_active",ACTIVE);renderProfiles();switchProfile();});
$("#q").addEventListener("input",e=>{state.q=e.target.value;apply();});
$("#sort").addEventListener("change",e=>{state.sort=e.target.value;apply();});
$("#minR").addEventListener("input",e=>{state.minR=+e.target.value;$("#minRv").textContent=(+e.target.value).toFixed(1);apply();});
$("#ymin").addEventListener("input",e=>{state.ymin=+e.target.value||0;apply();});
$("#ymax").addEventListener("input",e=>{state.ymax=+e.target.value||9999;apply();});
$("#streamOnly").addEventListener("change",e=>{state.streamOnly=e.target.checked;apply();});
$("#hideIntl").addEventListener("change",e=>{state.hideIntl=e.target.checked;apply();});
$("#savedOnly").addEventListener("change",e=>{state.savedOnly=e.target.checked;apply();});
$("#hideSeen").addEventListener("change",e=>{state.hideSeen=e.target.checked;apply();});
$("#reviewedOnly").addEventListener("change",e=>{state.reviewedOnly=e.target.checked;apply();});
document.querySelectorAll(".chip").forEach(ch=>ch.addEventListener("click",()=>{
  const set=ch.dataset.k==="lang"?state.langs:state.plats, v=ch.dataset.v;
  if(set.has(v)){set.delete(v);ch.classList.remove("active");}else{set.add(v);ch.classList.add("active");}
  apply();
}));
function resetAll(){
  state.q="";$("#q").value="";state.minR=R0;$("#minR").value=R0;$("#minRv").textContent=R0.toFixed(1);
  state.ymin=Y0;$("#ymin").value=Y0;state.ymax=Y1;$("#ymax").value=Y1;
  state.streamOnly=false;$("#streamOnly").checked=false;state.hideIntl=false;$("#hideIntl").checked=false;
  state.savedOnly=false;$("#savedOnly").checked=false;state.hideSeen=false;$("#hideSeen").checked=false;
  state.reviewedOnly=false;$("#reviewedOnly").checked=false;
  state.langs.clear();state.plats.clear();
  document.querySelectorAll(".chip.active").forEach(c=>c.classList.remove("active"));apply();
}
$("#clear").addEventListener("click",resetAll);
$("#reset").addEventListener("click",e=>{e.preventDefault();resetAll();});
renderProfiles();computeTaste();updateSummary();if(hasTaste())setSort("foryou");apply();
</script></body></html>'''


# --------------------------------------------------------------------------- #
# 9. landing page + full site build (for GitHub Pages)
# --------------------------------------------------------------------------- #
def build_landing(cfg):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(LANDING_TEMPLATE.replace("__DATE__", date.today().isoformat()))
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()   # serve files as-is on Pages
    log(f"landing page written: {os.path.join(OUT_DIR, 'index.html')}")


def build_site(con, cfg, use_tmdb=True):
    """Full weekly build for publishing: digest + sentiment + browse + landing."""
    build_digest(con, cfg, use_tmdb=use_tmdb)
    build_sentiment(con, cfg)
    build_browse(con, cfg, all_time=True)
    build_landing(cfg)


LANDING_TEMPLATE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Movie Finder</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--ink:#1b1d22;--muted:#5c616c;--faint:#8a8f9a;--line:#e4e5eb;
--accent:#a3172e;--accent-soft:#fbeef0;}
@media (prefers-color-scheme:dark){:root{--bg:#121317;--card:#1c1e24;--ink:#e8e8ec;--muted:#a6aab4;
--faint:#787d88;--line:#2c2f37;--accent:#e56a7c;--accent-soft:#331f27;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:64px 20px 80px}
.eyebrow{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:Georgia,serif;font-size:44px;margin:10px 0 8px;letter-spacing:-.01em}
.tag{color:var(--muted);font-size:18px;margin:0 0 40px;max-width:34ch}
.cards{display:grid;gap:14px}
a.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:24px 26px;text-decoration:none;color:inherit;transition:.14s}
a.card:hover{border-color:var(--accent);transform:translateY(-2px)}
.ct{font-family:Georgia,serif;font-size:23px;margin:0 0 4px}
.cd{color:var(--muted);font-size:14.5px;margin:0}
.arrow{color:var(--accent);font-weight:700}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);font-size:12px;color:var(--faint)}
</style></head><body><div class="wrap">
<div class="eyebrow">Updated __DATE__</div>
<h1>Movie Finder</h1>
<p class="tag">Genuinely good movies &mdash; Indian and international &mdash; and where to watch them in India.</p>
<div class="cards">
  <a class="card" href="digest-latest.html">
    <div class="ct">This week&rsquo;s picks <span class="arrow">&rarr;</span></div>
    <p class="cd">A short, curated list of genuinely good recent releases, judged against each language&rsquo;s own vote baseline, with where to stream them.</p>
  </a>
  <a class="card" href="browse.html">
    <div class="ct">Browse everything <span class="arrow">&rarr;</span></div>
    <p class="cd">The full all-time catalogue &mdash; filter by language, OTT platform, year and rating, search, and get personalised picks (each person can have their own profile).</p>
  </a>
</div>
<footer>Ratings &amp; votes: IMDb (used with permission). Language, availability &amp; reviews: TMDB / JustWatch.
Built with Movie Finder. Refreshed automatically each week.</footer>
</div></body></html>'''


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    try:  # keep the Windows console from crashing on non-ASCII titles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Movie Finder - Phase 0 personal weekly digest")
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
    if cmd == "landing":
        build_landing(cfg)
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
    if cmd == "browse":
        con = connect()
        all_time = args.years is None   # --years N narrows to the last N years
        path, n = build_browse(con, cfg, all_time=all_time)
        con.close()
        if path and args.open:
            webbrowser.open("file:///" + path.replace("\\", "/"))
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
