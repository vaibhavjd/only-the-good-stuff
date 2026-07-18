# Movie Finder v2 — Redesign Spec

The single source of truth for the v2 rebuild. Two builders work from this: BACKEND
(movie_finder.py + workflow + config) and FRONTEND (templates/app.html). The contract
between them is the MF_DATA schema and the template placeholders — do not deviate.

## Product requirements (from the owner)
1. Rethought user flow: user lands on a welcome gate BEFORE any recommendations —
   sign-in first (Google via Supabase), with "Continue as guest" as secondary. First
   visit then gets a short onboarding (pick languages + your OTT platforms) before Home.
2. TV shows/series join movies as first-class content.
3. Filtering and sorting must be extremely clear — grouped filter panel, active-filter
   chips, obvious sort control.
4. Weekly picks: user-selectable YEAR RANGES (presets + custom) instead of the fixed
   2-year window, plus sort options including latest→oldest.
5. Listings: expandable snippet per title — storyline, language, country, genres.

## Site structure (GitHub Pages, static)
- `out/index.html`  — THE app (single page): welcome gate → onboarding → Home.
- `out/data.js`     — `window.MF_DATA = {...}` catalog payload (separate file so the
  app shell stays readable; loaded via `<script src="data.js">` before the app script).
- `out/browse.html` and `out/digest-latest.html` — tiny redirect stubs to index.html
  (old family bookmarks must not break).
- Old landing page is retired; index.html IS the app.

## MF_DATA contract (backend emits, frontend consumes)
```js
window.MF_DATA = {
  built: "YYYY-MM-DD",        // build date
  threshold: 6.5,
  titles: [{
    t: "tt1234567",           // imdb id (stable key for likes/saves)
    k: "m" | "s",             // movie | series
    n: "Title",
    y: 2024,                  // start year
    y2: 2026 | null,          // series end year; null = ongoing; movies: omit
    l: "ml",                  // language bucket code (ml/ta/te/kn/bn/mr/hi/pa/gu/or/as/intl)
    ln: "Malayalam",          // display name
    mk: "indian" | "international",
    r: 7.8, v: 12345,         // IMDb rating + votes
    p: 78,                    // vote percentile within its (language × kind) category
    g: ["Drama","Thriller"],
    o: "Storyline snippet…",  // overview, truncated to ≤260 chars ending on a word
    c: ["India"],             // countries (display names, ≤3)
    img: "/abc123.jpg" | "",  // TMDB poster_path; frontend prefixes image.tmdb.org/t/p/w185
    s: ["JioHotstar"],        // streaming providers (IN)
    b: ["Apple TV"],          // rent/buy providers (IN)
    se: {ts:7.4, div:-0.4, v:"praise for …", c:"high"},  // sentiment; omit if none
    fs: "YYYY-MM-DD",         // first date this title surfaced in a build
    nw: 1                     // present iff fs within 8 days of build date ("NEW")
  }]
}
```

## Template placeholders (backend injects into templates/app.html)
`__BUILT__` `__THRESH__` `__SUPA_URL__` `__SUPA_KEY__` — same replace() mechanism as v1.
Data loads via `<script src="data.js"></script>` placed before the main app script.

## User flow (frontend)
1. **Welcome gate** (shown when: no Supabase session AND no `mf_guest=1` in localStorage).
   Brand + one-line pitch + two buttons: **Sign in with Google** (primary; hidden if
   Supabase unconfigured) and **Continue as guest**. No content visible behind it.
2. **Onboarding** (first run per profile: no `mf_onboarded`): two quick steps —
   (a) pick languages you watch (chips, multi); (b) pick OTT platforms you have
   (chips from the catalog's top providers). Skippable. Stored per profile (cloud
   users: inside their taste row data under `prefs`; guests: localStorage).
3. **Home** with 4 tabs:
   - **This Week** (default): "Picks for you" — year-range control (preset chips:
     This year · Last 2 years · Last 10 years · 2010s · 2000s · 90s & older · All time ·
     Custom min–max), sort dropdown, and NEW-this-week badges. Defaults: user's languages,
     their platforms boosted, year preset = Last 2 years, sort = Best rated.
   - **Movies** and **Shows**: full catalog browse of that kind with the filter system.
   - **My List**: saved titles + seen history, with remove controls.
