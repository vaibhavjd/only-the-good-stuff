# Movie Finder — Phase 0

A **zero-cost, zero-dependency** personal weekly digest of genuinely good movies —
Indian regional **and** international — that you'd otherwise miss.

It solves the core problem: a flat "6.5+ on IMDb" filter is useless because a
Malayalam film with 9,000 votes and a Hollywood film with 9,000 votes are not
remotely comparable in significance. Movie Finder computes a **separate vote
baseline for each language** and judges every film against *its own category*.

This is **Phase 0** of the full [product blueprint](https://claude.ai/code/artifact/1e21f77a-324d-4999-9e0f-caca3a209c5c):
a personal tool to prove the premise before building the real product. It uses
only the Python standard library and the free IMDb datasets.

---

## Quick start

Requires **Python 3.8+** (you have 3.11). No `pip install`, no accounts, nothing paid.

```powershell
python movie_finder.py run --open
```

That single command:
1. **Downloads** the free IMDb datasets (~700 MB, cached for 24h) into `data/`.
2. **Builds** a local SQLite database (`moviefinder.db`).
3. **Computes** per-language vote baselines and applies the two-gate quality filter.
4. **Writes** the digest to `out/digest-<date>.html` and `.md`, prints a summary,
   and (`--open`) opens the HTML in your browser.

First run takes a few minutes (mostly the one-time download + parsing 50M rows).
Later runs reuse the cached data and are fast.

---

## How the quality gate works

Every film is placed in a **stratum** (its language bucket). Each bucket gets its
own baselines, recomputed from the data:

- **Gate 1 — significance floor:** `votes ≥ v_min`, where `v_min` is the 35th
  percentile of that language's vote distribution (clamped). A hyped film with too
  few votes *for its category* is rejected regardless of rating.
- **Gate 2 — damped quality bar:** a Bayesian weighted rating must clear 6.5:

  ```
  WR = (v / (v + m))·R  +  (m / (v + m))·C
       v = the film's votes      R = the film's rating
       m = the bucket's prior    C = the bucket's mean rating
  ```

The result: a Malayalam film at 7.0 with 9k votes passes; a Hollywood film with
the *same* numbers fails, because 9k votes is top-tier for Malayalam and merely
average for Hollywood. That asymmetry is the whole point.

The run prints the actual baseline table it computed, e.g.:

```
    bucket          ref#  v_min(P35)  m(P60)     C      P90   note
    International   28431         8000   25000  6.21    41022
    Hindi           1204         1500    9800  5.88    38110
    Malayalam        612         1100    3000  6.40    22400
    ...
```

---

## Recommended: add a free TMDB key (accurate language + where-to-watch)

**Why this matters:** IMDb's free datasets can't reliably tell a film's *original*
language — original titles are romanized and the language codes are dominated by dub
tracks (e.g. *Oppenheimer* carries Hindi/Tamil/Telugu/Kannada/Bengali/Marathi dub
codes; ~91% of films tagged with an Indian language are actually Western films dubbed
for India). So **without a TMDB key the language buckets are coarse**. TMDB's
`original_language` fixes this, and the same lookup returns India OTT availability.

1. Create a free account and open <https://www.themoviedb.org/settings/api>.
2. Copy `config.example.json` to `config.json`.
3. Paste your **API Read Access Token (v4)** into `tmdb_bearer` (or the v3 **API Key**
   into `tmdb_api_key`). Either works.
4. Confirm it works:  `python movie_finder.py tmdb-selftest`
   (should print RRR's `original_language=te` and its India availability).
5. Run:  `python movie_finder.py run --open`

Now the digest's shown titles get their **accurate language** (correct
Malayalam / Tamil / Telugu / Hindi / International sections) and **where-to-watch**
("Stream: JioHotstar", "Rent/Buy: Apple TV", or "not on tracked Indian OTTs").
Lookups are cached in the DB per day.

> Note: this corrects the language of the films *shown* in the digest and adds
> availability. Making the per-language *vote baseline* itself fully TMDB-native
> (so the significance gate is category-relative for every language, not near-global)
> is the next increment — see the blueprint.

## Optional: push to your phone (Telegram)

1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Message **@userinfobot** → copy your numeric chat id.
3. Put both in `config.json` (`telegram_bot_token`, `telegram_chat_id`).
4. Every `run` now also sends the digest to your Telegram.

---

## Make it weekly (Windows Task Scheduler)

```powershell
$action  = New-ScheduledTaskAction -Execute "python" -Argument "`"$PWD\movie_finder.py`" run" -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 8:00am
Register-ScheduledTask -TaskName "MovieFinderWeekly" -Action $action -Trigger $trigger
```

Each Friday 8am it refreshes the data and produces a new digest; anything that
newly crossed the quality bar (or newly landed on an Indian OTT you can add later)
is tagged **NEW**.

---

## Commands & flags

| Command | Does |
|---|---|
| `run` | fetch (if stale) + build + score + weekly digest — the everyday command |
| `browse` | build the interactive all-time browse dashboard (`out/browse.html`) |
| `fetch` | (re)download the IMDb datasets |
| `build` | load datasets into SQLite + compute scores |
| `digest` | rebuild the weekly digest from the current DB (no re-download) |
| `send` | rebuild the digest and push it to Telegram |
| `sentiment` | fetch TMDB user reviews for top films + derive a viewer verdict |
| `tmdb-selftest` | verify your TMDB key works |
| `tmdb-baselines` | print the per-language TMDB vote baselines |

### Personalize the browse dashboard

The browse page has **Like / Not-for-me / Seen / Save** buttons on each card. They're
stored privately in your browser (localStorage), **per profile** (use the Profile
dropdown to add household members). Once you like a few films, the **"For You"** sort
and a **% match** badge appear, learned from the genres/languages/eras you like.

### Viewer sentiment

`python movie_finder.py sentiment` reads TMDB user reviews for the most-voted films
and derives a "TMDB viewers X/10 + what they praised/criticized" line (shown on the
cards; filter with **Reviewed**, sort by **Viewer sentiment**). Then rebuild browse.

> **Coverage caveat:** TMDB has plenty of reviews for popular/international films but
> almost none for Indian cinema (verified: ~7 of 1,000+ Indian films). For real Indian
> review sentiment, a YouTube-comments source (free API key) is the right upgrade.

### Two views: weekly digest vs browse

- **Weekly digest** (`run` / `digest`) is a tight, curated list — only films that clear
  their category's vote bar, grouped by language, for "what should I watch this week".
- **Browse** (`browse`) is the opposite: an interactive, all-time catalog of every
  good film (6.5+ with enough votes), with **filters** (language, OTT platform, year,
  rating), **search**, and **sort** — for exploring. Open `out/browse.html`.

```
python movie_finder.py browse --open        # all-time, opens in your browser
python movie_finder.py browse --years 5      # narrow to the last 5 years
```

The first `browse` build looks up several thousand films on TMDB (a few minutes,
run in parallel); afterwards results are cached, so it is fast. Tune the catalog size
with `browse_vote_floor` / `browse_cap` in `config.json`.

| Flag | Does |
|---|---|
| `--refresh` | force re-download even if cached |
| `--years N` | cover releases from the last N years (default 2) |
| `--threshold 6.5` | change the weighted-rating gate |
| `--no-tmdb` | skip availability lookups |
| `--open` | open the HTML digest when done |

---

## Files

```
movie_finder.py        the whole tool (stdlib only)
config.example.json    copy to config.json to add TMDB / Telegram / tuning
data/                  downloaded IMDb datasets (git-ignored)
moviefinder.db         local SQLite build (git-ignored)
out/                   generated digests, HTML + Markdown (git-ignored)
```

## Run it weekly on GitHub + share with family

The repo ships a GitHub Actions workflow ([.github/workflows/weekly.yml](.github/workflows/weekly.yml))
that rebuilds the digest + browse dashboard **every Friday** and publishes them to a
**GitHub Pages URL** your family can just open — no installs, no accounts for them.

Setup (once):

1. **Create the repo and push** (use your *personal* GitHub account):
   ```
   gh repo create movie-finder --private --source . --remote origin --push
   ```
   (`--public` if you want it public — see the caveat below.)

2. **Add your TMDB key as a secret** (never commit it):
   ```
   gh secret set TMDB_BEARER --body "<your TMDB v4 Read Access Token>"
   ```
   Or on the web: Settings → Secrets and variables → Actions → New repository secret,
   name `TMDB_BEARER`.

3. **Turn on Pages with the Actions source**: repo Settings → Pages → Build and
   deployment → Source = **GitHub Actions**.

4. **Run it once now**: Actions tab → "Weekly Movie Finder" → **Run workflow**. When it
   finishes (~20 min the first time), the run shows your Pages URL
   (`https://<user>.github.io/movie-finder/`). Share that link.

Notes:
- The weekly build re-downloads fresh IMDb data and re-checks TMDB, so the digest and
  availability stay current. Each person's likes/profiles live in their own browser.
- **Public vs private:** free GitHub Pages is **public** (anyone with the link can view).
  A private site needs a paid GitHub plan. See the licensing note below before going public.
- GitHub disables scheduled workflows after ~60 days with no repo commits — push a small
  change (or hit "Run workflow") occasionally to keep it alive.

## Important: licensing

The IMDb datasets are free for **personal, non-commercial** use only, and IMDb
treats *publishing an app* — even a free one — as commercial use. So this Phase 0
tool is for **your own use**. The full product (per the blueprint) drops IMDb data
at launch and displays an in-house composite score (TMDB + Trakt + first-party
ratings, calibrated with the baselines learned here), deep-linking to IMDb rather
than republishing its numbers.

*Information courtesy of IMDb (https://www.imdb.com). Used with permission.*
