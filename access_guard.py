"""Persistent access decisions for the Chrome collector."""

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime


SCHEMA = """
CREATE TABLE IF NOT EXISTS chrome_access_events (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL,
    outcome TEXT NOT NULL, http_status INTEGER, detail TEXT NOT NULL,
    observed_at TEXT NOT NULL, retry_until TEXT
);
CREATE INDEX IF NOT EXISTS chrome_access_event_status
    ON chrome_access_events(http_status, observed_at);
CREATE TABLE IF NOT EXISTS chrome_access_state (
    id INTEGER PRIMARY KEY CHECK(id=1), pause_reason TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '', rate_until TEXT,
    rate_streak INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO chrome_access_state(id) VALUES (1);
CREATE TABLE IF NOT EXISTS chrome_denial_backoff (
    id INTEGER PRIMARY KEY CHECK(id=1), retry_until TEXT, streak INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO chrome_denial_backoff(id) VALUES (1);
"""

LEGACY_DENIAL_PAUSE = "HTTP 403 on three different links within ten minutes"
PHASES = ("assigned", "navigation_started", "page_loaded", "collector_started",
          "result_ready", "delivery_failed")
TIMEOUT_REASONS = {
    "assigned": "extension_unresponsive",
    "navigation_started": "page_load_timeout",
    "page_loaded": "extension_unresponsive",
    "collector_started": "extension_unresponsive",
    "result_ready": "result_delivery_failed",
    "delivery_failed": "result_delivery_failed",
}


def state(db):
    return db.execute("SELECT * FROM chrome_access_state WHERE id=1").fetchone()


def denial_until(db):
    return db.execute("SELECT retry_until FROM chrome_denial_backoff WHERE id=1").fetchone()[0]


def record_success(db):
    db.execute("UPDATE chrome_access_state SET rate_until=NULL,rate_streak=0 WHERE id=1")
    db.execute("UPDATE chrome_denial_backoff SET retry_until=NULL,streak=0 WHERE id=1")


def migrate_denial_pause(db):
    """Retain past denials and turn the old broad pause into a timed recovery."""
    if state(db)["pause_reason"] != LEGACY_DENIAL_PAUSE:
        return
    last = db.execute("SELECT MAX(observed_at) FROM chrome_access_events WHERE http_status=403").fetchone()[0]
    if not last:
        return
    deadline = (datetime.fromisoformat(last) + timedelta(minutes=30)).isoformat()
    db.execute("UPDATE chrome_denial_backoff SET retry_until=?,streak=1 WHERE id=1", (deadline,))
    db.execute("UPDATE chrome_access_state SET pause_reason='' WHERE id=1")


def denied_urls(db):
    return {row[0] for row in db.execute(
        "SELECT DISTINCT url FROM chrome_access_events WHERE http_status=403"
    )}


def retry_deadline(value, now, streak):
    """Use a valid Retry-After verbatim, otherwise back off 1, 2, 4... minutes."""
    value = value.strip() if isinstance(value, str) else ""
    if value and value.isascii() and value.isdigit():
        try:
            return now + timedelta(seconds=int(value))
        except (ValueError, OverflowError):
            return datetime.max.replace(tzinfo=UTC)
    if value:
        try:
            date = parsedate_to_datetime(value)
            return max(now, date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC))
        except (ValueError, TypeError, OverflowError, AttributeError):
            pass
    if streak is not None:
        return now + timedelta(seconds=min(900, 60 * 2 ** min(max(streak - 1, 0), 4)))
    return None


def record(db, job, outcome, detail, timestamp, http_status=None, retry_after=None):
    """Write the event and any gate inside the caller's transaction."""
    current = state(db)
    now = datetime.fromisoformat(timestamp)
    retry_until = None
    if http_status == 429:
        streak = min(current["rate_streak"] + 1, 5)
        retry_until = retry_deadline(retry_after, now, streak).isoformat()
        retry_until = max(retry_until, current["rate_until"] or "")
        db.execute("UPDATE chrome_access_state SET rate_until=?,rate_streak=? WHERE id=1",
                   (retry_until, streak))
    db.execute("INSERT INTO chrome_access_events VALUES (?,?,?,?,?,?,?,?)",
               (job["id"], job["kind"], job["url"], outcome, http_status, detail,
                timestamp, retry_until))
    pause_reason = None
    if outcome == "access_paused":
        pause_reason = detail
    elif http_status == 403:
        floor = (now - timedelta(minutes=10)).isoformat()
        count = db.execute(
            """SELECT COUNT(DISTINCT url) FROM chrome_access_events
               WHERE http_status=403 AND observed_at>=? AND observed_at>?""",
            (floor, current["reviewed_at"]),
        ).fetchone()[0]
        streak = db.execute("SELECT streak FROM chrome_denial_backoff WHERE id=1").fetchone()[0]
        header_deadline = retry_deadline(retry_after, now, None)
        if count >= 3 or streak or header_deadline:
            streak = min(streak + 1, 4)
            wait = now + timedelta(minutes=30 * 2 ** (streak - 1))
            retry_until = max(wait, header_deadline or now).isoformat()
            db.execute("UPDATE chrome_denial_backoff SET retry_until=?,streak=? WHERE id=1",
                       (retry_until, streak))
            db.execute("UPDATE chrome_access_events SET retry_until=? WHERE job_id=?",
                       (retry_until, job["id"]))
    if pause_reason:
        db.execute("UPDATE chrome_access_state SET pause_reason=? WHERE id=1", (pause_reason,))
    return retry_until
