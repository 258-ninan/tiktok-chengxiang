import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import chrome_bridge
import access_guard
import collect
import local_collect


def checkpoint(bridge, job, comments, *, number=1, parent=None, cursor="0", next_cursor=None):
    return bridge.checkpoint(dict(jobId=job["id"], number=number, parentId=parent, cursor=cursor,
                                  hasMore=next_cursor is not None, nextCursor=next_cursor,
                                  comments=comments, valid=True, final=True))


class ChromeBridgeWorkflow(unittest.TestCase):
    def test_existing_chrome_result_is_atomic_and_repeated_result_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            (root / "data").mkdir()
            with closing(sqlite3.connect(root / "test.sqlite", isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                db.executescript(collect.SCHEMA + local_collect.LOCAL_SCHEMA)
                vid = "9000000000000000001"
                url = f"https://www.tiktok.com/@fixture/video/{vid}"
                stamp = local_collect.now()
                cfg = collect.config(Path("config.json"))
                collect.store_seeds(db, cfg, [
                    (vid, url, "@fixture", "2026-01-02T00:00:00+00:00", stamp,
                     "chips", "unknown", "", {"url": url, "keywords": "رقائق العود"})
                ])
                with patch.object(chrome_bridge, "ROOT", root), patch.object(local_collect, "export_local"):
                    bridge = chrome_bridge.Bridge(db, cfg, local_collect.settings())
                    job = bridge.job()
                    self.assertEqual(job["kind"], "video")
                    checkpoint(bridge, job, [
                        {"cid": "123", "aweme_id": vid, "reply_id": "0",
                         "reply_total": 0,
                         "text": "كم سعر الكيلو؟", "create_time": 1793000000,
                         "user": {"unique_id": "buyer"}},
                    ])
                    data = dict(jobId=job["id"], status="observed")
                    bridge.result(data)
                    self.assertEqual(db.execute("SELECT stage FROM videos").fetchone()[0], 2)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
                    with self.assertRaises(ValueError):
                        bridge.result(data)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
                    bridge.deadline = time.monotonic() + 50
                    self.assertIsNone(bridge.job())
                    self.assertEqual(bridge.status["status"], "time_limit_reached")
                    bridge.continuous = True
                    bridge.deadline = time.monotonic() + 4 * 60 * 60
                    bridge.searches = bridge.options["max_searches"]
                    bridge.profile_pages = bridge.options['profile_checks']
                    self.assertIsNone(bridge.job())
                    self.assertEqual(bridge.status["status"], "waiting_next_batch")
                    bridge.batch_started = time.monotonic() - 30 * 60 - 1
                    resumed = bridge.job()
                    self.assertEqual(resumed["kind"], "search")
                    self.assertEqual(bridge.batch_number, 2)
                    self.assertEqual(bridge.status["attempted_videos"], 1)

    def test_pairing_file_is_stable_and_never_requires_tiktok_credentials(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            (root / "chrome_extension").mkdir()
            with patch.object(chrome_bridge, "ROOT", root):
                token = chrome_bridge.setup_token()
                self.assertGreaterEqual(len(token), 32)
                self.assertEqual(token, chrome_bridge.setup_token())


class ChromeAccessWorkflow(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "data").mkdir()
        self.db = sqlite3.connect(root / "test.sqlite", isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(collect.SCHEMA + local_collect.LOCAL_SCHEMA)
        self.stamp = datetime.now(UTC).replace(microsecond=0)
        self.enterContext(patch.object(chrome_bridge, "ROOT", root))
        self.enterContext(patch.object(local_collect, "export_local"))
        self.clock = self.enterContext(patch.object(local_collect, "now", side_effect=lambda: self.stamp.isoformat()))
        self.cfg = collect.config(Path("config.json"))
        self.options = local_collect.settings()
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)

    def seed(self, count, accounts=("@fixture",)):
        items = []
        for index in range(count):
            video_id = str(9000000000000000100 + index)
            account = accounts[index % len(accounts)]
            url = f"https://www.tiktok.com/{account}/video/{video_id}"
            items.append((video_id, url, account, "2026-01-02T00:00:00+00:00",
                          self.stamp.isoformat(), "chips", "unknown", "", {"url": url}))
        collect.store_seeds(self.db, self.cfg, items)

    def respond(self, job, code, retry_after=None):
        self.bridge.result({"jobId": job["id"], "status": "http_error", "httpStatus": code,
                            "scope": "navigation", "retryAfter": retry_after})

    def test_distinct_denials_back_off_across_restart_and_allow_a_probe(self):
        self.seed(4)
        first = self.bridge.job()
        self.respond(first, 403)
        self.assertFalse(self.bridge.paused)
        with self.assertRaises(chrome_bridge.StaleJob):
            self.respond(first, 403)
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)
        second = self.bridge.job()
        self.assertNotEqual(second["url"], first["url"])
        self.respond(second, 403)
        self.assertFalse(self.bridge.paused)
        third = self.bridge.job()
        self.respond(third, 403)
        self.assertFalse(self.bridge.paused)
        self.assertIsNone(self.bridge.job())
        self.assertEqual(self.bridge.status["status"], "access_backoff")
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)
        self.assertIsNone(self.bridge.job())
        states = [state for _, state, _, _ in collect.queue(self.db, self.cfg, self.stamp.isoformat())]
        self.assertEqual(states.count("access_denied"), 3)
        self.assertEqual(states.count("ready"), 1)
        self.stamp += timedelta(minutes=30)
        fourth = self.bridge.job()
        self.assertNotIn(fourth["url"], access_guard.denied_urls(self.db))
        self.respond(fourth, 403)
        self.assertEqual(access_guard.denial_until(self.db), (self.stamp + timedelta(hours=1)).isoformat())

    def test_rate_limit_holds_all_work_across_restart_and_backoff_grows(self):
        self.seed(1)
        self.respond(self.bridge.job(), 429, "30")
        self.assertEqual(access_guard.state(self.db)["rate_until"],
                         (self.stamp + timedelta(seconds=30)).isoformat())
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)
        self.assertIsNone(self.bridge.job())
        self.assertEqual(self.bridge.status["status"], "rate_limit_wait")
        self.stamp += timedelta(seconds=30)
        self.respond(self.bridge.job(), 429)
        self.assertEqual(access_guard.state(self.db)["rate_until"],
                         (self.stamp + timedelta(minutes=2)).isoformat())
        self.assertEqual(collect.queue(self.db, self.cfg, self.stamp.isoformat())[0][3], 0)
        date = self.stamp + timedelta(minutes=3)
        self.assertEqual(access_guard.retry_deadline(format_datetime(date, usegmt=True), self.stamp, 2), date)

    def test_denied_attempts_rotate_accounts_across_resume(self):
        self.seed(4, ("@fixture", "@fixture", "@second", "@third"))
        self.stamp = datetime.fromisoformat(self.bridge.status["started_at"]) + timedelta(milliseconds=1)
        first = self.bridge.job()
        self.assertIn("/@fixture/", first["url"])
        self.respond(first, 403)

        second = self.bridge.job()
        self.assertIn("/@second/", second["url"])
        self.respond(second, 403)

        previous = self.bridge.status.copy()
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)
        self.bridge.restore(previous)
        third = self.bridge.job()
        self.assertIn("/@third/", third["url"])
        self.respond(third, 403)
        self.assertIsNone(self.bridge.job())
        self.assertEqual(self.bridge.status["status"], "access_backoff")

    def test_login_pause_preserves_video_and_does_not_charge_retry(self):
        self.seed(1)
        job = self.bridge.job()
        self.bridge.result({"jobId": job["id"], "status": "blocked"})
        self.assertIsNone(self.bridge.job())
        self.assertTrue(chrome_bridge.Bridge(self.db, self.cfg, self.options).paused)
        video, state, _, attempts = collect.queue(self.db, self.cfg, self.stamp.isoformat())[0]
        self.assertEqual((video["stage"], state, attempts), (1, "ready", 0))

    def test_export_failure_cannot_reject_a_committed_result(self):
        self.seed(1)
        job = self.bridge.job()
        video_id = job["url"].rsplit("/", 1)[1]
        export = local_collect.export_local
        export.side_effect = PermissionError("Report is open")
        checkpoint(self.bridge, job, [
            {"cid": "123", "aweme_id": video_id, "text": "كم سعر الكيلو؟", "reply_id": "0", "reply_total": 0},
        ])
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        export.assert_not_called()
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "success")
        self.bridge.publish_exports()
        self.assertTrue(self.bridge.export_pending)
        self.assertIn("PermissionError", self.bridge.status["export_error"])
        export.side_effect = None
        self.bridge.publish_exports()
        self.assertFalse(self.bridge.export_pending)
        self.assertNotIn("export_error", self.bridge.status)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)

    def test_one_minute_run_can_dispatch_a_job(self):
        self.seed(1)
        bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options | {"max_run_minutes": 1})
        self.assertEqual(bridge.job()["kind"], "video")

    def test_legacy_out_of_range_video_is_not_dispatched(self):
        self.seed(1)
        self.db.execute("UPDATE videos SET published_at='2025-12-31T00:00:00+00:00'")
        self.assertEqual(self.bridge.job()["kind"], "search")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    def test_single_video_probe_stops_before_dispatching_other_work(self):
        self.seed(2)
        target = "9000000000000000101"
        bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options | {"probe_video_id": target})
        job = bridge.job()
        self.assertTrue(job["url"].endswith("/" + target))
        checkpoint(bridge, job, [])
        bridge.result({"jobId": job["id"], "status": "observed"})
        self.assertIsNone(bridge.job())
        self.assertEqual(bridge.status["status"], "completed")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_root_page_with_reply_preserves_comment_and_cannot_confirm_empty(self):
        self.seed(1)
        job = self.bridge.job()
        self.assertFalse(checkpoint(self.bridge, job, [
            {"cid": "123", "reply_id": "99", "text": "Reply without a captured parent"},
        ])["complete"])
        self.assertFalse(checkpoint(self.bridge, job, [], number=2)["complete"])
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "coverage_incomplete")
        self.assertEqual(self.db.execute("SELECT stage FROM videos").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT parent_id FROM comments").fetchone()[0], "99")

    def test_reply_checkpoint_prevents_empty_root_confirmation(self):
        self.seed(1)
        job = self.bridge.job()
        checkpoint(self.bridge, job, [{"cid": "123", "text": "Reply"}], parent="99")
        self.assertFalse(checkpoint(self.bridge, job, [], number=2)["complete"])
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "coverage_incomplete")

    def test_empty_multi_page_chain_cannot_confirm_empty(self):
        self.seed(1)
        job = self.bridge.job()
        self.assertFalse(checkpoint(self.bridge, job, [], next_cursor="1")["complete"])
        self.assertFalse(checkpoint(self.bridge, job, [], number=2, cursor="1")["complete"])
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "coverage_incomplete")

    def test_missing_comment_entry_reloads_once_without_a_second_attempt(self):
        self.seed(2)
        first = self.bridge.job()
        trace = dict(step=0, panelState="control_missing", clickCount=0)
        self.bridge.progress(dict(jobId=first["id"], phase="collector_started", videoTrace=trace))
        self.bridge.current["entry_missing_since"] -= chrome_bridge.COMMENT_ENTRY_RETRY_SECONDS + 1
        retry = self.bridge.job()
        self.assertNotEqual(retry["id"], first["id"])
        self.assertEqual(retry["url"], first["url"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        with self.assertRaises(chrome_bridge.StaleJob):
            self.bridge.result(dict(jobId=first["id"], status="observed"))
        self.bridge.progress(dict(jobId=retry["id"], phase="collector_started", videoTrace=trace))
        self.bridge.result(dict(jobId=retry["id"], status="observed",
                                diagnostics={"stopReason": "comment_panel_not_open"}))
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "coverage_incomplete")
        self.assertNotEqual(self.bridge.job()["url"], first["url"])

    def test_denial_from_original_load_still_blocks_retried_video(self):
        self.seed(1)
        first = self.bridge.job()
        self.bridge.progress(dict(jobId=first["id"], phase="collector_started",
                                  videoTrace={"step": 0, "panelState": "control_missing", "clickCount": 0}))
        self.bridge.result(dict(jobId=first["id"], status="observed",
                                diagnostics={"stopReason": "comment_panel_not_open"}))
        self.assertNotEqual(self.bridge.current["id"], first["id"])
        self.respond(first, 403)
        self.assertIsNone(self.bridge.current)
        self.assertIn(first["url"], access_guard.denied_urls(self.db))
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0], "access_denied")

    def test_comment_diagnostics_separate_request_http_and_page_parsing(self):
        self.seed(1)
        job = self.bridge.job()
        self.bridge.progress(dict(jobId=job["id"], phase="collector_started", videoTrace={
            "step": 0, "entrySeen": True, "panelSeen": True, "panelState": "panel_open",
        }))
        self.assertEqual(self.bridge.status["pending_comment_diagnostics"]["stage"], "panel_open_without_request")
        event = dict(id="123", kind="list", event="requested")
        for update in (event, event, event | {"event": "response", "status": 200},
                       event | {"event": "completed", "status": 200}):
            self.bridge.progress(dict(jobId=job["id"], phase="navigation_started", commentNetwork=update))
        evidence = self.bridge.status["pending_comment_diagnostics"]
        self.assertEqual(evidence["stage"], "http_success_without_parsed_page")
        self.assertEqual(evidence["list_requests"], 1)
        self.assertEqual(evidence["http_statuses"], {200: 1})
        self.bridge.result(dict(jobId=job["id"], status="observed", videoTrace={
            "step": 1, "entrySeen": True, "panelSeen": True, "commentJsonFailures": 1,
        }))
        self.assertEqual(self.bridge.status["last_comment_diagnostics"]["json_failures"], 1)
        self.assertIn("http_success_without_parsed_page",
                      self.db.execute("SELECT detail FROM attempts").fetchone()[0])

    def test_pagination_saves_partial_rows_and_finishes_only_after_replies(self):
        self.seed(1)
        job = self.bridge.job()
        roots = [{"cid": str(index), "text": "root", "reply_total": int(index == 1)}
                 for index in range(1, 41)]
        self.assertFalse(checkpoint(self.bridge, job, roots, next_cursor="40")["complete"])
        self.assertFalse(checkpoint(self.bridge, job, roots, next_cursor="40")["complete"])
        self.assertFalse(checkpoint(self.bridge, job, [], number=2, cursor="40")["complete"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 40)
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        first = self.db.execute("SELECT status,rows_received,new_comments FROM attempts").fetchone()
        self.assertEqual(tuple(first), ("coverage_incomplete", 40, 40))
        self.assertEqual(self.db.execute("SELECT stage FROM videos").fetchone()[0], 1)
        self.stamp += timedelta(minutes=16)
        self.bridge = chrome_bridge.Bridge(self.db, self.cfg, self.options)
        retry = self.bridge.job()
        checkpoint(self.bridge, retry, roots)
        self.assertTrue(checkpoint(self.bridge, retry, [{"cid": "99", "text": "reply"}],
                                   number=2, parent="1")["complete"])
        self.bridge.result({"jobId": retry["id"], "status": "observed"})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 41)
        self.assertEqual(self.db.execute("SELECT stage FROM videos").fetchone()[0], 2)

    def test_archived_sample_can_be_backfilled_once(self):
        self.seed(1)
        due = (self.stamp - timedelta(minutes=1)).isoformat()
        self.db.execute("UPDATE videos SET stage=3, followup_due=?, archived_at=?", (due, due))
        job = self.bridge.job()
        self.assertEqual(job["kind"], "video")
        self.assertTrue(checkpoint(self.bridge, job, [])["complete"])
        self.bridge.result({"jobId": job["id"], "status": "observed"})
        self.assertEqual(tuple(self.db.execute("SELECT stage,archived_at FROM videos").fetchone()), (3, due))
        self.assertEqual(collect.queue(self.db, self.cfg, self.stamp.isoformat(), full_chrome=True)[0][1], "archived")

    def test_timeout_uses_last_acknowledged_phase_and_preserves_retry_limit(self):
        self.seed(3)
        for phase, reason in (("navigation_started", "page_load_timeout"),
                              ("page_loaded", "extension_unresponsive"),
                              ("result_ready", "result_delivery_failed")):
            job = self.bridge.job()
            self.bridge.progress({"jobId": job["id"], "phase": phase})
            self.bridge.progress({"jobId": job["id"], "phase": "assigned"})
            self.bridge.current["expires"] = time.monotonic() - 1
            self.bridge.expire_job()
            attempt = self.db.execute("SELECT status,detail FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(attempt["status"], "timeout")
            self.assertIn(reason, attempt["detail"])
        self.assertEqual([row[3] for row in collect.queue(self.db, self.cfg, self.stamp.isoformat())], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
