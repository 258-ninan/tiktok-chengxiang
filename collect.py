"""CLI and SQLite workflow for a two-round short-video sample."""

import argparse
import csv
import json
import math
import re
import sqlite3
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from report_output import BUNDLE_NAME, publish_reports
from reports import RAW, export
from sources import ActorFailure, ApifySource, JsonSource, Result, canonical, utc
import access_guard


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY, url TEXT NOT NULL, account TEXT NOT NULL,
    published_at TEXT, discovered_at TEXT NOT NULL, followup_due TEXT NOT NULL,
    material TEXT NOT NULL, merchant_country TEXT NOT NULL, location_evidence TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 1 CHECK(stage BETWEEN 1 AND 3),
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS discoveries (
    video_id TEXT NOT NULL REFERENCES videos, source_url TEXT NOT NULL,
    keyword TEXT NOT NULL, found_at TEXT NOT NULL,
    PRIMARY KEY(video_id, source_url, keyword)
);
CREATE TABLE IF NOT EXISTS discovery_cursor (
    id INTEGER PRIMARY KEY CHECK(id=1), next_group INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos,
    stage INTEGER NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL,
    lease_until TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', rows_received INTEGER NOT NULL DEFAULT 0,
    new_comments INTEGER NOT NULL DEFAULT 0, partial_rows TEXT
);
CREATE INDEX IF NOT EXISTS attempts_round ON attempts(video_id, stage, id);
CREATE UNIQUE INDEX IF NOT EXISTS active_attempt ON attempts(video_id) WHERE status='running';
CREATE TABLE IF NOT EXISTS comments (
    video_id TEXT NOT NULL REFERENCES videos, comment_id TEXT NOT NULL,
    parent_id TEXT, text TEXT, posted_at TEXT, author TEXT, collected_at TEXT NOT NULL,
    first_round INTEGER NOT NULL, timing TEXT NOT NULL, attempt_id INTEGER NOT NULL REFERENCES attempts,
    PRIMARY KEY(video_id, comment_id)
);
""" + access_guard.SCHEMA

MATERIALS = {"chips", "crumbs", "chunks", "mixed_raw", "perfume", "oil", "blend", "other", "unknown"}
COUNTRIES = {"SA", "AE", "other", "unknown"}
SUCCESS = {"success", "empty_confirmed"}
CHROME_FULL = "chrome_full"
DEFAULT_SINCE = "2026-01-01T00:00:00+04:00"
SEARCH_KEYWORDS = [
    "خشب العود", "عود طبيعي", "رقائق العود", "دقة العود",
    "تجربة حرق العود", "عود بالجملة", "مورد عود", "كيلو عود",
    "خشب العود السعودية", "خشب العود الإمارات",
]
SEARCH_GROUPS = [
    SEARCH_KEYWORDS,
    ["رقائق خشب العود", "شرائح العود", "قطع خشب العود", "كسر العود", "فتات العود",
     "دقة عود طبيعي", "خشب العود للتبخير", "تبخير خشب العود"],
    ["خشب العود بالجملة", "رقائق العود بالجملة", "دقة العود بالجملة", "كسر العود بالجملة",
     "توريد خشب العود", "مورد خشب العود", "سعر كيلو خشب العود", "كيلو دقة العود"],
    ["خشب العود الرياض", "خشب العود جدة", "دقة العود السعودية", "كسر العود السعودية",
     "خشب العود دبي", "خشب العود الشارقة", "رقائق العود الإمارات", "دقة العود الإمارات"],
    ["حرق رقائق العود", "حرق كسر العود", "تجربة خشب العود", "خشب عود طبيعي للبيع",
     "خشب العود الكمبودي", "خشب العود الهندي", "خشب العود الفيتنامي", "خشب العود الماليزي"],
    ["عود مروكي", "عود موروكي", "دقة مروكي", "دقه مروكي", "كسر مروكي", "كسر مباخر",
     "عود كلمنتان", "دقة كلمنتان", "عود فيتنامي", "عود كمبودي", "عود هندي", "عود ماليزي",
     "عود اندونيسي", "عود إندونيسي", "عود سيلاني", "عود بورمي", "عود لاوسي",
     "عود طبيعي بالجملة", "عود محسن", "تجارة العود", "تجار العود", "بيع العود بالكيلو"],
]


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def log(message):
    """Keep Unicode in UTF-8 logs and escape characters legacy consoles cannot display."""
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        message = message.encode(encoding, errors="backslashreplace").decode(encoding)
    try:
        print(message, flush=True)
    except OSError:
        # A closed output pipe must not turn a committed result into a failed job.
        pass


def after(value, **kwargs):
    return (datetime.fromisoformat(value) + timedelta(**kwargs)).isoformat()


def seed_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        if path.suffix.lower() == ".csv":
            reader = csv.DictReader(stream)
            if "url" not in (reader.fieldnames or []):
                raise ValueError("CSV needs a url column")
            yield from reader
        else:
            for line in stream:
                if line.strip() and not line.lstrip().startswith("#"):
                    yield {"url": line.strip()}


def import_seeds(db, args, cfg, now):
    prepared = []
    for line, row in enumerate(seed_rows(args.file), 1):
        try:
            if None in row:
                raise ValueError("CSV row has unexpected columns")
            row = {key: (value or "").strip() for key, value in row.items()}
            vid, url, account = canonical(row["url"], args.resolve_short)
            published = utc(row["published_at"]) if row.get("published_at") else None
            found = utc(row["discovered_at"]) if row.get("discovered_at") else now
            if found > now or (published and published > found):
                raise ValueError("Expected published_at <= discovered_at <= current time")
            material = row.get("material") or "unknown"
            country = row.get("merchant_country") or "unknown"
            evidence = row.get("location_evidence", "")
            if material not in MATERIALS or country not in COUNTRIES or (country != "unknown" and not evidence):
                raise ValueError("Check material, merchant_country and location_evidence")
            if args.published_since and (not published or published < utc(args.published_since)):
                continue
            prepared.append((vid, url, account, published, found, material, country, evidence, row))
        except (ValueError, OSError) as exc:
            raise ValueError(f"Import record {line}: {exc}") from None
    with transaction(db):
        store_seeds(db, cfg, prepared)
    print(f"Imported {len(prepared)} discovery rows; {db.execute('SELECT COUNT(*) FROM videos').fetchone()[0]} unique videos")


def store_seeds(db, cfg, prepared, automatic=False):
    for vid, url, account, published, found, material, country, evidence, row in sorted(
        prepared, key=lambda item: item[4]
    ):
        db.execute("""
                INSERT INTO videos(video_id,url,account,published_at,discovered_at,followup_due,
                                   material,merchant_country,location_evidence)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET
                    published_at=COALESCE(videos.published_at, excluded.published_at),
                    material=CASE WHEN excluded.material='unknown' OR (? AND videos.material!='unknown')
                                  THEN videos.material ELSE excluded.material END,
                    merchant_country=CASE WHEN excluded.merchant_country='unknown'
                                         THEN videos.merchant_country ELSE excluded.merchant_country END,
                    location_evidence=CASE WHEN excluded.merchant_country='unknown'
                                           THEN videos.location_evidence ELSE excluded.location_evidence END
        """, (vid, url, account, published, found, after(found, hours=cfg["window_hours"]),
              material, country, evidence, automatic))
        keywords = [part.strip() for part in row.get("keywords", "").split("|") if part.strip()] or [""]
        db.executemany("INSERT OR IGNORE INTO discoveries VALUES (?,?,?,?)",
                       ((vid, row["url"], keyword, found) for keyword in keywords))


def raw_material(caption):
    norm = re.sub(r"[\u064b-\u065f\u0670ـ]", "", caption)
    norm = norm.translate(str.maketrans("أإآىة_#", "ااايه  "))
    if re.search(r"عزف|اوتار|موسيقي",norm):
        return None
    if re.search(r"(?:عطر|زيت|دهن|خلطه)\s*(?:ال)?عود|عود\s*(?:معطر|مخلط)|"
                 r"خلطه\s*(?:ال)?بخور|طريقه\s*(?:عمل|صنع)\s*(?:ال)?بخور", norm):
        return None
    forms = [kind for kind, pattern in (
        ("chips", r"رقائق\s*(?:خشب\s*)?(?:ال)?عود|شرائح\s*(?:ال)?عود"),
        ("crumbs", r"دقه\s*(?:ال)?عود|كسر\s*(?:ال)?عود|فتات\s*(?:ال)?عود|(?:دقه|كسر)\s+(?:ال)?(?:مروكي|موروكي|كلمنتان)|كسر\s+مباخر"),
        ("chunks", r"قطع\s*(?:خشب\s*)?(?:ال)?عود|كتل\s*(?:ال)?عود"),
    ) if re.search(pattern, norm)]
    if forms:
        return forms[0] if len(forms) == 1 else "mixed_raw"
    if re.search(r"خشب\s*(?:ال)?عود|حرق\s*(?:ال)?عود", norm) and not re.search(
        r"(?:سعر|شراء|بيع)\s*(?:ال)?مبخره|مبخره\s*(?:كهربائيه|ذكيه)", norm
    ):
        return "mixed_raw"
    if re.search(r"(?:ال)?عود\s+(?:ال)?(?:طبيعي|محسن|مروكي|موروكي|كلمنتان|فيتنامي|كمبودي|هندي|ماليزي|اندونيسي|سيلاني|بورمي|لاوسي)(?:\s|$)", norm):
        return "mixed_raw"
    english = re.sub(r"[_-]+", " ", caption.casefold())
    if re.search(r"\b(?:agarwood|oud)\s+(?:essential\s+)?(?:oil|perfume)\b", english):
        return None
    material = re.search(r"\b(?:agarwood|oud)\s+(?:wood\s+)?(chips|chunks|pieces|crumbs|powder|dust)\b", english)
    if material:
        return {"chips": "chips", "chunks": "chunks", "pieces": "chunks",
                "crumbs": "crumbs", "powder": "crumbs", "dust": "crumbs"}[material[1]]
    if re.search(r"\b(?:raw|natural)\s+agarwood\b|\bagarwood\s+wood\b", english):
        return "mixed_raw"
    return None


def store_search_results(db, cfg, items, now, since, keywords=SEARCH_KEYWORDS):
    prepared = []
    eligible = set()
    for item in items:
        try:
            count = item.get("commentCount")
            caption = item.get("text")
            source_url = item.get("webVideoUrl")
            posted = utc(item.get("createTimeISO") or item["createTime"])
            vid, url, account = canonical(source_url)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            continue
        if not since <= posted <= now or type(count) is not int or count < 1 or not isinstance(caption, str):
            continue
        material = raw_material(caption)
        if not material:
            continue
        query = item.get("searchQuery")
        matches = [keyword for keyword in keywords if keyword == query or keyword in caption]
        row = {"url": source_url, "keywords": "|".join(matches)}
        prepared.append((vid, url, account, posted, now, material, "unknown", "", row))
        eligible.add(vid)
    with transaction(db):
        before = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        store_seeds(db, cfg, prepared, automatic=True)
        added = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] - before
    return len(eligible), added


def expire_leases(db, now):
    db.execute("""
        UPDATE attempts SET status='interrupted', detail='Worker lease expired', finished_at=?
        WHERE status='running' AND lease_until<=?
    """, (now, now))


def in_scope(video, since):
    return bool(video["published_at"] and video["published_at"] >= since and video["material"] in RAW)


def queue(db, cfg, now, *, full_chrome=False):
    result = []
    denied = access_guard.denied_urls(db)
    attempts_by_video = {}
    for attempt in db.execute("""
        SELECT a.video_id,a.source,a.status,a.finished_at FROM attempts a JOIN videos v
        ON a.video_id=v.video_id AND a.stage=CASE WHEN ? AND v.stage=3 THEN 2 ELSE v.stage END
        ORDER BY a.id
    """, (full_chrome,)):
        attempts_by_video.setdefault(attempt["video_id"], []).append(attempt)
    for video in db.execute("""
        SELECT * FROM videos ORDER BY CASE WHEN stage=2 THEN 0 ELSE 1 END, discovered_at, video_id
    """):
        stage = video["stage"]
        attempts = attempts_by_video.get(video["video_id"], ())
        if full_chrome and stage == 3 and not any(
            row["source"] == CHROME_FULL and row["status"] in SUCCESS for row in attempts
        ):
            video = dict(video) | {"stage": 2}
            stage = 2
        if full_chrome:
            attempts = [row for row in attempts if row["source"] == CHROME_FULL or row["status"] == "running"]
        state = "ready"
        due = video["discovered_at"] if stage == 1 else video["followup_due"]
        last = attempts[-1] if attempts else None
        charged = [row for row in attempts if row["status"] not in {"access_paused", "rate_limited"}]
        if stage == 3:
            state = "archived"
        elif video["url"] in denied:
            state = "access_denied"
        elif last and last["status"] == "running":
            state = "running"
        elif len(charged) >= cfg["max_attempts"]:
            state = "exhausted"
        else:
            if charged:
                due = max(due, after(charged[-1]["finished_at"], minutes=cfg["retry_minutes"]))
            if now < due:
                state = "retry_wait" if last else "waiting"
        result.append((video, state, due, len(charged)))
    return result


def claim(db, cfg, now, source, exclude, video_id=None, published_since=None, *,
          priority=None, lease_seconds=None):
    with transaction(db):
        expire_leases(db, now)
        candidates = queue(db, cfg, now, full_chrome=source == CHROME_FULL)
        if priority is not None:
            candidates.sort(key=lambda entry: priority(entry[0]))
        for video, state, due, count in candidates:
            vid = video["video_id"]
            if vid in exclude or (video_id and vid != video_id):
                continue
            if published_since and not in_scope(video, published_since):
                continue
            eligible = state == "ready"
            if source == "manual":
                eligible = state not in {"archived", "running", "access_denied"} and (
                    video["discovered_at"] if video["stage"] == 1 else video["followup_due"]
                ) <= now
            if eligible:
                lease = after(now, seconds=lease_seconds or cfg["actor_timeout_seconds"] + 120)
                cursor = db.execute("""
                    INSERT INTO attempts(video_id,stage,source,started_at,lease_until,status)
                    VALUES (?,?,?,?,?,'running')
                """, (vid, video["stage"], source, now, lease))
                return video, cursor.lastrowid, count + 1
    return None


def store_comments(db, video, attempt_id, rows, now):
    vid, stage = video["video_id"], video["stage"]
    added = 0
    for row in rows:
        posted = row["posted_at"]
        if posted is None:
            timing = "time_unknown"
        elif stage == 1:
            timing = "baseline"
        elif posted < video["discovered_at"]:
            timing = "older_not_seen"
        elif posted <= video["followup_due"]:
            timing = "window_new_observed"
        else:
            timing = "after_window_observed"
        added += db.execute("""
            INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(video_id,comment_id) DO NOTHING
        """, (vid, row["comment_id"], row["parent_id"], row["text"], posted, row["author"],
              now, stage, timing, attempt_id)).rowcount
        # A later observation may fill in metadata absent on first sight.
        existing = db.execute("""
            SELECT first_round, posted_at FROM comments WHERE video_id=? AND comment_id=?
        """, (vid, row["comment_id"])).fetchone()
        if existing["posted_at"] is None and posted:
            if existing["first_round"] == 1:
                timing = "baseline"
            db.execute("UPDATE comments SET posted_at=?, timing=? WHERE video_id=? AND comment_id=?",
                       (posted, timing, vid, row["comment_id"]))
        db.execute("""
            UPDATE comments SET text=COALESCE(text,?), author=COALESCE(author,?),
                                parent_id=COALESCE(parent_id,?)
            WHERE video_id=? AND comment_id=?
        """, (row["text"], row["author"], row["parent_id"], vid, row["comment_id"]))
    return added


def checkpoint_comments(db, video, attempt_id, rows, now, lease_seconds):
    with transaction(db):
        attempt = db.execute(
            "SELECT * FROM attempts WHERE id=? AND video_id=? AND stage=? AND status='running'",
            (attempt_id, video["video_id"], video["stage"]),
        ).fetchone()
        if not attempt or attempt["lease_until"] <= now:
            return False
        added = store_comments(db, video, attempt_id, rows, now)
        db.execute("""
            UPDATE attempts SET rows_received=rows_received+?, new_comments=new_comments+?, lease_until=?
            WHERE id=?
        """, (len(rows), added, after(now, seconds=lease_seconds), attempt_id))
    return True


def finish(db, video, attempt_id, result, now, on_finish=None):
    with transaction(db):
        attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        vid, stage = video["video_id"], video["stage"]
        if not attempt or attempt["video_id"] != vid or attempt["stage"] != stage:
            raise ValueError("Attempt does not match the video and round")
        if attempt["status"] != "running":
            return False
        if attempt["lease_until"] <= now:
            expire_leases(db, now)
            return False
        added = 0
        if result.status in SUCCESS:
            added = store_comments(db, video, attempt_id, result.rows, now)
            db.execute("UPDATE videos SET stage=?, archived_at=COALESCE(archived_at,?) WHERE video_id=?",
                       (stage + 1, now if stage == 2 else None, vid))
        partial = json.dumps(result.rows, ensure_ascii=False) if result.rows and result.status not in SUCCESS else None
        db.execute("""
            UPDATE attempts SET status=?, detail=?, finished_at=?, rows_received=rows_received+?, new_comments=new_comments+?, partial_rows=?
            WHERE id=?
        """, (result.status, result.detail, now, len(result.rows), added, partial, attempt_id))
        if on_finish:
            on_finish()
    observed = attempt["rows_received"] + len(result.rows)
    inserted = attempt["new_comments"] + added
    log(f"{vid} round={stage} {result.status} observed={observed} inserted={inserted} {result.detail}")
    return True


def collect(db, args, cfg, clock):
    if args.source == "apify":
        if args.now or not args.authorized or args.file:
            raise ValueError("Apify requires --authorized, with the real clock and without --file")
        source = ApifySource(cfg)
        source.require_free_plan()
    else:
        if not args.file:
            raise ValueError("JSON source requires --file")
        source = JsonSource(args.file, cfg)
    run_queue(db, cfg, clock, source, args.source, args.limit)


def fetch_result(source, video, count):
    try:
        return source.fetch(video, video["stage"], count)
    except Exception as exc:
        return Result("error", detail=f"Adapter exception: {type(exc).__name__}")


def run_queue(db, cfg, clock, source, source_name, limit, since=None, workers=1):
    seen = set()
    pending = {}
    blocked = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            while not blocked and len(seen) < limit and len(pending) < workers:
                job = claim(db, cfg, clock(), source_name, seen, published_since=since)
                if job is None:
                    break
                video, attempt_id, count = job
                seen.add(video["video_id"])
                pending[pool.submit(fetch_result, source, video, count)] = (video, attempt_id)
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                video, attempt_id = pending.pop(future)
                result = future.result()
                finish(db, video, attempt_id, result, clock())
                if result.status == "account_blocked" and not blocked:
                    blocked = True
                    print("Stopped submitting jobs. Check Apify token, Free plan usage, or rate limits.")
    print(f"Processed {len(seen)} eligible videos")


def next_search_groups(db, count):
    with transaction(db):
        row = db.execute("SELECT next_group FROM discovery_cursor WHERE id=1").fetchone()
        start = row["next_group"] if row else 0
        indexes = [(start + offset) % len(SEARCH_GROUPS) for offset in range(count)]
        db.execute("""
            INSERT INTO discovery_cursor VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_group=excluded.next_group
        """, ((start + count) % len(SEARCH_GROUPS),))
    return indexes


def discover(db, args, cfg, source, clock, since):
    indexes = next_search_groups(db, args.search_batches)
    blocked = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {index: pool.submit(source.search, SEARCH_GROUPS[index], args.per_keyword)
                   for index in indexes}
        for index, future in futures.items():
            try:
                items, detail = future.result()
                eligible, added = store_search_results(db, cfg, items, clock(), since, SEARCH_GROUPS[index])
                print(f"Search group={index + 1} rows={len(items)}; eligible videos={eligible}; new tasks={added}; {detail}")
            except ValueError as exc:
                print(f"Search group={index + 1} failed: {exc}", file=sys.stderr)
                blocked |= isinstance(exc, ActorFailure) and exc.status == "account_blocked"
    return blocked


def automatic(db, args, cfg, clock):
    if (args.now or not args.authorized or not 1 <= args.limit <= 100
            or not 1 <= args.per_keyword <= 20 or not 1 <= args.workers <= 3
            or not 1 <= args.search_batches <= len(SEARCH_GROUPS)):
        raise ValueError("Auto requires --authorized, real time and valid batch limits (see auto --help)")
    since = utc(args.since)
    if since > clock():
        raise ValueError("--since is in the future")
    source = ApifySource(cfg)
    source.require_free_plan()
    blocked = not args.skip_discovery and discover(db, args, cfg, source, clock, since)
    if not blocked:
        run_queue(db, cfg, clock, source, "apify", args.limit, since, args.workers)
    with publish_reports(args.output) as staging, transaction(db):
        states = [entry for entry in queue(db, cfg, clock()) if in_scope(entry[0], since)]
        export(db, states, staging, args.annotations)
    log(f"Published reports to {args.output / BUNDLE_NAME}")


def config(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    for field, low, high in (
        ("window_hours", 1, 720), ("top_level_limit", 1, 30),
        ("replies_per_comment", 0, 10), ("total_limit", 1, 1000),
        ("max_attempts", 1, 20), ("retry_minutes", 0, 1440),
        ("actor_timeout_seconds", 10, 600), ("max_run_usd", 0.50, 100),
    ):
        value = data.get(field)
        expected = (int, float) if field == "max_run_usd" else (int,)
        if type(value) not in expected or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid config: {field}")
    if data["total_limit"] < data["top_level_limit"]:
        raise ValueError("total_limit must be at least top_level_limit")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/comments.sqlite"))
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--now", help="Fixed ISO timestamp for offline verification")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("import")
    add.add_argument("file", type=Path)
    add.add_argument("--published-since")
    add.add_argument("--resolve-short", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--source", choices=["apify", "json"], required=True)
    run.add_argument("--file", type=Path)
    run.add_argument("--limit", type=int, default=10)
    run.add_argument("--authorized", action="store_true")
    auto = commands.add_parser("auto", help="Find raw oud videos and collect due comments using Apify Free")
    auto.add_argument("--since", default=DEFAULT_SINCE)
    auto.add_argument("--per-keyword", type=int, default=12, help="1–20 results per search term (default 12)")
    auto.add_argument("--search-batches", type=int, default=2, help="1–5 rotating keyword groups (default 2)")
    auto.add_argument("--limit", type=int, default=25, help="1–100 comment attempts per invocation (default 25)")
    auto.add_argument("--workers", type=int, default=3, help="1–3 concurrent Apify runs (default 3)")
    auto.add_argument("--skip-discovery", action="store_true")
    auto.add_argument("--output", type=Path, default=Path("out/apify"))
    auto.add_argument("--annotations", type=Path)
    auto.add_argument("--authorized", action="store_true")
    commands.add_parser("check-apify", help="Verify the token and Free plan without running an Actor")
    empty = commands.add_parser("confirm-empty")
    empty.add_argument("video_id")
    empty.add_argument("--evidence", required=True)
    commands.add_parser("status")
    report = commands.add_parser("export", help="Publish one complete reports.zip")
    report.add_argument("directory", type=Path, help="Directory for reports.zip")
    report.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    default_db = Path(__file__).resolve().parent / "data" / "comments.sqlite"
    default_annotations = default_db.parent.parent / "annotations.local.csv"
    if (args.command in {"export", "auto"} and args.annotations is None
            and args.db.resolve() == default_db and default_annotations.is_file()):
        args.annotations = default_annotations
    cfg = config(args.config)
    if args.command == "check-apify":
        ApifySource(cfg).require_free_plan()
        print("Apify token verified: Free personal plan")
        return
    fixed = utc(args.now) if args.now else None

    def clock():
        return fixed or datetime.now(UTC).isoformat()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.db, timeout=20, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        if args.command == "import":
            import_seeds(db, args, cfg, clock())
        elif args.command == "run":
            if args.limit <= 0:
                raise ValueError("--limit must be positive")
            collect(db, args, cfg, clock)
        elif args.command == "auto":
            automatic(db, args, cfg, clock)
        elif args.command == "confirm-empty":
            if not args.evidence.strip():
                raise ValueError("A dated observation and source are required")
            job = claim(db, cfg, clock(), "manual", set(), args.video_id)
            if job is None:
                raise ValueError("No eligible round for that video")
            video, attempt_id, _ = job
            finish(db, video, attempt_id, Result("empty_confirmed", detail=args.evidence), clock())
        elif args.command == "export":
            with publish_reports(args.directory) as staging, transaction(db):
                expire_leases(db, clock())
                export(db, queue(db, cfg, clock()), staging, args.annotations)
            log(f"Published reports to {args.directory / BUNDLE_NAME}")
        else:
            with transaction(db):
                expire_leases(db, clock())
                states = queue(db, cfg, clock())
                for video, state, due, count in states:
                    print(f"{video['video_id']} round={min(2, video['stage'])} {state} due={due} attempts={count}")
                for row in db.execute("SELECT status, COUNT(*) n FROM attempts GROUP BY status"):
                    print(f"{row['status']}={row['n']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
