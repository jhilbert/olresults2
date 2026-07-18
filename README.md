# olresults — Orienteering Results Database

A database and static website collecting orienteering competition results,
built to render detailed per-runner profiles: for every race the category,
rank, number of starters, classified finishers, time behind the winner and
percentage behind.

## Architecture

The published application is fully static. Data is ingested from source
systems into raw JSON snapshots, normalized, compiled into a SQLite database
at build time, and queried client-side in the browser via sql.js. Hosted on
GitHub Pages.

Collection and deployment are deliberately separate. A small authenticated
Cloudflare Worker is the only component that talks to ANNE from CI; it keeps
the ANNE API key out of GitHub, exposes only the read endpoints this project
needs, and stores the private championship-eligibility ledger in R2. The
scheduled `sync-data.yml` workflow commits new public snapshots and explicitly
dispatches the pure `deploy-pages.yml` build, which never contacts ANNE.

```
ingest/    source adapters (ANNE API, SportSoftware HTML/PDF parsers)
data/
  raw/     verbatim snapshots from sources (provenance)
  normalized/  parsed legacy results in the common JSON shape
build/     raw + normalized JSON -> site/data/results.db (SQLite)
site/      static frontend (sql.js), deployed to GitHub Pages
cloudflare/anne-gateway/  narrow ANNE read gateway + private R2 state
```

## Data sources

| Tier | Source | Coverage | Quality |
|---|---|---|---|
| 1 | ANNE API (`anne-api.oefol.at/v1`) structured results | ~322 events, growing | splits, stable person ids |
| 2 | SportSoftware HTML attachments | ~775 files | full result lists |
| 3 | SportSoftware PDF attachments | ~1,065 files | full result lists |
| 4 | External links (SPORTident Center, club sites) | ~504 links | varies |

ANNE (anne.orienteeringaustria.at) is the entry & results system of the
Austrian Orienteering Federation (ÖFOL). Its public API is documented at
<https://anne-api.oefol.at/v1/docs/>. CI requests are made politely (low
concurrency, identifying User-Agent) through the authenticated gateway, and
raw responses are cached in git so sources are only hit for new or changed
events.

## Usage

```
python3 ingest/anne_sync.py            # sync events + structured results
python3 ingest/parse_sportsoftware_html.py  # parse tier-2 HTML attachments
python3 build/build_db.py              # build site/data/results.db (pass 1)
python3 ingest/anne_user_eligibility.py     # sync ÖM/ÖSTM championship eligibility (needs ANNE_API_KEY)
python3 build/build_db.py              # rebuild with any newly-decided eligibility (pass 2)
python3 site/serve.py                  # local preview + writable result review
```

Open the review URL printed by the server (normally
`http://127.0.0.1:8643/review.html`) for the optimized list-by-list verification
workflow. If port 8643 is already in use, the server automatically selects the
next free port. See [docs/verification.md](docs/verification.md) for the review
dimensions, Family/OOC model and championship campaigns.

Direct local access to ANNE remains supported. CI instead sets
`ANNE_BASE_URL=https://<worker>/v1` and `ANNE_GATEWAY_TOKEN`.

## One-time Cloudflare/GitHub setup

The gateway uses the Cloudflare Workers Free plan and a private R2 bucket.

```bash
cd cloudflare/anne-gateway
npm install
npx wrangler login
npx wrangler r2 bucket create olresults-private
npm run deploy
npx wrangler secret put SYNC_GATEWAY_TOKEN
npx wrangler secret put ANNE_API_KEY
npx wrangler versions deploy  # activate the latest Secret Change version
```

Configure the repository after deployment:

- GitHub Actions variable `ANNE_GATEWAY_URL`: the Worker URL without `/v1`
- GitHub Actions secret `ANNE_GATEWAY_TOKEN`: the same random gateway token

Migrate the existing private eligibility ledger exactly once from a trusted
machine:

```bash
OLRESULTS_GATEWAY_URL=https://<worker> \
ANNE_GATEWAY_TOKEN=<token> \
python3 ingest/eligibility_state.py push
```

Both workflows intentionally fail if the remote ledger cannot be restored;
silently building without it would change historical ÖM/ÖSTM medal decisions.
Normal state uploads are monotonic: the gateway rejects a lower person or
decision count, stores the previous object under `eligibility/history/`, and
records SHA-256/count metadata. Recovery commands are:

```bash
python3 ingest/eligibility_state.py history
python3 ingest/eligibility_state.py restore eligibility/history/<version>.json
python3 ingest/eligibility_state.py pull --required
```

The scheduled workflow also treats parser failures as fatal and validates
SQLite integrity, foreign keys, provenance coverage and count regressions
against `data/build_health.json` before it commits or deploys anything.
Permanently unavailable historical links are listed explicitly in
`data/source_failure_allowlist.json`; they remain visible as `EXPECTED
UNAVAILABLE`, while every unrecognized failure still aborts the run.

## Identity and provenance

ANNE/ÖFOL identifiers, book-of-record verification, legacy aliases and raw
source observations are separate concepts in the generated database. Every
result points to a hashed source document; legacy-only people use stable,
deterministic ids and old runner links are retained through redirects. See
[`docs/identity-provenance.md`](docs/identity-provenance.md) for the table and
trust semantics.

### Championship (ÖM/ÖSTM) eligibility

`ingest/anne_user_eligibility.py` calls the gateway's minimal eligibility
endpoint (which calls ANNE's authenticated `/v1/user/:id` internally) to fetch
the `championshipEligibility` flag for every runner on
record with a non-Austrian nationality - the field ÖFOL itself maintains to
mark someone eligible for the Austrian championship despite a foreign
passport nationality (dual citizenship, long-tenured club membership,
etc.), which is a more reliable signal than nationality alone (see
`build/build_db.py`'s `apply_championship_eligibility_overrides`). Requires
an `ANNE_GATEWAY_TOKEN` in CI; direct local calls can still use an
`ANNE_API_KEY` from an ANNE account with at least clubManager role.

It needs to run **between two builds**: it finds candidates (runners with a
championship tag and a non-Austrian nationality) by querying the database
`build_db.py` just produced, since only that build has resolved which
person a given legacy (non-API) result actually belongs to. The second
build then applies whatever it just decided.

Each `(person, event)` pair, once decided, is cached permanently in
`data/raw/anne/user_eligibility.json` and never re-checked automatically -
eligibility isn't a permanent attribute of a person, so blindly re-deriving
it from today's status on every run would retroactively rewrite medals from
events where it was true at the time but no longer is (or vice versa). A
newly-discovered event for an already-known person gets its own,
independently-locked check. This cache is deliberately **not committed to
git** (it's per-person data obtained via elevated access - see `.gitignore`);
in CI it persists as a private R2 object behind the authenticated gateway. A
missing remote state is a hard CI error rather than a silent build with
incorrect medal decisions.
