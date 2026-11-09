import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import collect
from reports import classify
from sources import ApifySource, Result, canonical


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"
START = "2026-11-01T09:00:00Z"
DUE = "2026-11-04T09:00:00Z"
VID = "9000000000000000001"


class Workflow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "test.sqlite"
        self.out = Path(temp.name) / "export"
        self.cfg = collect.config(ROOT / "config.json")

    def cli(self, now, *args):
        return subprocess.check_output(
            [sys.executable, str(ROOT / "collect.py"), "--db", str(self.db), "--now", now,
             *map(str, args)], encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    def read(self, filename):
        with ZipFile(self.out / "reports.zip") as bundle, \
                io.TextIOWrapper(bundle.open(filename), encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def seed(self):
        return self.cli(START, "import", EXAMPLES / "seeds.demo.csv")

    def run_json(self, now):
        return self.cli(now, "run", "--source", "json", "--file", EXAMPLES / "comments.demo.json")

    def test_two_round_workflow(self):
        self.assertIn("11 discovery rows; 10 unique", self.seed())
        first = self.run_json(START)
        for failure in ("timeout", "partial", "empty_unverified", "error"):
            self.assertIn(f"round=1 {failure}", first)
        self.assertIn("Processed 0", self.run_json("2026-11-01T09:14:59Z"))
        self.assertIn("Processed 4", self.run_json("2026-11-01T09:15:00Z"))
        self.assertIn("Processed 0", self.run_json("2026-11-04T08:59:59Z"))
        self.assertEqual(self.cli(DUE, "status").count("round=2 ready"), 10)
        self.assertIn("Processed 10", self.run_json("2026-11-04T09:01:00Z"))
        self.cli("2026-11-05T09:00:00Z", "import", EXAMPLES / "seeds.demo.txt")
        self.assertIn("Processed 0", self.run_json("2026-11-05T09:00:00Z"))
        self.cli("2026-11-05T09:00:00Z", "export", self.out, "--annotations", EXAMPLES / "annotations.demo.csv")
        with ZipFile(self.out / "reports.zip") as bundle:
            stats = json.loads(bundle.read("stats.json"))
        self.assertEqual(stats["video_states"], {"archived": 10})
        self.assertEqual(stats["attempts_by_round"]["2"], {"success": 9, "empty_confirmed": 1})
        self.assertEqual(stats["comments"], 17)
        self.assertEqual(stats["round2_new_comments"], 6)
        self.assertEqual(stats["round2_procurement_comments"], 5)
        comments = {row["comment_id"]: row for row in self.read("comments.csv")}
        self.assertEqual(comments["9100000000000000001"]["text"], "كم سعر الكيلو؟")
        self.assertEqual(comments["9100000000000000001"]["procurement_signal"], "bulk")
        reply = comments["9200000000000000001"]
        self.assertEqual(reply["parent_id"], "9100000000000000001")
        self.assertEqual(reply["parent_status"], "captured")
        self.assertEqual(reply["quote_amount"], "١٢٠٠")
        self.assertEqual(reply["quote_currency"], "ريال سعودي")
        self.assertEqual(reply["quote_unit"], "كيلو")
        self.assertEqual(reply["quote_by"], "[text] @fixture_oud_1")
        self.assertEqual(comments["9300000000000000002"]["timing"], "older_not_seen")
        self.assertEqual(comments["9300000000000000003"]["posted_at"], "unknown")
        self.assertEqual(comments["9300000000000000004"]["delivery_destination"], "SA")
        self.assertEqual(comments["9300000000000000004"]["merchant_country"], "AE")
        self.assertEqual(comments["9300000000000000005"]["timing"], "after_window_observed")
        video = next(row for row in self.read("videos.csv") if row["video_id"] == VID)
        self.assertEqual(set(video["keywords"].split("|")), {"كيلو عود", "خشب العود", "عود طبيعي"})

    def test_export_neutralizes_external_formula_cells_and_excludes_reviewed_videos_from_demand(self):
        self.seed()
        self.run_json(START)
        from discovery import initialize

        formula_rows = ['=1+1', '  ＋1', '\t@SUM(1)', '\nplain', "'=1", '  \ufeff-1',
                        '+SUM(1,"x");=1', 'سؤال، عربي\nسطر جديد']
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            initialize(db)
            video_ids = [row[0] for row in db.execute("SELECT video_id FROM videos ORDER BY video_id LIMIT 3")]
            db.execute("UPDATE videos SET material='chips'")
            for index, vid in enumerate(video_ids):
                attempt_id = db.execute("SELECT id FROM attempts WHERE video_id=?", (vid,)).fetchone()[0]
                db.execute("DELETE FROM comments WHERE video_id=?", (vid,))
                rows = [(vid, "100", "how much per kg", collect.utc(START), attempt_id)]
                if index == 0:
                    rows += [(vid, str(101 + number), text, collect.utc(START), attempt_id)
                             for number, text in enumerate(formula_rows)]
                db.executemany("""
                    INSERT INTO comments (video_id,comment_id,text,collected_at,first_round,timing,attempt_id)
                    VALUES (?,?,?,?,1,'first_round',?)
                """, rows)
            db.execute("DELETE FROM comments WHERE video_id NOT IN (?,?,?)", video_ids)
            db.execute("INSERT INTO candidate_reviews VALUES (?,'exclude',NULL,'discovery review',?)",
                       (video_ids[2], collect.utc(START)))
        notes = self.db.with_name("notes.csv")
        notes.write_text(f"video_id,comment_id,video_scope,review_note\n{video_ids[1]},100,other,annotation\n",
                         encoding="utf-8")
        self.cli(START, "export", self.out, "--annotations", notes)
        comments = self.read("comments.csv")
        for number, text in enumerate(formula_rows):
            row = next(row for row in comments if row["comment_id"] == str(101 + number))
            self.assertEqual(row["text"], "[text] " + text if number < 7 else text)
        self.assertEqual({row["scope_note"] for row in comments if row["sample_group"] == "other"},
                         {"annotation", "discovery review"})
        demand, = self.read("market_demand.csv")
        self.assertEqual((demand["comments"], demand["videos"], demand["accounts"]), ("1", "1", "1"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT text FROM comments WHERE comment_id='101'").fetchone()[0],
                             formula_rows[0])

    def test_interrupted_attempt_is_fenced_and_transaction_rolls_back(self):
        self.seed()
        now = collect.utc(START)
        only = set()
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            video, stale_id, _ = collect.claim(db, self.cfg, now, "json", only, VID)
            self.assertIsNone(collect.claim(db, self.cfg, now, "json", only, VID))
            retry_at = collect.after(now, seconds=241, minutes=15)
            self.assertIsNone(collect.claim(db, self.cfg, collect.after(now, seconds=241), "json", only, VID))
            _, active_id, _ = collect.claim(db, self.cfg, retry_at, "json", only, VID)
            collect.finish(db, video, stale_id, Result("empty_confirmed"), retry_at)
            self.assertEqual(db.execute("SELECT stage FROM videos WHERE video_id=?", (VID,)).fetchone()[0], 1)
            row = dict(comment_id="99", parent_id=None, text="عينة؟", posted_at=None, author=None)
            with self.assertRaises(KeyError):
                collect.finish(db, video, active_id, Result("success", [row, {}]), retry_at)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 0)
            with redirect_stdout(io.StringIO()):
                collect.finish(db, video, active_id, Result("success", [row]), retry_at)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT stage FROM videos WHERE video_id=?", (VID,)).fetchone()[0], 2)

    def test_completion_at_lease_deadline_does_not_advance_round(self):
        self.seed()
        now = collect.utc(START)
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            video, attempt_id, _ = collect.claim(db, self.cfg, now, "json", set(), VID)
            deadline = db.execute("SELECT lease_until FROM attempts WHERE id=?", (attempt_id,)).fetchone()[0]
            self.assertFalse(collect.finish(db, video, attempt_id, Result("empty_confirmed"), deadline))
            self.assertEqual(db.execute("SELECT status FROM attempts").fetchone()[0], "interrupted")
            self.assertEqual(db.execute("SELECT stage FROM videos WHERE video_id=?", (VID,)).fetchone()[0], 1)

    def test_log_is_safe_on_a_legacy_windows_output_stream(self):
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="gbk") as stream, redirect_stdout(stream):
            collect.log("seed=عود")
            self.assertIn(b"seed=\\u0639\\u0648\\u062f", buffer.getvalue())

    def test_apify_request_and_unverified_empty(self):
        url = "https://www.tiktok.com/@fixture/video/" + VID
        with patch.dict("os.environ", {"APIFY_TOKEN": "fake-token"}):
            source = ApifySource(self.cfg)
        run = {"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "set1"}}
        with patch.object(source, "request", side_effect=[run, []]) as request:
            self.assertEqual(source.fetch({"video_id": VID, "url": url}, 1, 1).status, "empty_unverified")
        path, payload = request.call_args_list[0].args
        self.assertTrue(path.startswith("actors/clockworks~tiktok-comments-scraper/runs?"))
        self.assertIn("maxTotalChargeUsd=0.5", path)
        self.assertEqual(payload, dict(postURLs=[url], topLevelCommentsPerPost=30,
                                       maxRepliesPerComment=1, commentsPerPost=35))
        self.assertEqual(canonical(url + "?x=1")[1], url)

    def test_short_link_resolution_without_network(self):
        with patch("sources.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.geturl.return_value = "https://m.tiktok.com/@fixture/video/" + VID + "?x=1"
            self.assertEqual(canonical("https://vm.tiktok.com/fake/", resolve=True)[0], VID)
            response.geturl.return_value = "https://example.com/@fixture/video/" + VID
            with self.assertRaises(ValueError):
                canonical("https://vm.tiktok.com/fake/", resolve=True)

    def test_auto_filters_and_does_not_repeat_comments(self):
        url = "https://www.tiktok.com/@fixture/video/" + VID
        selected = {
            "webVideoUrl": url, "createTimeISO": collect.DEFAULT_SINCE, "commentCount": 3,
            "text": "رقائق خشب العود", "searchQuery": "رقائق العود", "locationCreated": "SA",
        }
        dataset = [
            selected,
            selected | {"webVideoUrl": url + "?tracking=1", "searchQuery": "عود طبيعي"},
            selected | {"webVideoUrl": url + "2", "createTimeISO": "2025-12-31T19:59:59Z"},
            selected | {"webVideoUrl": url + "3", "commentCount": 0},
            selected | {"webVideoUrl": url + "4", "text": "عطر العود"},
            selected | {"webVideoUrl": url + "5", "createTimeISO": None},
        ]
        self.cli(START, "status")
        args = Namespace(now=None, authorized=True, limit=10, per_keyword=5, workers=1, search_batches=1, since=collect.DEFAULT_SINCE,
                         skip_discovery=False, output=self.out, annotations=None)
        reply = dict(comment_id="99", parent_id=None, text="فيه جملة؟", posted_at=None, author=None)
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db, \
                patch("collect.ApifySource") as apify, redirect_stdout(io.StringIO()):
            db.row_factory = sqlite3.Row
            source = apify.return_value
            source.search.return_value = (dataset, "mock run")
            source.fetch.return_value = Result("success", [reply])
            collect.automatic(db, args, self.cfg, lambda: collect.utc(START))
            collect.automatic(db, args, self.cfg, lambda: collect.utc("2026-11-02T09:00:00Z"))
            self.assertEqual(source.fetch.call_count, 1)
            self.assertEqual(source.search.call_count, 2)
            args.skip_discovery = True
            collect.automatic(db, args, self.cfg, lambda: collect.utc(DUE))
            self.assertEqual(source.fetch.call_count, 2)
            self.assertEqual(source.search.call_count, 2)
            args.skip_discovery = False
            collect.automatic(db, args, self.cfg, lambda: collect.utc("2026-11-05T09:00:00Z"))
            self.assertEqual(source.fetch.call_count, 2)
        video, = self.read("videos.csv")
        self.assertEqual(video["discovered_at"], collect.utc(START))
        self.assertEqual(video["state"], "archived")
        self.assertNotIn("caption", video)
        self.assertEqual(video["merchant_country"], "unknown")
        self.assertEqual(video["material"], "chips")
        self.assertEqual(set(video["keywords"].split("|")),
                         {"رقائق العود", "عود طبيعي", "خشب العود", "رقائق خشب العود"})
        self.assertEqual(len(self.read("comments.csv")), 1)

    def test_apify_free_plan_gate_and_search_request(self):
        with patch.dict("os.environ", {"APIFY_TOKEN": "fake-token"}):
            source = ApifySource(self.cfg)
        free = {"data": {"plan": {"tier": "FREE", "monthlyBasePriceUsd": 0}}}
        with patch.object(source, "request", return_value=free) as request:
            source.require_free_plan()
            request.assert_called_once_with("users/me")
            request.return_value = {"data": {"plan": {"tier": "STARTER", "monthlyBasePriceUsd": 29}}}
            with self.assertRaisesRegex(ValueError, "Free personal"):
                source.require_free_plan()
        with patch.object(source, "run_actor", return_value=([], {}, "mock run")) as actor:
            self.assertEqual(source.search(["خشب العود"], 5), ([], "mock run"))
            actor.assert_called_once_with("tiktok-scraper", {
                "searchQueries": ["خشب العود"], "searchSection": "/video", "resultsPerPage": 5,
                "shouldDownloadVideos": False, "shouldDownloadCovers": False,
                "shouldDownloadSlideshowImages": False, "shouldDownloadAvatars": False,
            }, 0.50, 5)

    def test_due_followup_has_priority_and_groups_rotate(self):
        self.seed()
        with closing(sqlite3.connect(self.db, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(collect.next_search_groups(db, 2), [0, 1])
            self.assertEqual(collect.next_search_groups(db, 2), [2, 3])
            self.assertEqual(collect.next_search_groups(db, 2), [4, 5])
            self.assertEqual(collect.next_search_groups(db, 2), [0, 1])
            later = "9000000000000000002"
            db.execute("UPDATE videos SET stage=2 WHERE video_id=?", (later,))
            job = collect.claim(db, self.cfg, collect.utc(DUE), "json", set())
            self.assertEqual(job[0]["video_id"], later)

    def test_procurement_topics_distinguish_package_weight_from_price(self):
        self.assertNotIn("公斤报价", classify("الكيس كم كيلو")[0])
        self.assertNotIn("公斤报价", classify("كم كيلو")[0])
        self.assertIn("公斤报价", classify("كم روبيه الكيلو")[0])
        self.assertNotIn("公斤报价", classify("هذا اللي بالفديو هو كمية ثمن كيلوا ؟؟؟")[0])
        self.assertIn("明确批发或供货", classify("الكيلو للجمله بكم")[0])
        self.assertNotIn("样品", classify("في زيوت معينه لعمل المستكه المعطره")[0])
        self.assertIn("样品", classify("فيه عينات للتجربه؟")[0])
        self.assertEqual(classify("توصل الامارات بكم")[1], "AE")
        self.assertEqual(classify("فيه شحن لا جازان")[1], "SA")
        self.assertEqual(classify("بكم الكيلو واصل السعودية")[1], "SA")


if __name__ == "__main__":
    unittest.main()
