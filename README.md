# TN Garmin MCP

**English** | [Русский](README.ru.md)

An MCP server for training analysis on top of Garmin Connect: not "another
VO2max dashboard", but **enriched, per-profile access to your own data right
inside a Claude chat** — HR/pace distribution shapes, decoupling, pace-bucketed
biomechanics, lactate marks anchored to the exact second they were taken,
cross-period aggregates. Methodologically rejects device-side estimates
(VO2max / training-effect / out-of-the-box thresholds) — only measurable facts
and derivations you can trace, no hidden models.

Two layers, both served over MCP:

- **`garmin-tn` (profile-aware, recommended)** — enriched analysis plus your
  own lactate marks and notes. **One connector = one Garmin account/profile.**
  Multiple people/accounts → multiple connectors; data never crosses between
  them.
- **`garmin-raw` (raw, optional)** — unprocessed HR/cadence/power/stride/
  elevation per lap and per second, comment-based lactate. Single account per
  server, no profiles. Useful if you want raw data directly or a JSON export
  for external analysis.

If unsure what you need, install `garmin-tn` — it's the primary path.

## Requirements

Python 3.10+ and [`uv`](https://astral.sh/uv). Claude Desktop for MCP access
(connectors run over local stdio — no server or hosting needed).

## Fresh install

```bash
git clone https://github.com/asilenin/Garmin-TN-MCP.git
cd Garmin-TN-MCP
uv sync
```

### 1. Authenticate with Garmin (once per account)

```bash
uv run garmin-raw-auth
```

Email, password, MFA code. Tokens land in `~/.garminconnect` — this is the
**default account** (see "Multiple profiles" below if you have more than one
Garmin account, e.g. yourself + a training partner).

### 2. Prime the local cache

The profile layer keeps its own local database
(`~/.garmin-tn/profiles/<slug>/cache.db`) — MCP tools read from it and never
touch the network (see "How this works" below). Fill it once before first use:

```bash
uv run python garmin_raw/sync.py <slug>              # activity catalog (all dates)
uv run python garmin_raw/sync.py <slug> enrich 50     # enrichment + streams, newest first
uv run python garmin_raw/sync.py <slug> fetch-aux     # laps + comments (lactate)
```

`<slug>` is the connection key `<provider>-<user>` (e.g. `garmin-anton`,
`garmin-mila`; letters/digits/`-`). With years
of history, run `enrich` in batches (`enrich 100`, then again) — it skips
what's already enriched. **On a large archive, first enrichment can take over
an hour** — expected, Garmin has no bulk endpoint for per-second streams.

Check what's accumulated:

```bash
uv run python garmin_raw/sync.py <slug> status
```

### 3. Connect to Claude Desktop

```bash
uv run tn-install <provider> <user>
```

A **connection** is `(provider, user)` — e.g. `garmin anton`. Appends a
`tn-<provider>-<user>` connector (e.g. `tn-garmin-anton`) to
`claude_desktop_config.json` without touching anything else (backs up before
writing), with env `TN_USER`/`TN_PROVIDER`. Repo path is auto-detected; pass it
explicitly if needed: `uv run tn-install <provider> <user> /full/path/to/repo`.

Restart Claude Desktop (Cmd+Q on macOS — not just closing the window) — 15
`garmin_*` tools will appear.

## Multiple profiles (different Garmin accounts)

Example: your own account plus a training partner's.

```bash
# for the account authenticated via garmin-raw-auth above — reuse its tokens:
uv run tn-install garmin anton --tokenstore ~/.garminconnect

# for a second account — authenticate separately into its own token folder
# (connection slug = <provider>-<user>, e.g. garmin-mila):
mkdir -p ~/.garmin-tn/profiles/garmin-mila/tokens
GARMIN_TOKENSTORE=~/.garmin-tn/profiles/garmin-mila/tokens uv run garmin-raw-auth
uv run python garmin_raw/sync.py garmin-mila
uv run python garmin_raw/sync.py garmin-mila enrich 50
uv run python garmin_raw/sync.py garmin-mila fetch-aux
uv run tn-install garmin mila
```

`--tokenstore` is needed **only if** the profile has no token folder of its
own — it's an explicit flag, not a hidden default: install warns at setup time
if no tokens were found anywhere. A profile with its own tokens
(`~/.garmin-tn/profiles/<slug>/tokens/`) keeps using them and never mixes up
with another account.

With several profiles installed, Claude sees all of them at once —
`garmin_compact`, `garmin_add_lactate`, etc. operate in the context of
whichever connector you're using; profiles never cross (the profile is
deliberately absent from tool names — it's determined by which connector you
use, not by anything you type in chat).

## Updating the code

```bash
cd Garmin-TN-MCP
git pull
uv sync
```

No need to touch Claude's config — the connector only stores the repo path,
new code is picked up on Claude Desktop's next launch (restart it). If the
local database schema changed, migration runs automatically on first access,
additively (existing data is preserved).

## Syncing — keeping the cache fresh

MCP tools **never touch the network** — they only read the local cache (fast,
predictable, no network failures mid-conversation). New activities need to be
pulled in **explicitly**:

```bash
uv run python garmin_raw/sync.py <slug>              # new activities into the catalog
uv run python garmin_raw/sync.py <slug> enrich 20     # enrich the newest 20
uv run python garmin_raw/sync.py <slug> fetch-aux     # laps/comments for what's missing
```

Run this before discussing recent activities (or on a schedule). `sync.py
<slug> recompute` recomputes all metrics from already-downloaded raw data (run
after a code update if the computation logic changed; no network needed).

