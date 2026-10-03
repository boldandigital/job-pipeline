# Job Approval UI — Design Handoff

## Status: v2 ready for review

**OD projects created:**
- **v1** (proof of concept): `routine-f648b8ca-3b75-435a-a255-ee988b8295df`
  - Files: `~/Library/Application Support/Open Design/namespaces/release-stable/data/projects/routine-f648b8ca-3b75-435a-a255-ee988b8295df/opendesign/mockups/job-approval-ui/`
- **v2** (final design, real data + detail view): `routine-b9a4f5a0-b42b-435b-8cdf-1c8f3151bb5f`
  - Files: `~/Library/Application Support/Open Design/namespaces/release-stable/data/projects/routine-b9a4f5a0-b42b-435b-8cdf-1c8f3151bb5f/opendesign/mockups/job-approval-ui/`

## What v2 has (final design)

### Layout
- **Top bar (sticky)**: Captain's Bridge logo, 4 stat chips (today/auto-fit/approved/skipped), search, Settings, LZ avatar
- **Hero**: "9 new roles, 3 clear fits — your move, Captain." with weather/timestamp
- **Anchor · top 3 + 6 ghosted**: 9-card grid (3 real jobs at top, 6 ghosted below)
- **Sidebar (right)**: Captain profile (Lars Zimmermann, 12+ yrs, DE/EN C2, Online · Brussels), Today's catch, This week, Pipeline state timeline (5 steps), Sources breakdown
- **Pipeline status ticker** at bottom: "Pipeline status: idle - waiting on Captain's decision - next auto-fit sweep in 14 min"

### Job card
- Score circle (94 / 88 / 82 with gradient arc)
- Title + company + location
- Salary band (€60-75k, €100-130k OTE, €60-75k + 1.0%)
- Source tag (XING) + match tag (C2 + 12y fit, AI · Series B, Founder track)
- 1-line teaser in DE
- [Open ↗] [★ Approve] [✗ Skip] buttons
- When approved: button turns green, card moves to bottom with green border + "Approved HH:MM" label
- When skipped: card fades to 30% opacity with red border + "✗ Skipped"

### Detail view (click any card)
- **Left half**: Full job description (200-2000 chars), "Why this fits Lars" 4-bullet list at top
- **Right half**: 2 stacked PDF preview embeds (CV on top, Anschreiben below) with "View full" links
- **Sticky footer**: 3 buttons [Skip] [Open in XING] [Approve & queue submit]
- Click another card → first auto-collapses (only one open at a time)

### Stats
- Hero updates count when cards approved/skipped
- Sidebar chips update in real-time
- Score circles have gradient arc (cyan to darker cyan)

## Brand
- BOLD = dark navy (#0a0e1a) background, cyan (#06b6d4) accent, white cards
- Inter font, JetBrains Mono for numbers
- ⚓ + 🏴‍☠️ markers sparingly (only in Captain's Bridge logo, log ticker, anchor section title)
- No emoji-heavy UI

## What works / what's mock

| Works | Mock |
|---|---|
| Grid expansion ✅ | Real data wired ✅ |
| Approve/Skip animations ✅ | Approve POST to backend (mock — would need FastAPI) |
| PDF previews ✅ | PDFs are iframe placeholders, not real CV/letter |
| Captain's Log ticker ✅ | Updates via JS interval, not real pipeline events |
| Sidebar stats ✅ | Static numbers, not real DB queries |
| Search bar ✅ | No filter logic |
| Settings button ✅ | No settings panel |

## Next step (Phase 2)

Build the FastAPI backend that:
1. Reads from `data/jobs.db` (SQLite, already exists)
2. Serves `index.html` + `index.css` from v2 design
3. Wires real PDF paths from `data/batches/2026-10-02/<Company>/` into iframe src
4. Approve button POSTs to `/api/jobs/{id}/approve` → updates `status='approved'` in DB
5. Skip button POSTs to `/api/jobs/{id}/skip` → updates `status='skipped'`
6. Discord webhook fires on each approve
7. Auth: simple email + password (single user, no session expiry)

Estimated: 1-2 hours for a working v3 backend.
