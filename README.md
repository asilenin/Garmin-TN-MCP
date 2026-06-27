# Garmin-TN-MCP (`garmin-raw`)

**English** | [Русский](README.ru.md)

Minimal **raw** access to Garmin Connect for training analysis. One backend
(`garminconnect` 0.3.x) powers two frontends:

- **MCP server** (`garmin-raw-mcp`) — live data access inside Claude Desktop chat.
- **One-shot export** (`garmin-raw-export`) — dumps a date range to JSON for
  reuse with other athletes (the file is uploaded into a chat).

Principle: raw data only (per-lap HR / cadence / power / stride / elevation,
per-second streams, the lactate comment). **No** VO2max / training-effect /
device threshold estimates — those are deliberately excluded.

## Install

Requires Python 3.10+ and [`uv`](https://astral.sh/uv).

```bash
git clone https://github.com/asilenin/Garmin-TN-MCP.git
cd Garmin-TN-MCP
uv sync
```

### 1. Authenticate (once)

```bash
uv run garmin-raw-auth
```

Enter email, password and the MFA code. Tokens are saved to `~/.garminconnect`
(0.3.x format). After that no login is needed — the server and the export run on
tokens (and avoid 429 rate-limiting from repeated logins).

### 2. Connect the MCP to Claude Desktop — one command

```bash
uv run garmin-raw-install
```

It auto-resolves `uv` and this folder's path and **safely merges** the
`garmin-raw` server into `claude_desktop_config.json` without overwriting the
rest (your preferences, etc.), with a backup. Cross-platform (macOS/Windows/Linux).
You can pass the path explicitly:

```bash
uv run garmin-raw-install /full/path/to/Garmin-TN-MCP
```

Then **fully restart Claude Desktop** (Cmd+Q on macOS) — the 6 tools appear.

<details>
<summary>Manual alternative (if you prefer not to use the script)</summary>

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "garmin-raw": {
      "command": "/Users/<you>/.local/bin/uv",
      "args": ["--directory", "/full/path/to/Garmin-TN-MCP", "run", "garmin-raw-mcp"]
    }
  }
}
```
</details>

## Uninstall

```bash
uv run garmin-raw-uninstall   # removes garmin-raw from the config (with backup), leaves the rest
```

Restart Claude Desktop. Tokens are not removed — delete them manually if you want:

```bash
rm -rf ~/.garminconnect        # saved Garmin tokens
```

Remove the whole thing:

```bash
cd .. && rm -rf Garmin-TN-MCP  # the repository itself
```

## Export (for reuse with other athletes)

```bash
# whole period
uv run garmin-raw-export --start 2026-06-01 --end 2026-06-21

# single activity + per-second streams
uv run garmin-raw-export --start 2026-06-20 --end 2026-06-21 \
    --activity 23321211303 --streams
```

Produces `garmin_export.json` — upload it into a chat.

## Tools (identical in the MCP and the export)

| Tool | Returns |
|---|---|
| `list_activities(start, end, sport)` | raw activity summaries for a period (1 request) |
| `get_activity_laps(id)` | HR / cadence / power / stride / elevation **per lap** (lapDTOs) |
| `get_activity_streams(id)` | per-second streams (HR, cadence, elevation, grade, power, stride, respiration) |
| `get_activity_comment(id)` | the activity comment + parsed lactate (`LA:x.x`) |
| `get_activity_lactate(id)` | numeric lactate marks from the TN Splits View developer field: `[(time, mmol, lap)]` |
| `get_wellness(date)` | sleep, HRV, RHR, stress, Body Battery |
| `get_personal_records()` | personal records by distance |

## Lactate

Write it into the **activity comment** in Garmin Connect as `LA:6.1` (context is
fine: `LA:6.6 @rep12`). `get_activity_comment` reads the `description` field and
parses every value into `lactate_mmol`. The comment is fetched lazily — only for
activities actually under analysis — to avoid doubling the request count.

Numeric lactate (new): values logged from the watch via the **TN Splits View** ConnectIQ
field live in the activity **streams**, not the comment. `get_activity_lactate` reads them —
the field is matched by `developerFieldNumber == 1` (the per-stream index is not stable, and
the appID is zeroed on sideload / becomes `7c294f6c-…` after publishing). Any sample > 0 is a
mark; 0 means no measurement. Each mark is attributed to the nearest lap.

## Notes

- **Garmin PRs are auto-detected fastest splits**, not certified race times; they
  can be faster than official results. Use certified times as form markers and
  Garmin PRs only as a hint.
- **Wellness/PR** methods are resolved by trying candidate names: if your
  `garminconnect` version renames one, the tool returns `_error` instead of
  crashing the whole response.
- **PII** (owner name/ID) is stripped from outputs, case-insensitively — hygiene
  for shared exports.
- If the MCP silently stops responding, it's almost always stale tokens: re-run
  `garmin-raw-auth`. The one-shot export is the robust fallback.

## Garmin disclaimer

This project accesses Garmin Connect through an **unofficial** method (the
community [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect)
library, which logs in with your own credentials). It is **not affiliated with,
endorsed by, or supported by Garmin**. Your use may be subject to Garmin's Terms
of Service; you use it **at your own risk**. No warranty is provided (see
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
