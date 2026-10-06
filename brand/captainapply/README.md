# CaptainApply — Logo Concept Brief (v2 with generated art)

## What we have now

12 image-generated logo concepts in `brand/captainapply/concepts/`. All generated via MiniMax text-to-image. None are final — they're concept directions. A human designer (or more refined generation) is needed for the production version.

## Final rankings (5 selected from 12)

| # | File | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|
| 1 | `concepts/01-sailboat-standalone-v1.png` | Two-tone sails (cyan + navy), waterline wake, classic sailboat | Sails look slightly stiff at small size | 🥇 **Memorable, best standalone** |
| 2 | `concepts/02-sailboat-clean-v4.png` | Single sail, simple hull, 3 cyan wake dots, classic | No two-tone, simpler | ⭐⭐⭐⭐ Best favicon |
| 3 | `concepts/03-lockup-clean-v3.png` | Cleanest lockup, single sail, "CaptainApply" wordmark, all-navy | No cyan accent in wordmark | ⭐⭐⭐⭐ Most B2B |
| 4 | `concepts/04-lockup-twotone-v2.png` | Two-tone text, classic boat, balanced | Has 2 sails (mainsail + jib) | ⭐⭐⭐ |
| 5 | `concepts/05-sailboat-gradient-v3.png` | Gradient sail, premium feel | Harder to reproduce in flat design | ⭐⭐⭐ |

## Reference we steered AWAY from

**Corsair Gaming** (corsair.com): three angular black sails, monochrome, aggressive.
Our single-sail approach = visually distinct.

## What we WANT to say with the mark

Brief from Lars: "A boat sailing forward into unknown waters."

Encoded values:
- **Forward motion** (not stationary, not docked)
- **Open horizon** (uncertainty = opportunity)
- **Single vessel** (you, the Captain, in charge)
- **Subtle nautical, not pirate pageant** (chart under)

## MY RECOMMENDATION: Hybrid

Take the **mark from #2** (clean single sail + 3 cyan wake dots)
+
the **two-tone wordmark from #4** ("captain" navy + "apply" cyan)
+
ship via a designer to:
- Tighten kerning
- Add a tiny cyan dot on the "i" of apply (the brand signature)
- Set proper baseline alignment between mark and text
- Export at 16px / 32px / 180px / 1024px sizes

## Color tokens (consistent with HostSalt palette)

```
--ink-navy:    #051E40   (sail, "captain" text)
--salt-cyan:   #29F2F2   (hull, wake dots, "apply" text)
--bone:        #FAFAFA   (background)
--charcoal:    #0F1115   (dark mode background)
```

## File deliverables to make

```
brand/captainapply/
  concepts/                            # this folder (12 generated)
  final/
    captainapply-mark.svg              # just the sailboat
    captainapply-mark-dark.svg         # for dark mode
    captainapply-lockup-horizontal.svg # mark + text
    captainapply-favicon-32.png
    captainapply-favicon-180.png       # apple-touch-icon
    captainapply-og-1200x630.png       # social sharing
    captainapply-logo-stacked.svg      # vertical lockup
    brand-guidelines.md                # clearspace, sizes, do/don't
  README.md                            # this file's parent
```

## Where to take it next (Ye.r choices)

| Option | Cost | Time | Quality |
|---|---|---|---|
| **A. Commission Fiverr designer with this brief** | $50-300 | 3-7 days | ⭐⭐⭐⭐ |
| **B. Refine with more image generation** | $0 (MiniMax) | 30 min | ⭐⭐ (rough) |
| **C. Use as-is for v0 launch, refine later** | €0 | 0 | ⭐⭐ |
| **D. Designer via 99designs** | $300-1500 | 5-10 days | ⭐⭐⭐⭐⭐ |

## Constraints to enforce with designer

- ❌ Never 3 sails (Corsair collision)
- ❌ Never skull/pirate imagery
- ❌ Never coral/orange/purple/magenta (Ye.r hard rule)
- ❌ Never fancy script font (must be Inter Tight family)
- ❌ Never include letters that look like other letters
- ✅ Single sail (counts as 1 even with jib)
- ✅ Forward motion implied (sail + wake dots or waterline)
- ✅ Works at 16px (favicon)
- ✅ Works in monochrome (single-color print)
- ✅ Two-tone palette: navy + cyan only
- ✅ Brand signature: tiny cyan dot on the 'i' of "apply"