4. Profile bar: guest profiles (dropdown + New/Rename/Delete) or "Syncing as <name>"
   + Sign out — port the existing v1 logic (localStorage guest, Supabase cloud sync,
   merge on login, like-beats-dislike).

## Filter & sort system (applies to This Week / Movies / Shows)
- Always-visible bar: search box, sort dropdown, "Filters" button with active-count badge.
- Sort options (exact list): **Best rated · Newest first · Oldest first · For You ·
  Viewer sentiment · Most voted · Hidden gems** (= category significance).
- Filter panel (collapsible, collapsed by default on <760px; open panel scrolls inside
  itself, max 52vh), groups with clear headers:
  - **Language** — chips with counts
  - **Platform** — chips with counts + "Streaming now" toggle
  - **Year** — preset chips + custom min/max inputs
  - **Rating** — min-rating slider
  - **My stuff** — Indian only · Saved · Hide seen · Reviewed
- Below the bar: removable active-filter chips (each with ×) + "Clear all".
- Personalization: For You sort uses the ported taste model (genre+language+decade,
  % match badge, EMA-free simple weights as in v1).

## Card design (req 5)
Collapsed: poster thumb (fallback: initial letter block), score, title, year(s —
series show "2019–2023" or "2019–"), kind chip (Movie/Show), language chip, genres line,
provider badges, NEW badge, % match, sentiment line if present, action buttons
(Like / Not for me / Seen / Save).
Expanded (tap "More" toggle): storyline snippet, Language: X · Country: Y, votes +
category significance line, IMDb link. Smooth expand, no layout jank.

## Backend changes (movie_finder.py)
- **Ingest**: keep titleType `movie` + add `tvSeries`, `tvMiniSeries`. New column
  `kind` ('m'/'s') and `end_year` on titles. Adult filter stays.
- **Baselines/scores (IMDb path)**: strata become language×kind with roll-ups
  (kind-level GLOBAL). Same two-gate formula.
- **TMDB enrichment v3** (new cache table `tmdb_cache3`, drop nothing old):
  1) `/find/{imdb_id}` → movie_results OR tv_results (sets kind authoritatively);
  2) one details call `/movie/{id}?append_to_response=watch/providers` or
     `/tv/{id}?append_to_response=watch/providers` → original_language, vote_count,
     vote_average, overview, production_countries (tv: origin_country), poster_path,
     IN providers, tv: first/last air year + status.
  Concurrency + 429 retry + daily cache as in v1.
- **TMDB baselines**: keyed (bucket, kind) via /discover/movie and /discover/tv.
- **Sentiment**: route /movie/{id}/reviews vs /tv/{id}/reviews by kind; caps unchanged.
- **first_surfaced**: reuse digest_history semantics for ALL emitted titles →
  `fs`/`nw` fields.
- **build_site**: emits data.js + index.html (from templates/app.html) + redirect stubs
  + keeps email/Telegram digest and sentiment steps. `site` command unchanged externally.
- **Config additions**: `shows_vote_floor` (default 1000), `shows_cap` (default 2500).
- **Workflow**: sanity check now tests out/index.html + out/data.js exist and data.js
  contains at least 1000 titles; keepalive + secrets unchanged.

## Visual identity (keep, refine)
Crimson accent #a3172e (dark: #e56a7c), Georgia display + Segoe UI body, light+dark
via prefers-color-scheme, card-based, IMDb/TMDB/JustWatch attribution in footer.
Mobile-first: BOTTOM tab bar on <760px, top tabs on desktop.

## Non-negotiables
- Guest mode must fully work with Supabase unconfigured (auto-hide sign-in).
- No pip dependencies; Python stdlib only.
- Every external string HTML-escaped; overview snippets must be escaped.
- localStorage keys: keep `mf_*` naming; do NOT break existing v1 stored likes
  (same `mf_data_<profile>` shape).
- Attribution footer: IMDb (used with permission) + TMDB + JustWatch.
