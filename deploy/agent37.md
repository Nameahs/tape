# Deploying Tape on Agent37

Tape runs inside one Agent37 workspace. Python does the fetching and scoring; the hosted agent
only wakes up to write briefs. These steps assume the Basic plan or higher (needed for Telegram).

## 1. Workspace

In the workspace web terminal:

```bash
git clone https://github.com/Nameahs/tape.git && cd tape
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[prices]"
tape init
```

Add these as workspace secrets or to `~/.profile`:

| Variable | Value |
| --- | --- |
| `TYPESAFE_API_KEY` | from the TypeSafe console |
| `TAPE_USER_AGENT` | `Tape <your email>` (the SEC requires a contact) |
| `TAPE_DB_PATH` | `/workspace/tape/data/tape.db` (keep it on persistent storage) |

Smoke test: `tape run -v` should print a summary with `scored=` greater than zero during market hours.

## 2. Brief skill

Copy `skills/tape-brief.md` into the agent's skills folder so it can be invoked by name.

## 3. Telegram

Connect Telegram in the workspace's messaging settings so the agent can message you directly.
If you would rather send the digest from a script, create a bot with @BotFather, set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, and use `tape digest --send`.

## 4. Schedules (all times ET)

| Schedule | Task given to the agent |
| --- | --- |
| Every 5 min, 7:00 am to 8:00 pm, Mon to Fri | "Use the tape-brief skill." |
| 6:00 pm, Mon to Fri | "Use the tape-brief skill, including the evening digest." |
| 9:00 pm, Mon to Fri | Run `tape evaluate && tape dashboard` (no briefs) |

Most 5-minute runs find nothing pending and end after two commands, so they cost almost no
agent credit. Jev scoring costs cents per day.

## 5. Backfill (once)

```bash
tape backfill --days 90 -v
tape evaluate
tape dashboard
```

Then read `out/eval.json` and tune thresholds with the `TAPE_*` variables. Apply new thresholds
to stored scores with `tape reroute` instead of re-scoring.

## Open item

Agent37's developer API isn't documented publicly in detail. This plan uses only what the product
page lists (scheduled jobs, web terminal, messaging). Confirm scheduled tasks can run shell
commands before relying on the 5-minute cadence; see the issue tracking this.
