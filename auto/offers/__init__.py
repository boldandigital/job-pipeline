"""
auto/offers — MAIL-3: Offer evaluation UI.

Three small modules (tested in isolation, all best-effort / fail-open):

  * ``summary``  — render a 1-page offer PDF (WeasyPrint) and post to Discord.
  * ``decision`` — parse operator replies like ``accept 48`` / ``decline 48`` /
                   ``negotiate 48 <notes>`` / ``offer info 48``.
  * ``sheet``    — append/update the dedicated "Offers" tab in the Job
                   Pipeline Google Sheet, with auto-coloring per decision.

Trigger contract (consumed by MAIL-1 mail watcher — not yet wired here):

    from auto.offers import summary
    summary.handle_offer_transition(job_id=48, job=job_dict)

The pipeline integration is left to MAIL-1 — this card ships the surface,
not the source detector.
"""

__version__ = "0.1.0"
__phase__ = "MAIL-3"
