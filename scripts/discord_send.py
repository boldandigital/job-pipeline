#!/usr/bin/env python3
"""Send a Discord message via curl (urllib hits Discord 1010 from this bot
because of subtle header ordering). Uses subprocess + curl.
Usage: python scripts/discord_send.py "message"
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from dotenv import dotenv_values
for k, v in dotenv_values(PROJECT_ROOT / ".env").items():
    if v:
        os.environ[k] = v

msg = sys.argv[1] if len(sys.argv) > 1 else "🏴‍☠️ ping"
result = subprocess.run(
    [
        "curl",
        "-s",
        "-X", "POST",
        f"https://discord.com/api/v10/channels/{os.environ['DISCORD_HOME_CHANNEL']}/messages",
        "-H", f"Authorization: Bot {os.environ['DISCORD_BOT_TOKEN']}",
        "-H", "Content-Type: application/json",
        "-d", __import__("json").dumps({"content": msg}),
        "-w", "\nHTTP %{http_code}\n",
    ],
    capture_output=True,
    text=True,
)
print(result.stdout)
print(result.stderr)
sys.exit(0 if "HTTP 200" in result.stdout or "HTTP 204" in result.stdout else 1)