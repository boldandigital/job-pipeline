# SECRETS — Cross-project credential sharing

How job-pipeline reads Discord credentials from `~/.hermes/.env` instead of duplicating them in its own `.env`. ADOPT-8.

## The problem

Job-pipeline needs to post daily batches to Discord. Discord authentication is a bot token (`DISCORD_BOT_TOKEN`) plus a channel ID (`DISCORD_CHANNEL_ID`). These are *real secrets* — the token is the only thing standing between an attacker and a Discord-spamming endpoint.

Before ADOPT-8, we used a separate Discord webhook (`DISCORD_WEBHOOK_URL`), which sidestepped the bot-token problem by trading flexibility for a per-channel URL. ADOPT-7 made that the default. ADOPT-8 reverses course because:

1. We **already** have a Discord bot configured in `~/.hermes/.env` (`DISCORD_BOT_TOKEN`).
2. The Hermes bot is **already** in the home channel (`DISCORD_HOME_CHANNEL=1497988205788663991`).
3. Adding a parallel webhook = duplicate auth surface = two secrets to rotate on every incident.

## The pattern: shared `.env` with project-local override

```
~/.hermes/.env           ← owned by Hermes. Authoritative source for shared Discord creds.
                            NEVER edit from job-pipeline.
~/Documents/Projects/job-pipeline/.env   ← owned by job-pipeline. May OVERRIDE Hermes
                                            values, but does NOT need to duplicate them.
```

Resolution order in `scripts/lars-daily-run.sh`:

1. Source `job-pipeline/.env` first — this wins for any var it sets.
2. For each `DISCORD_*` var that is STILL empty after step 1, read from `~/.hermes/.env`.
3. `DISCORD_CHANNEL_ID` defaults to `DISCORD_HOME_CHANNEL` if neither file set it.

The result: in the happy path, job-pipeline's `.env` only needs `ANTHROPIC_API_KEY` (and an optional `DISCORD_CHANNEL_ID` override). The Discord bot token rides along from Hermes.

## What goes in each file

### `~/.hermes/.env` (Hermes-owned — DON'T edit from job-pipeline)

```env
DISCORD_BOT_TOKEN=<bot-…>
DISCORD_HOME_CHANNEL=1497988205788663991
DISCORD_HOME_CHANNEL_NAME=<channel name>
DISCORD_ALLOWED_USERS=<comma-separated user IDs>
```

### `/Users/lars/Documents/Projects/job-pipeline/.env` (job-pipeline-owned)

```env
# Required
ANTHROPIC_API_KEY=sk-ant-…

# Optional override — pin job-pipeline to a dedicated #job-pipeline channel.
# If left blank, DISCORD_HOME_CHANNEL is used.
# DISCORD_CHANNEL_ID=1234567890123456789

# Optional display-name override
# DISCORD_USERNAME=Lars Job Pipeline
```

That's it. No `DISCORD_BOT_TOKEN` line. The wrapper inherits it from Hermes.

## Override the fallback path

By default the wrapper reads `$HOME/.hermes/.env`. Override with the `HERMES_ENV` env var:

```bash
# In job-pipeline/.env
HERMES_ENV=/Users/lars/.hermes/.env.production
```

Or inline at the command line:

```bash
HERMES_ENV=/tmp/test.env bash scripts/lars-daily-run.sh --test
```

## Rotating shared secrets

Because `DISCORD_BOT_TOKEN` is shared:

- Rotating it in `~/.hermes/.env` immediately affects Hermes AND job-pipeline.
- If you need to rotate ONLY for job-pipeline (rare), set `DISCORD_BOT_TOKEN` in `job-pipeline/.env` to a separate bot. That bot will need to be invited to your Discord server.
- See `docs/DAILY-RUN.md` §7 for the rotation runbook.

## What about `daily_pipeline.sh` (the dome317 Docker path)?

The dome317 stock pipeline at `scripts/daily_pipeline.sh` and `scripts/cron-setup.sh` is used by the upstream Docker deployment (target `/opt/job-pipeline`). It still uses `DISCORD_WEBHOOK_URL` — ADOPT-7 added it, ADOPT-8 keeps the webhook fallback for that environment because the Docker container does not have access to `~/.hermes/.env` from the host.

If you ever want to migrate the Docker path to the bot API:

1. Mount the host's `~/.hermes/` into the container (read-only).
2. Set `HERMES_ENV=/run/secrets/hermes.env` in the container.
3. The same fallback logic in the wrapper will pick it up.

## Why not a more "professional" secret store?

- macOS Keychain access from cron-launched bash scripts is non-trivial (interactive prompts, security framework wrapping).
- 1Password CLI would work, but adds a runtime dependency for a script that should fire at 09:00 with no human in the loop.
- `direnv` / `doppler` / `vault` are overkill for a one-person setup with ~5 secrets.
- A flat `~/.hermes/.env` with `chmod 600` is good enough for the current scale and matches how every other project on this Mac already handles shared creds. Revisit if a third project joins the Discord party.

## Checklist for a new shared secret

- [ ] Add it to `~/.hermes/.env` first.
- [ ] Document the var name in this file under "What goes in each file".
- [ ] Add the var to the `load_hermes_discord_fallback` loop in `lars-daily-run.sh` (or extend with a second fallback function for non-Discord vars).
- [ ] Mention the new var in `.env.example` as "inherited from `~/.hermes/.env`".
- [ ] If rotation matters, write a rotation runbook in `docs/DAILY-RUN.md` §7.