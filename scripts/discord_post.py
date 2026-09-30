#!/usr/bin/env python3
"""Post a Discord embed via the bot API."""
import os
import sys
import json
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for k, v in dotenv_values(PROJECT_ROOT / ".env").items():
    if v:
        os.environ[k] = v

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL = os.environ["DISCORD_HOME_CHANNEL"]


def post(content: str, embeds: list | None = None) -> int:
    body = {"content": content}
    if embeds:
        body["embeds"] = embeds
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL}/messages",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return r.status


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "🏴‍☠️ ping"
    print(f"HTTP {post(msg)}")