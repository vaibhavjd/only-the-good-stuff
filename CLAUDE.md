# Only the good stuff! — project context

A weekly, quality-gated movie **and** TV-show discovery web app for India. Surfaces
genuinely good titles (6.5+ IMDb, judged against per-language×kind vote baselines),
maps each to Indian OTT availability, adds review sentiment, and personalises per user.

- **Live site:** https://vaibhavjd.github.io/only-the-good-stuff/
- **Repo:** https://github.com/vaibhavjd/only-the-good-stuff (public, owner's PERSONAL account `vaibhavjd`)
- **Product name:** "Only the good stuff!" (was "Movie Finder" — do not reintroduce that name in UI)

## Architecture (Python stdlib only — no pip deps)
- `movie_finder.py` — the whole backend/CLI. Downloads IMDb TSVs → SQLite (`moviefinder.db`),
  computes per-(language×kind) vote baselines + two-gate quality filter, enriches via TMDB
  (language, storyline, countries, posters, IN availability, TMDB rating), runs review
  sentiment, and emits the site.
- `templates/app.html` — the single-page app (welcome gate → onboarding → 4 tabs:
  This Week / Movies / Shows / My List). Inline CSS + one inline `<script>`. Placeholders
  `__BUILT__ __THRESH__ __SUPA_URL__ __SUPA_KEY__` are str.replace()'d at build time.
- `out/` (gitignored) — generated: `index.html` (app shell), `data.js`
  (`window.MF_DATA={built,threshold,titles:[…]}`), redirect stubs `browse.html` /
  `digest-latest.html` → index.html, `.nojekyll`.
- `DESIGN-V2.md` — the MF_DATA field contract + UX spec (read before touching either side).
- `config.json` (gitignored) — local keys: tmdb_bearer/tmdb_api_key + supabase_url/anon_key.
- `config.example.json` — documented template.

## CLI
`python movie_finder.py <cmd>` — `site` (full build: fetch+build+catalog+sentiment+app,
what CI runs), `browse` (rebuild app from existing DB — fast, uses cached TMDB), `sentiment`,
`digest`, `landing` (shell only), `tmdb-selftest`, `tmdb-baselines`, `fetch`/`build`/`run`.
First full build ~40-60 min cold (TMDB cache `tmdb_cache3` empty); `browse` reuses cache in seconds.

## MF_DATA title fields (data.js)
`t` imdb id · `k` "m"/"s" · `n` name · `y` start yr · `y2` (series only; null=ongoing) ·
`l`/`ln` lang code/name · `mk` indian/international · `r` IMDb rating · `tr` TMDB rating ·
`v` IMDb votes · `p` category vote percentile · `g` genres[] · `o` storyline · `c` countries[] ·
`img` TMDB poster_path · `s` streaming providers[] · `b` rent/buy[] · `se` {ts,div,v,c} sentiment ·
`fs` first-surfaced date · `nw` 1 if new this week. Frontend suppresses NEW badges if >40% are new (cold start).

## Deployment
- GitHub Actions `.github/workflows/weekly.yml`: Fri 02:30 UTC (~08:00 IST) + manual dispatch.
  Writes config.json from secrets, curls the previous live `data.js` (→ prev-data.js) so NEW
  badges diff week-over-week, runs `site`, sanity-checks (index.html has MF_DATA + data.js ≥1000
  titles), pings Supabase keepalive, deploys to Pages.
- **Repo secrets** (set in repo Settings, NOT committed): `TMDB_BEARER`, `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`.
- **Pushing / the token dance** (IMPORTANT): `gh` CLI on this machine is logged into the owner's
  WORK account `pharmeasyMarketing`, which cannot push to `vaibhavjd`'s repo. To push:
  the user drops a **classic** PAT (`ghp_…`, scopes `repo`+`workflow`) into
  `C:\Users\vaibh\Downloads\ghtoken.txt`; then in one PowerShell block read it into `$env:GH_TOKEN`,
  verify `gh api user --jq .login` == `vaibhavjd` (abort otherwise), push with
  `git -c http.extraheader="AUTHORIZATION: basic <base64 x-access-token:$token>" push` (bypasses
  Windows Credential Manager, which serves the wrong account), then trigger
  `gh workflow run weekly.yml --ref main -R vaibhavjd/only-the-good-stuff`, and DELETE the token file.
  Never print the token. Fine-grained tokens FAIL (repo-scope); only classic works.
- Watch a run: `gh run watch <id> -R vaibhavjd/only-the-good-stuff --exit-status` in PowerShell
  (gh is NOT on Git Bash's PATH, so poll from PowerShell, not Bash).

## Cloud profiles (Supabase + Google sign-in)
- Table `taste` (user_id uuid PK → auth.users, data jsonb, RLS own-row policies). The whole
  per-user `{like,dislike,seen,save,prefs}` object syncs; guest localStorage is the offline
  fallback/cache and is adopted on first login. Anon/publishable key is public (in the page) —
  RLS protects data.
- Google OAuth needs an https (or http://localhost) origin — never `file://`. For LOCAL login
  testing: keys are in local config.json; serve `out/` on **http://localhost:8080** (port 8000 is
  taken on this machine) via `python -m http.server 8080 --bind 127.0.0.1`, and add
  `http://localhost:8080/**` to Supabase Auth → URL Configuration → Redirect URLs.
- After the repo rename, the live **Site URL + redirect URLs** in Supabase must point at
  `https://vaibhavjd.github.io/only-the-good-stuff/` or live Google sign-in breaks.

## Gotchas
- Secrets set via piped `gh secret set` can carry a trailing newline → 401s; the code `.strip()`s
  all config strings and the workflow strips the secret.
- `moviefinder.db` + IMDb `data/` are gitignored and rebuilt fresh each CI run.
