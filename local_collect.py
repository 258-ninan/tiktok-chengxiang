"""Zero-service-fee local browser workflow."""

import argparse
import json
import msvcrt
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import access_guard
import collect
from local_browser import BrowserBlocked, LocalBrowser
from report_output import BUNDLE_NAME, publish_reports, write_csv
from reports import export
from sources import Result


ROOT = Path(__file__).resolve().parent
LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_searches (
    id INTEGER PRIMARY KEY, keyword TEXT NOT NULL, window_name TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
    returned_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL, new_count INTEGER NOT NULL,
    observed_oldest TEXT, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_profiles (
    account TEXT PRIMARY KEY, last_checked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_cursor (
    id INTEGER PRIMARY KEY CHECK(id=1), next_keyword INTEGER NOT NULL
);
"""


def now():
    return datetime.now(UTC).isoformat()


@contextmanager
def run_lock():
    with (ROOT / ".local-run.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("Another local browser job or login is running") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def settings():
    data = json.loads((ROOT / "local_config.json").read_text(encoding="utf-8-sig"))
    for key, minimum, maximum in (
        ("max_run_minutes", 1, 30), ("max_video_tasks", 1, 100), ("max_searches", 1, 20),
        ("search_scrolls", 1, 40), ("video_timeout_seconds", 10, 90), ("profile_checks", 0, 10),
    ):
        if type(data.get(key)) is not int or not minimum <= data[key] <= maximum:
            raise ValueError(f"Invalid local config: {key}")
    if data["channel"] not in {"msedge", "chrome"}:
        raise ValueError("Browser channel must be msedge or chrome")
    collect.utc(data["history_floor"])
    if type(data.get('max_discovery_scrolls',80)) is not int or not 20<=data.get('max_discovery_scrolls',80)<=200:
        raise ValueError('Invalid max_discovery_scrolls')
    if type(data.get('metadata_checks',12)) is not int or not 0<=data.get('metadata_checks',12)<=100:
        raise ValueError('Invalid metadata_checks')
    return data


def next_keyword(db):
    words = list(dict.fromkeys(word for group in collect.SEARCH_GROUPS for word in group))
    with collect.transaction(db):
        row = db.execute("SELECT next_keyword FROM local_cursor WHERE id=1").fetchone()
        index = row[0] if row else 0
        db.execute("""
            INSERT INTO local_cursor VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_keyword=excluded.next_keyword
        """, ((index + 1) % len(words),))
    return words[index % len(words)]


def record_search(db, cfg, keyword, window, items, start, floor):
    end = now()
    eligible, added = collect.store_search_results(db, cfg, items, end, floor, [keyword])
    times = [item["createTimeISO"] for item in items if item.get("createTimeISO")]
    db.execute("""
        INSERT INTO local_searches(keyword,window_name,started_at,finished_at,status,returned_count,
                                   eligible_count,new_count,observed_oldest,detail)
        VALUES (?,?,?,?,'success',?,?,?,?,?)
    """, (keyword, window, start, end, len(items), eligible, added, min(times) if times else None,
          "Observed page results. Search completeness and newest-first order are unverified."))
    print(f"Local search {window}: returned={len(items)} eligible={eligible} new={added}", flush=True)


def discover(db, browser, cfg, options, deadline):
    floor = collect.utc(options["history_floor"])
    for _ in range(options["max_searches"]):
        if time.monotonic() + 30 >= deadline:
            return
        keyword = next_keyword(db)
        start = now()
        try:
            items = browser.search(keyword, options["search_scrolls"], deadline)
            recent_floor = max(floor, collect.after(start, days=-7))
            recent = [row for row in items if row["createTimeISO"] >= recent_floor]
            historical = [row for row in items if floor <= row["createTimeISO"] < recent_floor]
            record_search(db, cfg, keyword, "recent_7_days", recent, start, recent_floor)
            record_search(db, cfg, keyword, "history_since_floor", historical, start, floor)
        except BrowserBlocked:
            db.execute("""
                INSERT INTO local_searches(keyword,window_name,started_at,finished_at,status,
                    returned_count,eligible_count,new_count,detail) VALUES (?,?,?,?,'blocked',0,0,0,?)
            """, (keyword, "all", start, now(), "Login or verification requires attention"))
            raise
    accounts = db.execute("""
        SELECT v.account FROM videos v LEFT JOIN local_profiles p ON v.account=p.account
        WHERE v.material IN ('chips','crumbs','chunks','mixed_raw')
        GROUP BY v.account ORDER BY COALESCE(p.last_checked,''), v.account LIMIT ?
    """, (options["profile_checks"],)).fetchall()
    for row in accounts:
        if time.monotonic() + 30 >= deadline:
            return
        account = row["account"]
        start = now()
        items = browser.profile_videos(account, options["search_scrolls"], deadline)
        record_search(db, cfg, account, "account_recent", items, start, floor)
        db.execute("""
            INSERT INTO local_profiles VALUES (?,?) ON CONFLICT(account) DO UPDATE SET last_checked=excluded.last_checked
        """, (account, now()))


def next_local_job(db, cfg, seen, account_counts, *, lease_seconds=None, published_since=None,
                   source="local_browser", video_id=None):
    def priority(video):
        return (video["stage"] != 2, account_counts.get(video["account"], 0),
                -datetime.fromisoformat(video["published_at"] or video["discovered_at"]).timestamp())

    job = collect.claim(db, cfg, now(), source, seen, video_id=video_id, priority=priority,
                        lease_seconds=lease_seconds, published_since=published_since)
    if job is not None:
        account = job[0]["account"]
        account_counts[account] = account_counts.get(account, 0) + 1
    return job


def process(db, browser, cfg, options, deadline, seen, account_counts):
    while len(seen) < options["max_video_tasks"] and time.monotonic() + 90 < deadline:
        job = next_local_job(db, cfg, seen, account_counts)
        if job is None:
            return
        video, attempt_id, count = job
        seen.add(video["video_id"])
        try:
            result = browser.fetch(video, video["stage"], count)
        except BrowserBlocked as exc:
            collect.finish(db, video, attempt_id, Result("access_paused", detail=str(exc)), now())
            raise
        except Exception as exc:
            result = Result("error", detail=f"Local browser exception: {type(exc).__name__}")
        collect.finish(db, video, attempt_id, result, now())


def export_local(db, cfg):
    import discovery

    timestamp = now()
    directory = ROOT / "out" / "auto"
    with publish_reports(directory) as output, collect.transaction(db):
        collect.expire_leases(db, timestamp)
        annotations = ROOT / "annotations.local.csv"
        export(db, collect.queue(db, cfg, timestamp), output, annotations if annotations.is_file() else None)
        rows = [dict(row) for row in db.execute("SELECT * FROM local_searches ORDER BY id")]
        write_csv(output / "local_searches.csv",
                  "id keyword window_name started_at finished_at status returned_count eligible_count new_count observed_oldest detail".split(), rows)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_observations'").fetchone():
            discovery.export_audit(db, output)
        events = db.execute("SELECT * FROM chrome_access_events ORDER BY observed_at")
        write_csv(output / "chrome_access_events.csv",
                  [field[0] for field in events.description], [dict(row) for row in events])
        coverage = db.execute("""
            SELECT v.video_id,v.url,v.published_at,
                MAX(CASE WHEN a.stage=1 AND a.status IN ('success','empty_confirmed') THEN a.finished_at END) AS first_complete_at,
                MAX(CASE WHEN a.stage=2 AND a.status IN ('success','empty_confirmed') THEN a.finished_at END) AS followup_complete_at,
                (SELECT status FROM attempts WHERE video_id=v.video_id AND source=? ORDER BY id DESC LIMIT 1) AS last_status,
                (SELECT COUNT(*) FROM comments WHERE video_id=v.video_id) AS comments_saved
            FROM videos v LEFT JOIN attempts a ON a.video_id=v.video_id AND a.source=?
            GROUP BY v.video_id ORDER BY v.published_at,v.video_id
        """, (collect.CHROME_FULL, collect.CHROME_FULL))
        write_csv(output / "chrome_comment_coverage.csv",
                  [field[0] for field in coverage.description], [dict(row) for row in coverage])
    collect.log(f"Published reports to {directory / BUNDLE_NAME}")


def check_collection_access(db):
    guard = access_guard.state(db)
    if guard["pause_reason"]:
        raise BrowserBlocked(guard["pause_reason"])
    if guard["rate_until"] and now() < guard["rate_until"]:
        raise BrowserBlocked(f"Local browser collection is paused until {guard['rate_until']}")
    denial = access_guard.denial_until(db)
    if denial and now() < denial:
        raise BrowserBlocked(f"Local browser collection is in access backoff until {denial}")


def login(browser, minutes):
    browser.page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
    print("Complete login manually in the dedicated browser. Waiting up to 10 minutes.", flush=True)
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if browser.signed_in():
            print("Local TikTok session is ready.", flush=True)
            return
        browser.page.wait_for_timeout(1500)
    raise BrowserBlocked("Manual login has not completed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "run", "probe", "export"])
    parser.add_argument("--visible", action="store_true", help="Show the browser for manual inspection")
    parser.add_argument("--video-id", help="Probe a known database video without changing collection rounds")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "comments.sqlite")
    args = parser.parse_args()
    options = settings()
    cfg = collect.config(ROOT / "config.json")
    status = {"started_at": now(), "command": args.command}
    args.db.parent.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    with run_lock(), closing(sqlite3.connect(args.db, timeout=20, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(collect.SCHEMA + LOCAL_SCHEMA)
        if args.command == "export":
            export_local(db, cfg)
            return
        try:
            if args.command in {"run", "probe"}:
                check_collection_access(db)
            with LocalBrowser(ROOT / "local-browser", options, cfg,
                              headless=args.command != "login" and not args.visible) as browser:
                if args.command == "login":
                    login(browser, 10)
                    status["status"] = "login_ready"
                else:
                    if not browser.signed_in():
                        raise BrowserBlocked("Run local-login.ps1 to sign in on this computer")
                    if args.command == "probe":
                        video = db.execute("SELECT * FROM videos WHERE video_id=?", (args.video_id,)).fetchone()
                        if video is None:
                            raise ValueError("Probe requires a known --video-id")
                        if video["url"] in access_guard.denied_urls(db):
                            raise BrowserBlocked("The video has a recorded HTTP 403")
                        result = browser.fetch(video, video["stage"], 1)
                        known = {row[0] for row in db.execute("SELECT comment_id FROM comments WHERE video_id=?",
                                                           (video["video_id"],))}
                        status.update(status=result.status, rows=len(result.rows),
                                      matched_known_ids=sum(row["comment_id"] in known for row in result.rows),
                                      replies=sum(bool(row["parent_id"]) for row in result.rows),
                                      detail=result.detail)
                    else:
                        deadline = time.monotonic() + options["max_run_minutes"] * 60
                        seen = set()
                        account_counts = {}
                        # Existing due tasks are serviced before discovery can fail.
                        process(db, browser, cfg, options, deadline, seen, account_counts)
                        discover(db, browser, cfg, options, deadline)
                        process(db, browser, cfg, options, deadline, seen, account_counts)
                        status.update(status="completed", attempted_videos=len(seen))
        except BrowserBlocked as exc:
            status.update(status="attention_required", detail=str(exc))
            exit_code = 2
        except Exception as exc:
            status.update(status="failed", detail=f"Local browser operation failed: {type(exc).__name__}")
            exit_code = 1
        finally:
            if args.command != "login":
                export_local(db, cfg)
            status["finished_at"] = now()
            (ROOT / "data" / "local-status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(status, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as exc:
        # Avoid printing browser call logs, response bodies or session data.
        print(f"Local collector failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
