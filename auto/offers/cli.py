"""
auto/offers/cli.py — Minimal CLI for the offers subsystem.

Subcommands (all best-effort, all mockable for tests):

  generate   — render the PDF for one offer JSON on stdin / --json
  parse      — read a Discord reply on stdin / --reply, print the Decision
  append     — append an offer row to the Sheet (needs credentials)
  update     — update an offer row by id + decision

This is the operator's escape-hatch when something in the Discord bot
chain isn't working — they can replay the state changes here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from auto.offers.summary import handle_offer_transition, render_pdf
from auto.offers.decision import parse_reply, DecisionKind
from auto.offers.sheet import append_offer, update_decision

log = logging.getLogger("auto.offers.cli")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="auto.offers")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Render the offer PDF for a job")
    g.add_argument("--json", default="", help="Job dict as JSON")
    g.add_argument("--why-you", default="", help="Why-firm snippet")
    g.add_argument("--decision", default="pending", help="accept/negotiate/decline/pending")
    g.add_argument("--out-dir", type=Path, default=None, help="Output directory")

    pa = sub.add_parser("parse", help="Parse a Discord reply line")
    pa.add_argument("--reply", default="", help="Reply text (else reads stdin)")

    ap = sub.add_parser("append", help="Append an offer row to the Offers tab")
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--json", default="", help="Offer dict as JSON")
    ap.add_argument("--tab", default="Offers")

    up = sub.add_parser("update", help="Update an offer row by id")
    up.add_argument("--sheet-id", required=True)
    up.add_argument("--job-id", required=True, help="Numeric or string id")
    up.add_argument("--decision", required=True,
                    choices=["accept", "decline", "negotiate"])
    up.add_argument("--notes", default="")
    up.add_argument("--tab", default="Offers")

    return p


def _cmd_generate(args: argparse.Namespace) -> int:
    job: dict[str, Any] = json.loads(args.json) if args.json else {}
    pdf = render_pdf(
        job, why_you=args.why_you, out_dir=args.out_dir,
        decision=args.decision,
    )
    print(f"OK {pdf}")
    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    text = args.reply if args.reply else sys.stdin.read()
    cmd = parse_reply(text)
    out = {
        "kind": cmd.kind.value,
        "job_id": cmd.job_id,
        "notes": cmd.notes,
        "actionable": cmd.is_actionable,
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_append(args: argparse.Namespace) -> int:
    offer: dict[str, Any] = json.loads(args.json) if args.json else {}
    url = append_offer(args.sheet_id, offer, tab_name=args.tab)
    if url:
        print(f"OK {url}")
        return 0
    print("FAIL (no credentials / gspread / sheet — nothing written)")
    return 1


def _cmd_update(args: argparse.Namespace) -> int:
    ok = update_decision(
        args.sheet_id, args.job_id, args.decision, args.notes, tab_name=args.tab,
    )
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.cmd == "generate":
        return _cmd_generate(args)
    if args.cmd == "parse":
        return _cmd_parse(args)
    if args.cmd == "append":
        return _cmd_append(args)
    if args.cmd == "update":
        return _cmd_update(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
