# Offers — MAIL-3 Architecture & Operator Guide

When the mail watcher (MAIL-1) detects a job transitioning to `outcome=offer`,
the offers subsystem renders a one-page PDF summary, posts a Discord
notification, and writes a row to the dedicated **Offers** tab in the Job
Pipeline Google Sheet. The operator then replies in Discord to accept / decline
/ negotiate.

This card ships the **surface** — the trigger detector on top (MAIL-1) and the
Sheet schema extension (MAIL-2) are still in flight. Until they ship, the
subsystem is exercised via the CLI for replays.

## Module map

```
auto/offers/
├── __init__.py        — version + module doc
├── summary.py         — 1-page PDF renderer + Discord notification
├── decision.py        — Discord reply parser + outcome/colour mappings
├── sheet.py           — Google Sheet "Offers" tab writer
└── cli.py             — operator escape-hatch (replays)
templates/offer-summary.html — WeasyPrint template
```

All three modules are **best-effort / fail-open**: missing credentials,
missing WeasyPrint, or sheet-API errors return `None` / `False` and log a
warning. The mail watcher and Discord bot don't need to wrap calls in
`try/except`.

## Trigger flow (post-MAIL-1)

```python
from auto.offers import summary

# Called by MAIL-1 when it sees "outcome": "offer" on a row
summary.handle_offer_transition(
    job_id=48,
    job=job_dict,                         # full row + offer fields
    why_you="...",                        # matched from why-you-paragraphs.yaml
    sheet_url="https://docs.google.com/...",  # tab URL for the Discord message
)
```

Internally this:

1. Renders `data/offers/48-offer-summary.pdf` from `templates/offer-summary.html`
2. Posts the templated Discord notification
3. Returns the PDF path (or `None` if WeasyPrint isn't installed)

## Discord commands (operator replies)

| Reply | Sheet outcome | Notes column |
| --- | --- | --- |
| `accept 48` | `accepted` | _preserved_ |
| `decline 48` | `declined` | _preserved_ |
| `negotiate 48 ask for 110k base` | `offer` (unchanged) | "ask for 110k base" |
| `offer info 48` | `offer` (unchanged) | _preserved_ (bot DMs the PDF) |
| anything else | — | bot replies "didn't understand" |

Parsing is case-insensitive and whitespace-tolerant. Job IDs must be numeric
(the sheet uses 1-based row indices — the bot maps Discord `id` → sheet row
internally). Multi-line commands use the **first** non-empty line.

## Sheet contract — Offers tab

Headers (column order matters):

```
id | company | role | base_salary | bonus | equity | total_comp |
location | remote_days | start_date | decision | notes
```

`decision` is one of:

- ⭐ accept
- 🟡 negotiate
- 🔴 decline
- ❓ pending (initial state on offer detection)

Auto-coloring per decision (sets the row's background):

| Decision | RGB | Visual |
| --- | --- | --- |
| accept | 0.78 / 0.95 / 0.78 | green |
| negotiate | 1.0 / 0.95 / 0.78 | yellow |
| decline | 1.0 / 0.82 / 0.85 | pink |
| pending | 1.0 / 1.0 / 1.0 | white |

## Dependencies

The offer subsystem adds two new requirements on top of the Phase A-D base:

- `weasyprint>=60.0` — PDF rendering (requires `libgobject`, `libpango`, `libcairo`)
- `gspread>=6.0` + `google-auth>=2.0` — Sheet writes (optional at test-time;
  `auto.offers.sheet._ensure_tab` lazy-imports gspread so the unit tests
  run cleanly without the dependency installed — only the live Sheet
  write path actually needs it)

**macOS install:**

```bash
brew install glib cairo pango gdk-pixbuf libffi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib   # WeasyPrint system libs
```

Without `DYLD_FALLBACK_LIBRARY_PATH` set, WeasyPrint imports fail with
`libgobject-2.0-0 not found`. WeasyPrint is imported lazily
(`auto.offers.summary._weasyprint_write_pdf`) so the rest of the codebase can
run without system libs installed — the offers PDF is the only thing that
needs them.

## CLI replays

The CLI is the operator escape-hatch when the Discord bot is down:

```bash
# Render the PDF on its own
python -m auto.offers.cli generate \
    --json '{"id": 48, "company": "BrandForward", "title": "Head of Digital", ...}' \
    --why-you "..." \
    --decision pending

# Test the reply parser
echo "negotiate 48 ask for 110k" | python -m auto.offers.cli parse

# Append / update the Sheet (when gspread is installed + creds configured)
export GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/creds.json
python -m auto.offers.cli append --sheet-id "$CV_SHEET_ID" \
    --json '{"id": 48, "company": "BrandForward", ...}'

python -m auto.offers.cli update --sheet-id "$CV_SHEET_ID" \
    --job-id 48 --decision accept --notes "signed offer letter"
```

## Tests

```bash
cd ~/Documents/Projects/CV
source .venv/bin/activate
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
python -m pytest tests/test_offer_summary.py \
               tests/test_decision_parser.py \
               tests/test_offer_sheet.py -v
```

52 tests cover:

- HTML rendering (all facts present, empty fields skipped, html-escape safety)
- PDF rendering (WeasyPrint mocked, output naming, slug fallback)
- Discord message format (present + missing values, sheet URL included)
- `handle_offer_transition` fail-open behaviour
- All four command patterns + case + whitespace + edge cases
- Outcome mapping (DecisionKind → sheet Outcome)
- Color mapping (every DecisionKind has a color)
- Sheet row validator + length invariants
- Mocked sheet writes for `append_offer` + `update_decision` (every error path)

## Future work (out of scope here)

- **MAIL-1 detection** — wires `handle_offer_transition` into the mail
  watcher's `outcome=offer` handler.
- **MAIL-2 schema** — when the Sheet has an `outcome` column already, surface
  the offer offers on the daily tab to the operator.
- **Discord bot** — the bot lives outside this repo (Hermes / dedicated
  daemon) and consumes `parse_reply` + `update_decision` via the
  `auto.offers.decision` / `auto.offers.sheet` public APIs.
- **Offer email field extraction** — currently the job dict must include the
  offer fields (`base_salary`, `equity`, etc.) populated by the mail parser.
  A future card can extend the mail parser to extract these from the email
  body automatically.
