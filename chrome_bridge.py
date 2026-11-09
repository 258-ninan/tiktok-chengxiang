"""Local-only collector for the existing signed-in Chrome profile."""

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
import hmac
import json
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import access_guard
import collect
from comment_coverage import Coverage, page_coordinates
import discovery
import local_collect
from local_browser import comment_items, video_items
from sources import Result, item_id, normalize


ROOT = Path(__file__).resolve().parent
PROTOCOL = "5"
JOB_TIMEOUT_SECONDS = 150
COMMENT_ENTRY_RETRY_SECONDS = 40
INTERNATIONAL_SEARCH_WORDS = (
    "raw agarwood chips", "agarwood chips wholesale", "agarwood wood chips for burning",
    "agarwood powder supplier", "agarwood chunks wholesale",
)


class StaleJob(ValueError):
    pass


def setup_token():
    path = ROOT / "chrome_extension" / "local_config.json"
    if not path.exists():
        path.write_text(json.dumps({"token": secrets.token_urlsafe(32)}) + "\n", encoding="utf-8")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value.get("token"), str) or len(value["token"]) < 32:
        raise ValueError("Invalid local pairing token")
    return value["token"]


class Bridge:
    def __init__(self, db, cfg, options):
        self.db = db
        self.cfg = cfg
        self.options = options
        self.probe_video_id = options.get("probe_video_id")
        self.history_floor = collect.utc(options["history_floor"])
        discovery.initialize(db)
        self.started_monotonic = time.monotonic()
        self.deadline = self.started_monotonic + options["max_run_minutes"] * 60
        self.minimum_job_window = min(90, options["max_run_minutes"] * 30)
        self.seen = set()
        self.account_counts = {}
        self.searches = 0
        self.profile_pages = 0
        self.metadata_pages = 0
        self.prior_metadata_pages = 0
        self.prior_profile_pages = 0
        self.discovery_turn = 0
        self.active_seeds = {}
        self.video_streak = 0
        self.continuous = options.get("continuous", False)
        self.batch_started = self.started_monotonic
        self.batch_number = 1
        self.prior_searches = 0
        self.prior_attempts = 0
        self.unique_videos = set()
        self.current = None
        self.last_contact = None
        self.paused = bool(access_guard.state(db)["pause_reason"])
        self.stop_requested = False
        self.export_pending = False
        self.lock = threading.Lock()
        started = datetime.now(UTC)
        self.status = {
            "status": "waiting_for_extension", "started_at": started.isoformat(),
            "planned_end_at": (started + timedelta(minutes=options["max_run_minutes"])).isoformat(),
            "duration_minutes": options["max_run_minutes"], "continuous": self.continuous,
            "baseline_comments": db.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
            "baseline_videos": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        }
        if self.paused:
            self.status.update(status="attention_required", detail=access_guard.state(db)["pause_reason"])
        if self.probe_video_id:
            self.status["probe_video_id"] = self.probe_video_id

    def save_status(self):
        self.status.update(
            last_contact=self.last_contact, searches=self.prior_searches + self.searches,
            attempted_videos=self.prior_attempts + len(self.seen),
            unique_attempted_videos=len(self.unique_videos),
            batch_number=self.batch_number, batch_searches=self.searches,
            batch_attempts=len(self.seen), pending_job=self.current["kind"] if self.current else None,
            pending_phase=self.current.get("phase", "assigned") if self.current else None,
            pending_video_trace=self.current.get("video_trace") if self.current else None,
            pending_comment_diagnostics=self.comment_diagnostics(self.current)
            if self.current and self.current["kind"] == "video" else None,
            pending_entry_retries=int(bool(self.current and self.current.get("entry_retry_of"))),
            profile_pages=self.prior_profile_pages + self.profile_pages,
            metadata_pages=self.prior_metadata_pages+self.metadata_pages,
            heartbeat_at=local_collect.now(),
        )
        comments = self.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        videos = self.db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        self.status.update(comments=comments, videos=videos,
                           added_comments=comments - self.status["baseline_comments"],
                           added_videos=videos - self.status["baseline_videos"])
        self.status['candidate_videos'] = self.db.execute('SELECT COUNT(DISTINCT video_id) FROM discovery_observations').fetchone()[0]
        self.status["retry_until"] = access_guard.state(self.db)["rate_until"]
        self.status["denial_retry_until"] = access_guard.denial_until(self.db)
        path = ROOT / "data" / "chrome-bridge-status.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.status, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            collect.log(f"Could not publish Chrome status: {type(exc).__name__}")

    def publish_exports(self):
        try:
            local_collect.export_local(self.db, self.cfg)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.export_pending = True
            self.status["export_error"] = f"{type(exc).__name__}: {exc}"
            collect.log(f"Chrome export failed: {self.status['export_error']}")
        else:
            self.export_pending = False
            self.status.pop("export_error", None)
            self.status["exported_at"] = local_collect.now()

    def expire_job(self):
        if self.current and time.monotonic() > self.current["expires"]:
            phase = self.current.get("phase", "assigned")
            detail = f"{access_guard.TIMEOUT_REASONS[phase]} (last phase: {phase})"
            trace = self.current.get("video_trace")
            if trace:
                detail += " | video_trace=" + json.dumps(trace, ensure_ascii=False)
            self.record_failure("timeout", detail)

    def active_job(self, data):
        self.expire_job()
        if not self.current or data.get("jobId") != self.current["id"]:
            raise StaleJob("Unknown or expired job")
        return self.current

    def progress(self, data, *, final_trace=False):
        job = self.active_job(data)
        phase = data.get("phase")
        if phase not in access_guard.PHASES:
            raise ValueError("Unknown progress phase")
        changed = access_guard.PHASES.index(phase) > access_guard.PHASES.index(job.get("phase", "assigned"))
        if changed:
            job["phase"] = phase
        changed |= self.observe_comment_network(job, data.get("commentNetwork"))
        trace = data.get("videoTrace")
        if job["kind"] == "video" and phase == "collector_started" and isinstance(trace, dict):
            step = trace.get("step")
            previous = job.get("video_trace", {}).get("step", -1)
            if type(step) is int and (previous < step or (final_trace and previous == step)):
                job["video_trace"] = {
                    "step": step,
                    "panel_open_or_data": trace.get("panelOpened") is True,
                    "visibility": trace.get("visibility") if trace.get("visibility") in ("hidden", "visible") else "unknown",
                    "comments_seen": trace.get("commentsSeen") if type(trace.get("commentsSeen")) is int else None,
                    "comment_responses": trace.get("commentResponses") if type(trace.get("commentResponses")) is int else None,
                }
                for source, target in (("entrySeen", "entry_seen"), ("panelSeen", "panel_seen")):
                    if type(trace.get(source)) is bool:
                        job["video_trace"][target] = trace[source]
                for source, target in (("commentPagesParsed", "pages_parsed"), ("commentJsonFailures", "json_failures")):
                    if type(trace.get(source)) is int and trace[source] >= 0:
                        job["video_trace"][target] = trace[source]
                if trace.get("panelState") in {
                    "panel_open", "button_clicked", "wrapper_clicked", "awaiting_panel", "control_missing",
                }:
                    job["video_trace"]["panel_state"] = trace["panelState"]
                    for field in ("clickCount", "renderedTopLevel"):
                        if type(trace.get(field)) is int and trace[field] >= 0:
                            job["video_trace"][field] = trace[field]
                    if trace.get("controlSource") in {
                        "structure", "structure_wrapper", "text_tab", "aria_label", "svg_icon",
                    }:
                        job["video_trace"]["control_source"] = trace["controlSource"]
                    if trace["panelState"] == "control_missing" and trace.get("clickCount") == 0:
                        job.setdefault("entry_missing_since", time.monotonic())
                    else:
                        job.pop("entry_missing_since", None)
                changed = True
        if changed:
            self.save_status()

    def observe_comment_network(self, job, event):
        if job["kind"] != "video" or not isinstance(event, dict):
            return False
        request_id = event.get("id")
        kind = event.get("kind")
        phase = event.get("event")
        if (not isinstance(request_id, str) or not request_id.isascii() or not request_id.isdecimal()
                or len(request_id) > 30 or kind not in ("list", "reply")
                or phase not in ("requested", "response", "completed", "failed")):
            return False
        requests = job.setdefault("comment_network", {})
        if request_id not in requests and len(requests) >= 10000:
            return False
        request = requests.setdefault(request_id, {"kind": kind, "events": set()})
        previous = dict(request)
        request["events"] = request["events"] | {phase}
        status = event.get("status")
        if phase in ("response", "completed") and type(status) is int and 100 <= status <= 599:
            request["status"] = status
        error = event.get("error")
        if phase == "failed" and isinstance(error, str) and error.startswith("net::ERR_"):
            name = error.removeprefix("net::ERR_")
            if 0 < len(name) <= 70 and name.isascii() and name.replace("_", "").isalnum():
                request["error"] = error
        return previous != request

    def comment_diagnostics(self, job):
        trace = job.get("video_trace", {})
        requests = list(job.get("comment_network", {}).values())
        statuses = Counter(request["status"] for request in requests if "status" in request)
        errors = Counter(request.get("error", "unknown") for request in requests if "failed" in request["events"])
        entry_seen = trace.get("entry_seen", trace.get("clickCount", 0) > 0)
        panel_seen = trace.get("panel_seen", trace.get("panel_open_or_data", False))
        parsed = trace.get("pages_parsed", 0)
        if parsed or job.get("checkpoint_number"):
            stage = "body_parsed"
        elif any(200 <= code < 300 for code in statuses):
            stage = "http_success_without_parsed_page"
        elif statuses or errors:
            stage = "request_failed_or_rejected"
        elif requests:
            stage = "request_seen_without_response"
        elif panel_seen:
            stage = "panel_open_without_request"
        elif entry_seen:
            stage = "entry_seen_without_panel"
        elif trace:
            stage = "entry_missing"
        else:
            stage = "waiting_for_page_trace"
        return {
            "stage": stage, "entry_seen": entry_seen, "panel_open": panel_seen,
            "list_requests": sum(request["kind"] == "list" for request in requests),
            "reply_requests": sum(request["kind"] == "reply" for request in requests),
            "http_statuses": dict(sorted(statuses.items())), "network_errors": dict(errors),
            "json_responses": trace.get("comment_responses"),
            "json_failures": trace.get("json_failures"),
            "pages_parsed": parsed, "checkpoints": job.get("checkpoint_number", 0),
        }

    def retry_missing_entry(self):
        job = self.current
        if (self.stop_requested or self.paused or not job or job["kind"] != "video"
                or job.get("entry_retry_of") or job["checkpoint_number"]
                or job.get("comment_network")
                or "entry_missing_since" not in job or time.monotonic() + 30 >= self.deadline):
            return False
        timestamp = local_collect.now()
        if not collect.checkpoint_comments(self.db, job["video"], job["attempt_id"], [],
                                           timestamp, JOB_TIMEOUT_SECONDS + 30):
            return False
        previous_id = job["id"]
        diagnosis = self.comment_diagnostics(job)
        job.update(id=secrets.token_hex(12), entry_retry_of=previous_id, phase="assigned",
                   expires=time.monotonic() + JOB_TIMEOUT_SECONDS)
        job.pop("video_trace", None)
        job.pop("entry_missing_since", None)
        self.status["last_entry_retry"] = {"url": job["url"], "at": timestamp, "diagnostics": diagnosis}
        collect.log(f"{job['video']['video_id']} comment entry missing; reloading once")
        self.save_status()
        return True

    def record_failure(self, outcome, detail, http_status=None, retry_after=None):
        job = self.current
        timestamp = local_collect.now()

        def persist():
            return access_guard.record(self.db, job, outcome, detail, timestamp, http_status, retry_after)

        if job["kind"] == "video":
            diagnosis = self.comment_diagnostics(job)
            self.status["last_comment_diagnostics"] = {"url": job["url"], **diagnosis}
            detail += " | comment_diagnostics=" + json.dumps(diagnosis, ensure_ascii=False)
            collect.finish(self.db, job["video"], job["attempt_id"],
                           Result(outcome, detail=detail), timestamp, on_finish=persist)
        else:
            with collect.transaction(self.db):
                retry_until = persist()
                self.fail_discovery(outcome, detail, retry_until)
            collect.log(f"{job['kind']} {outcome} {detail}")
        self.current = None
        self.export_pending = True
        guard = access_guard.state(self.db)
        self.status["last_failure"] = {
            "url": job["url"], "outcome": outcome, "http_status": http_status, "detail": detail,
        }
        if guard["pause_reason"]:
            self.paused = True
            self.status.update(status="attention_required", detail=guard["pause_reason"])
        elif outcome == "rate_limited":
            self.status["status"] = "rate_limit_wait"
        elif access_guard.denial_until(self.db) and timestamp < access_guard.denial_until(self.db):
            self.status["status"] = "access_backoff"

    def fail_discovery(self, status, detail="", retry_until=None):
        job = self.current
        stamp = local_collect.now()
        self.db.execute('INSERT OR IGNORE INTO discovery_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (job['id'],job['kind'],job['seed'],job['started_at'],stamp,status,
                         job['targetDepth'],0,0,0,0,0,json.dumps({"detail": detail})))
        self.db.execute('''INSERT INTO discovery_progress(kind,seed,next_due,last_status)
          VALUES (?,?,?,?) ON CONFLICT(kind,seed) DO UPDATE SET next_due=excluded.next_due,last_status=excluded.last_status''',
                        (job['kind'],job['seed'],retry_until or collect.after(stamp,minutes=30),status))
        self.active_seeds.pop(job['kind'],None)

    def discovery_job(self, kind):
        stamp = local_collect.now()
        seed = self.active_seeds.get(kind)
        state = None
        if seed:
            state = self.db.execute('SELECT * FROM discovery_progress WHERE kind=? AND seed=?',(kind,seed)).fetchone()
            if state and state['next_due'] and state['next_due'] > stamp:
                seed = None
        if not seed and kind == 'profile':
            row = self.db.execute('''SELECT v.account FROM videos v
              LEFT JOIN discovery_progress p ON p.kind='profile' AND p.seed=v.account
              WHERE v.material IN ('chips','crumbs','chunks','mixed_raw')
              AND (p.next_due IS NULL OR p.next_due<=?)
              AND NOT EXISTS (SELECT 1 FROM chrome_access_events e WHERE e.http_status=403
                              AND e.url='https://www.tiktok.com/' || v.account)
              GROUP BY v.account ORDER BY COALESCE(p.last_checked,''),COUNT(DISTINCT v.video_id) DESC,v.account LIMIT 1''',(stamp,)).fetchone()
            seed = row['account'] if row else None
        if not seed and kind == 'search':
            denied = access_guard.denied_urls(self.db)
            words = list(dict.fromkeys(word for group in collect.SEARCH_GROUPS for word in group))
            words.extend(INTERNATIONAL_SEARCH_WORDS)
            ranked=[]
            for index,candidate in enumerate(words):
                if "https://www.tiktok.com/search/video?q=" + quote(candidate) in denied:
                    continue
                row = self.db.execute("SELECT * FROM discovery_progress WHERE kind='search' AND seed=?",(candidate,)).fetchone()
                if row and row['next_due'] and row['next_due'] > stamp:
                    continue
                legacy=self.db.execute('SELECT 1 FROM local_searches WHERE keyword=? LIMIT 1',(candidate,)).fetchone()
                ranked.append(((bool(row),bool(legacy),row['last_checked'] or '' if row else '',index),candidate))
            if ranked:
                seed=min(ranked)[1]
        if not seed:
            return None
        state = self.db.execute('SELECT * FROM discovery_progress WHERE kind=? AND seed=?',(kind,seed)).fetchone()
        depth = state['depth'] if state else 0
        self.active_seeds[kind] = seed
        url = ('https://www.tiktok.com/' + seed if kind == 'profile' else
               'https://www.tiktok.com/search/video?q=' + quote(seed))
        maximum = self.options.get('max_discovery_scrolls',80)
        return dict(id=secrets.token_hex(12),kind=kind,url=url,seed=seed,keyword=seed,
                    scrolls=self.options['search_scrolls'],resumeDepth=depth,
                    targetDepth=min(maximum,depth+self.options['search_scrolls']),
                    floor=self.options['history_floor'],started_at=stamp,
                    expires=time.monotonic()+JOB_TIMEOUT_SECONDS)

    def allocate_video(self, video_id=None):
        if len(self.seen)>=self.options['max_video_tasks']:
            return False
        job=local_collect.next_local_job(
            self.db, self.cfg, self.seen, self.account_counts,
            lease_seconds=JOB_TIMEOUT_SECONDS + 30, published_since=self.history_floor,
            source=collect.CHROME_FULL, video_id=video_id,
        )
        if not job:
            return False
        video,attempt_id,_=job
        self.seen.add(video['video_id'])
        self.unique_videos.add(video['video_id'])
        self.video_streak+=1
        self.current = dict(id=secrets.token_hex(12), kind="video", url=video["url"],
                            video=dict(video), attempt_id=attempt_id,
                            expires=time.monotonic() + JOB_TIMEOUT_SECONDS,
                            coverage=Coverage(), checkpoint_number=0)
        return True

    def metadata_job(self):
        stamp=local_collect.now()
        row=self.db.execute('''SELECT o.* FROM discovery_observations o
          LEFT JOIN videos v ON v.video_id=o.video_id
          LEFT JOIN discovery_progress p ON p.kind='metadata' AND p.seed=o.video_id
          LEFT JOIN candidate_reviews r ON r.video_id=o.video_id
          WHERE o.kind='profile' AND o.reason='missing_publish_time' AND v.video_id IS NULL
          AND (r.decision IS NULL OR r.decision!='exclude')
          AND (p.next_due IS NULL OR p.next_due<=?)
          AND NOT EXISTS (SELECT 1 FROM chrome_access_events e WHERE e.http_status=403 AND e.url=o.url)
          AND NOT EXISTS (SELECT 1 FROM discovery_observations newer WHERE newer.video_id=o.video_id AND newer.published_at IS NOT NULL)
          GROUP BY o.video_id ORDER BY o.video_id DESC LIMIT 1''',(stamp,)).fetchone()
        if not row:
            return None
        return dict(id=secrets.token_hex(12),kind='metadata',seed=row['video_id'],
                    video_id=row['video_id'],account=row['account'],url=row['url'],
                    scrolls=0,targetDepth=0,resumeDepth=0,floor=self.options['history_floor'],
                    started_at=stamp,expires=time.monotonic()+JOB_TIMEOUT_SECONDS)

    def restore(self, previous):
        end=datetime.fromisoformat(collect.utc(previous['planned_end_at']))
        remaining=(end-datetime.now(UTC)).total_seconds()
        if not 0<remaining<=24*60*60:
            raise ValueError('Previous collection window expired or invalid')
        start=collect.utc(previous['started_at'])
        elapsed=(datetime.now(UTC)-datetime.fromisoformat(start)).total_seconds()
        batch_start=collect.after(start,seconds=int(elapsed//1800)*1800)
        self.deadline=time.monotonic()+remaining
        self.continuous=True
        self.batch_number=int(elapsed//1800)+1
        self.batch_started=time.monotonic()-elapsed%1800
        attempts = self.db.execute(
            """SELECT a.video_id, a.started_at, v.account FROM attempts a
               JOIN videos v ON v.video_id = a.video_id
               WHERE a.source IN ('local_browser', ?) AND a.started_at >= ?""",
            (collect.CHROME_FULL, start),
        ).fetchall()
        batch_attempts = [row for row in attempts if row["started_at"] >= batch_start]
        self.seen = {row["video_id"] for row in batch_attempts}
        self.account_counts.clear()
        for row in batch_attempts:
            account = row["account"]
            self.account_counts[account] = self.account_counts.get(account, 0) + 1
        self.unique_videos={r['video_id'] for r in attempts}
        self.prior_attempts=len(attempts)-len(self.seen)
        for kind in ('search','profile','metadata'):
            all_count=self.db.execute('SELECT COUNT(*) FROM discovery_pages WHERE kind=? AND started_at>=?',(kind,start)).fetchone()[0]
            batch_count=self.db.execute('SELECT COUNT(*) FROM discovery_pages WHERE kind=? AND started_at>=?',(kind,batch_start)).fetchone()[0]
            if kind=='search':
                self.searches=batch_count
                self.prior_searches=all_count-batch_count
            elif kind=='profile':
                self.profile_pages=batch_count
                self.prior_profile_pages=all_count-batch_count
            else:
                self.metadata_pages=batch_count
                self.prior_metadata_pages=all_count-batch_count
        self.status.update({key:previous[key] for key in ('started_at','planned_end_at','duration_minutes','baseline_comments','baseline_videos')})
        self.status.update(continuous=True,resumed_at=local_collect.now())

    def job(self):
        self.last_contact = local_collect.now()
        self.expire_job()
        if self.paused or self.stop_requested:
            self.save_status()
            return None
        if self.current is None and time.monotonic() + self.minimum_job_window >= self.deadline:
            self.status["status"] = "time_limit_reached"
            self.save_status()
            return None
        if self.current:
            missing_since = self.current.get("entry_missing_since")
            if missing_since is not None and time.monotonic() - missing_since >= COMMENT_ENTRY_RETRY_SECONDS:
                self.retry_missing_entry()
            return self.public_job()
        retry_until = access_guard.state(self.db)["rate_until"]
        if retry_until and self.last_contact < retry_until:
            self.status["status"] = "rate_limit_wait"
            self.save_status()
            return None
        denial_until = access_guard.denial_until(self.db)
        if denial_until and self.last_contact < denial_until:
            self.status["status"] = "access_backoff"
            self.save_status()
            return None
        if self.probe_video_id:
            if not self.seen and self.allocate_video(self.probe_video_id):
                self.status["status"] = "running"
            else:
                self.status["status"] = "completed"
                if not self.seen:
                    self.status["detail"] = "Probe video is not eligible in the current queue"
                    for video, state, due, _ in collect.queue(
                        self.db, self.cfg, self.last_contact, full_chrome=True
                    ):
                        if video["video_id"] == self.probe_video_id and state in {"waiting", "retry_wait"}:
                            self.status.update(status="probe_retry_wait", detail=f"Probe video due at {due}")
                            break
            if self.current:
                self.status.pop("detail", None)
            self.save_status()
            return self.public_job()
        if self.continuous and time.monotonic() - self.batch_started >= 30 * 60:
            self.prior_searches += self.searches
            self.prior_profile_pages += self.profile_pages
            self.prior_metadata_pages += self.metadata_pages
            self.prior_attempts += len(self.seen)
            self.seen.clear()
            self.account_counts.clear()
            self.searches = 0
            self.profile_pages = 0
            self.metadata_pages = 0
            self.batch_started = time.monotonic()
            self.batch_number += 1
        due = self.video_streak >= 3 and any(
            video["stage"] == 2 and state == "ready" and video["video_id"] not in self.seen
            and collect.in_scope(video, self.history_floor)
            for video, state, _, _ in collect.queue(self.db, self.cfg, local_collect.now(), full_chrome=True)
        )
        if self.video_streak<3 or due:
            self.allocate_video()
        if self.current is None:
            if self.metadata_pages<self.options.get('metadata_checks',12):
                self.current=self.metadata_job()
                if self.current:
                    self.metadata_pages+=1
                    self.video_streak=0
            kinds = ['search','profile'] if self.discovery_turn % 3 != 2 else ['profile','search']
            for kind in kinds:
                if self.current:
                    break
                if kind == 'search' and self.searches >= self.options['max_searches']:
                    continue
                if kind == 'profile' and self.profile_pages >= self.options['profile_checks']:
                    continue
                self.current = self.discovery_job(kind)
                if self.current:
                    self.discovery_turn += 1
                    if kind == 'search':
                        self.searches += 1
                    else:
                        self.profile_pages += 1
                    self.video_streak=0
                    break
        if self.current is None:
            self.allocate_video()
        self.status["status"] = "running" if self.current else (
            "waiting_next_batch" if self.continuous else "completed"
        )
        self.save_status()
        return self.public_job()

    def public_job(self):
        if not self.current:
            return None
        return {key: self.current[key] for key in ("id", "kind", "url", "scrolls",'resumeDepth','targetDepth','floor') if key in self.current}

    def checkpoint(self, data):
        job = self.active_job(data)
        if job["kind"] != "video":
            raise ValueError("Comment checkpoints require a video task")
        number = data["number"]
        if type(number) is not int or not 0 < number <= job["checkpoint_number"] + 1:
            raise ValueError("Invalid checkpoint sequence")
        coverage = job["coverage"]
        if number <= job["checkpoint_number"]:
            return {"accepted": True, "complete": coverage.complete()}
        raw = data["comments"]
        if not isinstance(raw, list) or len(raw) > 200:
            raise ValueError("Invalid comment checkpoint")
        try:
            coordinates = page_coordinates(data)
        except ValueError:
            coordinates = None
        video = job["video"]
        mapped = comment_items({"comments": raw}, video["video_id"], video["url"],
                               coordinates[0] if coordinates else None)
        result = normalize(mapped, video["video_id"], None)
        verified = (data.get("valid") is True and len(mapped) == len(raw)
                    and result.status in {"success", "empty_unverified"})
        if not collect.checkpoint_comments(self.db, video, job["attempt_id"], result.rows,
                                           local_collect.now(), JOB_TIMEOUT_SECONDS + 30):
            raise StaleJob("Attempt lease is no longer active")
        coverage.saw_comment_rows |= bool(raw)
        if coordinates:
            if coordinates[0] is None:
                verified &= all(row["parent_id"] is None for row in result.rows)
            ids = {row["comment_id"] for row in result.rows}
            accepted = (row for row in raw if isinstance(row, dict) and str(row.get("cid")) in ids)
            coverage.observe(coordinates, accepted, valid=verified, final=data.get("final") is True)
        job["checkpoint_number"] = number
        job["expires"] = time.monotonic() + JOB_TIMEOUT_SECONDS
        self.export_pending = True
        return {"accepted": True, "complete": coverage.complete()}

    def result(self, data):
        # A refusal reported by the original load still applies after its retry was assigned.
        if (self.current and self.current.get("entry_retry_of") and
                data.get("jobId") == self.current["entry_retry_of"] and
                data.get("status") in {"http_error", "blocked"}):
            data = data | {"jobId": self.current["id"]}
        job = self.active_job(data)
        if data.get("status") not in {"observed", "blocked", "error", "http_error", "navigation_error"}:
            raise ValueError("Unexpected page observation status")
        diagnostics = data.get("diagnostics")
        if isinstance(diagnostics, dict):
            self.status["last_page_diagnostics"] = {
                key: diagnostics[key] for key in
                ("commentResponses", "commentStatusCodes", "searchResponses", "commentButtons", "urlMatches",
                 'protocol','reachedDepth','stopReason','scrollMoved','newObservedVideos','resumeMode',
                 'profileVideoLinks','profileState','visibility')
                if key in diagnostics
            }
        if data["status"] == "blocked":
            self.record_failure("access_paused", "Chrome page requires login or verification")
        elif data["status"] == "http_error":
            code = data.get("httpStatus")
            scope = data.get("scope")
            if code not in {401, 403, 429} or scope not in {"navigation", "data_request"}:
                raise ValueError("Unexpected HTTP observation")
            outcome = {401: "access_paused", 403: "access_denied", 429: "rate_limited"}[code]
            self.record_failure(outcome, f"HTTP {code} ({scope})", code, data.get("retryAfter"))
        elif data["status"] == "navigation_error":
            self.record_failure("timeout", "page_load_failed (Chrome navigation error)")
        elif data['status'] == 'error':
            if job['kind'] == 'video':
                reason = diagnostics.get("stopReason") if isinstance(diagnostics, dict) else None
                detail = reason if reason in ("navigation_changed", "page_pace_unavailable",
                                              "comment_checkpoint_unavailable") else "Local page collection failed"
                self.record_failure("error", detail)
            else:
                self.record_failure("error", "Local discovery failed")
        elif job["kind"] == "video":
            video = job["video"]
            coverage = job["coverage"]
            if isinstance(data.get("videoTrace"), dict):
                self.progress({"jobId": job["id"], "phase": "collector_started",
                               "videoTrace": data["videoTrace"]}, final_trace=True)
            reason = diagnostics.get("stopReason") if isinstance(diagnostics, dict) else None
            if reason == "comment_panel_not_open" and self.retry_missing_entry():
                return
            if coverage.complete():
                result = Result("success" if coverage.roots else "empty_confirmed",
                                detail="Chrome full pagination verified")
            else:
                detail = "Chrome comment panel did not open" if reason == "comment_panel_not_open" else (
                    "Chrome pagination or replies not verified"
                )
                detail += " | comment_diagnostics=" + json.dumps(self.comment_diagnostics(job), ensure_ascii=False)
                result = Result("coverage_incomplete", detail=detail)
            if not collect.finish(self.db, video, job["attempt_id"], result, local_collect.now(),
                                  on_finish=lambda: access_guard.record_success(self.db) if coverage.pages else None):
                self.current = None
                self.export_pending = True
                raise StaleJob("Attempt lease is no longer active")
            self.status["last_comment_diagnostics"] = {"url": job["url"], **self.comment_diagnostics(job)}
        else:
            raw = data.get("videos")
            if not isinstance(raw, list) or len(raw) > 1000:
                raise ValueError("Invalid search observation")
            if not isinstance(diagnostics,dict) or diagnostics.get('protocol') != int(PROTOCOL):
                self.status['status']='extension_update_required'
                raise ValueError('Extension update required')
            items = video_items(raw,keep_incomplete=True)
            if job['kind']=='metadata':
                items=[item for item in items if collect.canonical(item['webVideoUrl'])[0]==job['video_id'] and item.get('createTimeISO')]
            if job['kind']=='metadata' and not items:
                with collect.transaction(self.db):
                    self.fail_discovery('metadata_missing')
                outcome={'cooldown':True}
            else:
                outcome = discovery.record_page(
                    self.db, self.cfg, job, items, self.options, diagnostics, local_collect.now(),
                    on_finish=lambda: access_guard.record_success(self.db) if items else None,
                )
            if outcome['cooldown']:
                self.active_seeds.pop(job['kind'],None)
        self.current = None
        self.export_pending = True
        self.last_contact = local_collect.now()
        self.save_status()


def handler_for(bridge, token):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            return

        def authenticated(self):
            supplied = self.headers.get("Authorization", "")
            origin = self.headers.get("Origin", "")
            return hmac.compare_digest(supplied, "Bearer " + token) and (
                not origin or origin.startswith("chrome-extension://")
            ) and self.headers.get("Host") == "127.0.0.1:8766"

        def respond(self, status, body):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.authenticated() or self.path != "/job":
                self.respond(403, {"error": "Request rejected"})
                return
            with bridge.lock:
                version = self.headers.get("X-Chengxian-Version", "")[:30]
                required_version = bridge.options.get("probe_extension_version")
                if self.headers.get('X-Chengxian-Protocol') != PROTOCOL or (
                    required_version and version != required_version
                ):
                    bridge.last_contact=local_collect.now()
                    bridge.status['status']='extension_update_required'
                    bridge.save_status()
                    self.respond(200,{'job':None,'upgradeRequired':True})
                    return
                bridge.status['extension_protocol']=int(PROTOCOL)
                bridge.status["extension_version"] = version or None
                self.respond(200, {"job": bridge.job()})

        def do_POST(self):
            if (not self.authenticated() or self.headers.get("X-Chengxian-Protocol") != PROTOCOL
                    or self.path not in {"/result", "/progress", "/comments", "/stop"}):
                self.respond(403, {"error": "Request rejected"})
                return
            if self.path == "/stop":
                if self.headers.get("Origin"):
                    self.respond(403, {"error": "Use the local command to stop"})
                    return
                with bridge.lock:
                    bridge.stop_requested = True
                    self.respond(200, {"accepted": True})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 5_000_000:
                    raise ValueError("Invalid payload size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                with bridge.lock:
                    response = {"accepted": True}
                    if self.path == "/progress":
                        bridge.progress(data)
                    elif self.path == "/comments":
                        response = bridge.checkpoint(data)
                    else:
                        bridge.result(data)
                self.respond(200, response)
            except StaleJob:
                self.respond(409, {"error": "Unknown or expired job"})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "Invalid page observation"})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--stop", action="store_true", help="Stop the active local run and export its results")
    parser.add_argument("--duration-minutes", type=int, help="One run's time window, from 1 to 1440 minutes")
    parser.add_argument("--scheduled", action="store_true", help="Skip a scheduled launch during a persistent access pause")
    parser.add_argument('--resume-state',type=Path,help='Resume the original deadline and baseline from a saved local status file')
    parser.add_argument('--profile-first',action='store_true',help='Prioritize one known raw-material account page for validation')
    parser.add_argument("--probe-video", help="Collect one eligible video and exit without scheduling other jobs")
    parser.add_argument("--clear-access-pause", action="store_true",
                        help="Resume after manually checking the login or access issue")
    args = parser.parse_args()
    token = setup_token()
    if args.stop:
        with closing(HTTPConnection("127.0.0.1", 8766, timeout=15)) as connection:
            try:
                connection.request("POST", "/stop", body=b"", headers={
                    "Authorization": "Bearer " + token, "X-Chengxian-Protocol": PROTOCOL,
                })
                response = connection.getresponse()
            except ConnectionRefusedError:
                print("No active local Chrome collector.")
                return
            if response.status != 200:
                raise ValueError("Local stop request was rejected")
            print("Stop requested. The collector is saving and exporting its results.")
        return
    if args.setup:
        print("Local extension pairing configured. Token stays on this computer.")
        return
    options = local_collect.settings()
    if args.duration_minutes is not None:
        if not 1 <= args.duration_minutes <= 1440:
            raise ValueError("--duration-minutes must be between 1 and 1440")
        options = options | {"max_run_minutes": args.duration_minutes, "continuous": True}
    if args.probe_video:
        manifest = json.loads((ROOT / "chrome_extension" / "manifest.json").read_text(encoding="utf-8"))
        options = options | {"max_run_minutes": args.duration_minutes or 5, "continuous": False,
                             "probe_video_id": item_id(args.probe_video),
                             "probe_extension_version": manifest["version"]}
    with local_collect.run_lock(), closing(sqlite3.connect(
        ROOT / "data" / "comments.sqlite", timeout=20, isolation_level=None, check_same_thread=False
    )) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(collect.SCHEMA + local_collect.LOCAL_SCHEMA)
        with collect.transaction(db):
            access_guard.migrate_denial_pause(db)
        if args.scheduled and access_guard.state(db)["pause_reason"]:
            print("Scheduled collection is paused. See the Chrome status and access events.", flush=True)
            return
        if args.clear_access_pause:
            with collect.transaction(db):
                db.execute("UPDATE chrome_access_state SET pause_reason='',reviewed_at=? WHERE id=1",
                           (local_collect.now(),))
                db.execute("UPDATE chrome_denial_backoff SET retry_until=NULL,streak=0 WHERE id=1")
        for attempt in db.execute("""SELECT id,video_id,stage FROM attempts
            WHERE source IN ('local_browser', ?) AND status='running'""", (collect.CHROME_FULL,)).fetchall():
            video = dict(db.execute("SELECT * FROM videos WHERE video_id=?", (attempt["video_id"],)).fetchone())
            video["stage"] = attempt["stage"]
            collect.finish(db,video,attempt['id'],Result('interrupted',detail='Previous local worker exited; exclusive lock recovered'),local_collect.now())
        bridge = Bridge(db, collect.config(ROOT / "config.json"), options)
        if args.resume_state:
            bridge.restore(json.loads(args.resume_state.read_text(encoding='utf-8-sig')))
        if args.profile_first:
            bridge.video_streak=3
            bridge.discovery_turn=2
        bridge.save_status()
        last_heartbeat = time.monotonic()
        with ThreadingHTTPServer(("127.0.0.1", 8766), handler_for(bridge, token)) as server:
            server.timeout = 1
            print("Local Chrome collector listening on 127.0.0.1:8766. Waiting for extension.", flush=True)
            while time.monotonic() < bridge.deadline and not bridge.paused and not bridge.stop_requested:
                server.handle_request()
                if bridge.status["status"] == "completed" or (
                    bridge.status["status"] == "time_limit_reached" and not bridge.continuous
                ):
                    break
                if time.monotonic() - last_heartbeat >= 30:
                    with bridge.lock:
                        bridge.expire_job()
                        if bridge.export_pending and bridge.current is None:
                            bridge.publish_exports()
                        bridge.save_status()
                    last_heartbeat = time.monotonic()
                if (not bridge.continuous and bridge.last_contact is None
                        and time.monotonic() > bridge.started_monotonic + 120):
                    bridge.status.update(status="attention_required", detail="Chrome extension has not connected")
                    break
            with bridge.lock:
                if bridge.current and bridge.current["kind"] == "video":
                    collect.finish(db, bridge.current["video"], bridge.current["attempt_id"],
                                   Result("interrupted", detail="Local bridge stopped before confirmation"),
                                   local_collect.now())
                elif bridge.current:
                    with collect.transaction(db):
                        bridge.fail_discovery('interrupted')
                bridge.current = None
                if bridge.stop_requested:
                    bridge.status["status"] = "stopped_by_user"
                elif time.monotonic() >= bridge.deadline or bridge.status["status"] == "running":
                    bridge.status["status"] = "time_limit_reached"
                bridge.status["finished_at"] = local_collect.now()
                bridge.publish_exports()
                bridge.save_status()
                runs = ROOT / "data" / "runs"
                runs.mkdir(exist_ok=True)
                name = datetime.fromisoformat(bridge.status["started_at"]).strftime("%Y%m%dT%H%M%SZ")
                (runs / f"chrome-{name}.json").write_text(
                    json.dumps(bridge.status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                if bridge.export_pending:
                    raise ValueError("Final report export failed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Local Chrome bridge failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
