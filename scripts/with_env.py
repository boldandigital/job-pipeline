#!/usr/bin/env python3
"""Tiny helper: load .env with dotenv, then exec the rest of argv as a script.
Usage:  python scripts/with_env.py <module> [args...]
        python scripts/with_env.py src/mail/watcher.py --dry-run --once --folder INBOX
"""
import os
import sys
import runpy
from pathlib import Path
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    for k, v in dotenv_values(env_path).items():
        if v is not None:
            os.environ[k] = v

sys.path.insert(0, str(PROJECT_ROOT))

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")