> Triggering a sync from inside the Claude chat itself ("download today's
> run") is planned — see the backlog. For now, syncing is a separate terminal
> step.

## Uninstalling

```bash
uv run tn-uninstall <provider> <user>    # remove one connection connector
uv run garmin-raw-uninstall              # remove the raw connector (if installed)
```

Both only edit Claude's config (with a backup) — code and data are untouched.
Restart Claude Desktop afterward.

Full cleanup:

```bash
rm -rf ~/.garmin-tn                # local databases for all profiles
rm -rf ~/.garminconnect            # default account's tokens
cd .. && rm -rf Garmin-TN-MCP      # the repository itself
```

## Tools (`garmin-tn`, profile-aware)

| Tool | Returns |
|---|---|
| `garmin_status()` | what's in the cache: date range, activity count, last sync time |
| `garmin_query(limit, order, filters)` | activity catalog by filter (no histograms) |
| `garmin_compact(activity_id)` | HR/pace shapes, clusters, decoupling, biomechanics, lactate marks |
| `garmin_full(activity_id)` | the entire enrichment |
| `garmin_aggregates(period_key?)` | cross-period aggregates (form over time) |
| `garmin_add_lactate(activity_id, mmol, …)` | record a lactate reading anchored to a second (see below) |
| `garmin_add_note(activity_id, text)` | attach a free-text note to an activity |
| `garmin_delete_mark(mark_id)` | delete a mark/note |

## Lactate — anchored to the exact second

Three sources, of differing value:

1. **Garmin comment** (`LA:6.1`, optionally with context: `LA:6.6 @rep12`) —
   read automatically, but it's a **bare number**: no way to know the HR/pace
   it was taken at.
2. **ConnectIQ TN Splits View watch app** (numeric field in the stream) — also
   read automatically, has a lap anchor, but needs a third-party watch app.
3. **`garmin_add_lactate` — the recommended way.** Tell the chat when the
   reading was taken (elapsed time or lap number) — the tool resolves it to
   the exact second in the stream and returns the HR/pace at that moment. The
   difference matters: "5.1 mmol at HR 129 deep in recovery" and "5.1 mmol at
   HR 175 mid-effort" are different physiology behind the same number.

Three ways to specify the second (exactly one):

```
garmin_add_lactate(id, 5.5, at_elapsed_s=2190)     # 36:30 from start (as written in comments)
garmin_add_lactate(id, 5.5, at_ms=1782884498000)   # absolute timestamp (exact path)
garmin_add_lactate(id, 5.5, user_ref="lap14")      # end of Garmin lap 14
```

If the activity isn't cached yet, `sync` first. If the per-second stream
hasn't been downloaded yet, the mark is saved and resolved automatically on
the next `enrich`.

## How this works (short version)

MCP tools only read a local SQLite database
(`~/.garmin-tn/profiles/<slug>/cache.db`) — this gives fast, predictable
responses and keeps profiles isolated from each other: each connector is
hard-wired to its own file and its own tokens via environment variables in
Claude's config (not anything typed in chat). Filling the cache is a separate,
explicit operation (`sync.py`) — only it touches the network.

## Notes

- **Garmin PRs are an auto-detected fastest split**, not a race-protocol time;
  they can beat official results. For fitness markers, prefer protocol races.
- **PII** (owner name/ID) is stripped from the local cache and output,
  case-insensitively.
- If tools return empty/errors on a previously-synced profile, it's almost
  always expired Garmin tokens: `uv run garmin-raw-auth` (with
  `GARMIN_TOKENSTORE=<profile path>` for a non-default account), then `sync`.

## Raw layer (`garmin-raw`, optional)

A separate single-account MCP with no profiles and no enrichment — raw data
only. Useful for JSON export to external analysis, or if the profile layer
isn't needed.

```bash
uv run garmin-raw-install     # garmin-raw connector in Claude
uv run garmin-raw-export --start 2026-06-01 --end 2026-06-21   # or a JSON export
```

<details>
<summary>garmin-raw tools</summary>

| Tool | Returns |
|---|---|
| `list_activities(start, end, sport)` | raw summaries for a period |
| `get_activity_laps(id)` | HR/cadence/power/stride/elevation per lap |
| `get_activity_streams(id)` | per-second streams |
| `get_activity_comment(id)` | comment + parsed `LA:x.x` |
| `get_activity_lactate(id)` | numeric lactate from TN Splits View, per lap |
| `get_wellness(date)` | sleep, HRV, RHR, stress, Body Battery |
| `get_personal_records()` | PRs by distance |

</details>

## Garmin disclaimer

This project talks to Garmin Connect through an **unofficial** route (the
community library [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect),
which logs in with your own credentials). It is **not affiliated with, endorsed
by, or supported by Garmin**. Use may be subject to Garmin's Terms of Service;
you use it **at your own risk**. No warranties are made (see
[`LICENSE`](LICENSE)).

## Authorship & AI generation

This project was designed and written by **Claude** (Anthropic's AI assistant)
during an extended pair-programming session, under the direction, review and
testing of **Anton Silenin**. The methodology, architecture, debugging and final
verification against real Garmin data were done collaboratively in conversation:
the human author initiated the work, made the design decisions, validated every
step on live data, and is the copyright holder.

AI-generated output carries no separate human authorship under copyright law, so
it is released under the human author's name (MIT, see [`LICENSE`](LICENSE)). This
note is here for transparency, not as a license requirement. As with any
AI-assisted code, review it before relying on it.
