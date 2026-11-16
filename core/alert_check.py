"""Second check of every alert: does another model, reading the notice on its own, reach the same
person on the same day?

Shadow mode. Nothing waits on this - the alert has usually gone out already. Every person alerted
gets one alert_checks row, and /stats shows how often the two readings agree, so we know how
accurate the alerts are without anyone checking them by hand.

How: one extra call per outage (not per person), with a stronger model than the one that parsed
the notice (config.GEMINI_FALLBACK_MODEL_NAME while this check's share of its daily budget lasts,
else another cascade model). The model gets only the notice's own text - never a subscriber's address. Its reading is
then compared with each alerted person here, with the same street/number rule the alerts use
(core.matchmaker._target_match), and its dates with the outage's day.

"u blizini" alerts (a fault nearby, a side street) are not about the person's own street, so the
second reading can't judge them: they are recorded as NEARBY and left out of the agreement rate.

    alert_checks(outage_id, target_id, verdict AGREE|DISAGREE|NEARBY|ERROR, reason, model)
"""
from datetime import timedelta

import config
from core.api_budget import remaining
from core.database import get_connection

# Alerts this recent are checked; older ones are history (a fresh deploy doesn't re-check a month).
WINDOW = "-2 days"
# At most this many outages per cycle, so one busy morning never burns the day's budget at once.
PER_CYCLE = 5
# The cheaper model when the stronger one's daily budget is spent. Not config.MODEL_NAME: a second
# reading by the same model that made the first proves little.
SPARE_MODEL = "gemini-3.1-flash-lite"
NEARBY_MARK = "u blizini."


TRIES = 2


def _model() -> tuple[str, tuple[str, int] | None]:
    """The stronger model while this check's own share of its daily budget lasts (core/api_budget
    .py, every request counted), else the spare lite model, which isn't capped. A full TRIES must
    be left, so a reading never runs out of budget halfway."""
    budget = (f"{config.GEMINI_FALLBACK_MODEL_NAME}:check", config.GEMINI_FALLBACK_CHECK_SHARE)
    if remaining(*budget) >= TRIES:
        return config.GEMINI_FALLBACK_MODEL_NAME, budget
    return SPARE_MODEL, None


def _day_in(day, second) -> bool | None:
    """Is the outage's day inside the second reading's dates? A night window ("22:00 - 06:00")
    may end the next morning, so one day past the end still counts. None when either can't be
    read."""
    from core.matchmaker import _parse_date
    start = _parse_date(second.outage_date or "")
    end = _parse_date(getattr(second, "end_date", None) or "") or start
    if not day or not start:
        return None
    return start.date() <= day.date() <= (end + timedelta(days=1)).date()


def judge(alert, second, target) -> tuple[str, str]:
    """(AGREE | DISAGREE, reason) for one person alerted by `alert`, against the second reading."""
    from core.matchmaker import _parse_date, _target_match
    if _target_match(second, second.affected_locations, target, "EXACT") is None:
        return "DISAGREE", "second reading does not reach this street and number"
    day = _parse_date(alert.outage_date or "")
    if _day_in(day, second) is False:
        span = second.outage_date + (f"-{second.end_date}" if getattr(second, "end_date", None) else "")
        return "DISAGREE", f"day {alert.outage_date} is not in the second reading's {span}"
    return "AGREE", ""


def check(outage_id: int, target_ids: list[int]) -> int:
    """Second reading for one outage, judged for each of `target_ids`. Returns rows written."""
    rows = judge_outage(outage_id, target_ids)
    conn = get_connection()
    try:
        conn.executemany("INSERT OR IGNORE INTO alert_checks (outage_id, target_id, verdict, reason, model) "
                         "VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def judge_outage(outage_id: int, target_ids: list[int], model: str | None = None,
                 tries: int = TRIES) -> list[tuple]:
    """The second reading and its verdict per person, written nowhere: (outage_id, target_id,
    verdict, reason, model) rows. `model` given: that model, no budget (scripts/try_model.py
    trying a candidate); else _model() picks it."""
    from parser import OutageAlert, parse_announcement
    conn = get_connection()
    try:
        outage = conn.execute("SELECT raw_text, parsed_json FROM outages WHERE id = ?",
                              (outage_id,)).fetchone()
        targets = {r["id"]: r for r in conn.execute(
            f"SELECT id, ulica, broj, city, opstina, naselje, lat, lng FROM targets "
            f"WHERE id IN ({','.join('?' * len(target_ids))})", target_ids)}
        messages = {r["target_id"]: r["custom_message"] or "" for r in conn.execute(
            "SELECT target_id, custom_message FROM message_queue WHERE outage_id = ?", (outage_id,))}
    finally:
        conn.close()
    if not outage:
        return []

    rows, ask = [], []
    for tid in target_ids:
        if NEARBY_MARK in messages.get(tid, ""):
            rows.append((outage_id, tid, "NEARBY", None, None))
        elif tid in targets:
            ask.append(tid)

    if ask:
        model, budget = (model, None) if model else _model()
        second = None
        try:
            parsed = parse_announcement(outage["raw_text"] or "", max_retries=tries, model=model,
                                        budget=budget)
            second = OutageAlert.model_validate(parsed) if parsed else None
            if second is not None:
                # A block notice is judged as a block on both readings: the houses inside it.
                from core.matchmaker import _BB_LABEL, _drop_bb_streets, _mark_block_ring
                _mark_block_ring(second, outage["raw_text"] or "")
        except Exception as e:
            print(f"⚠️ alert check #{outage_id}: second reading failed: {e}")
        alert = OutageAlert.model_validate_json(outage["parsed_json"] or "{}") if outage["parsed_json"] else None
        # "Улица бб" as one building, on the second reading too - when the first was read that way
        # (a fault list's "бб" is the fault's spot, and is left alone on both).
        if second is not None and alert is not None and any(
                _BB_LABEL.search(loc.landmark or "") and not loc.main_street for loc in alert.affected_locations):
            _drop_bb_streets(second, outage["raw_text"] or "")
        for tid in ask:
            if second is None or alert is None:
                rows.append((outage_id, tid, "ERROR", "no second reading", model))
                continue
            verdict, reason = judge(alert, second, targets[tid])
            rows.append((outage_id, tid, verdict, reason or None, model))
            if verdict == "DISAGREE":
                print(f"🔎 alert check #{outage_id}, target {tid}: {reason}")
    return rows


def run_pending() -> int:
    """Every recent alert without a second check yet, a few outages per cycle."""
    conn = get_connection()
    try:
        pending = conn.execute(f"""
            SELECT m.outage_id, m.target_id FROM message_queue m
            LEFT JOIN alert_checks c ON c.outage_id = m.outage_id AND c.target_id = m.target_id
            WHERE c.outage_id IS NULL AND m.status IN ('QUEUED', 'SENDING', 'SENT', 'HELD')
              AND m.created_at >= datetime('now', '{WINDOW}')
            GROUP BY m.outage_id, m.target_id ORDER BY m.outage_id""").fetchall()
    finally:
        conn.close()
    by_outage: dict[int, list[int]] = {}
    for r in pending:
        by_outage.setdefault(r["outage_id"], []).append(r["target_id"])
    done = 0
    for outage_id in list(by_outage)[:PER_CYCLE]:
        try:
            done += check(outage_id, by_outage[outage_id])
        except Exception as e:
            print(f"⚠️ alert check #{outage_id} failed: {e}")
    return done
