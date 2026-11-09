import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import collect
import local_collect
from local_browser import BrowserBlocked, LocalBrowser, bounded_comments, comment_items, video_items
from profit_analysis import INPUT_FIELDS, NUMBER_FIELDS, calculate, export_profit
from sources import normalize


class LocalWorkflow(unittest.TestCase):
    def test_navigation_checks_http_rejection_before_inspecting_page(self):
        browser = LocalBrowser(Path("unused"), {}, {})
        browser.page = Mock()
        browser.page.goto.return_value.status = 403
        with self.assertRaisesRegex(BrowserBlocked, "HTTP 403"):
            browser.navigate("https://www.tiktok.com/@fixture/video/123")
        browser.page.wait_for_timeout.assert_not_called()

    def test_profile_scan_excludes_recommended_videos_from_other_accounts(self):
        browser = LocalBrowser(Path("unused"), {}, {})
        own = {"webVideoUrl": "https://www.tiktok.com/@Fixture/video/123"}
        recommended = {"webVideoUrl": "https://www.tiktok.com/@other/video/456"}
        browser.scan = Mock(return_value=[own, recommended])
        self.assertEqual(browser.profile_videos("@fixture", 1, 10), [own])

    def test_native_page_responses_keep_ids_times_and_parent_links(self):
        payload = {"data": [{"item": {
            "id": "9000000000000000001", "desc": "رقائق خشب العود",
            "createTime": 1793000000, "author": {"uniqueId": "fixture"},
            "stats": {"commentCount": 30},
        }}]}
        video, = video_items(payload)
        self.assertEqual(video["commentCount"], 30)
        self.assertTrue(video["createTimeISO"].endswith("+00:00"))
        raw = {"comments": [
            {"cid": "123", "text": "كم سعر الكيلو؟", "aweme_id": "9000000000000000001",
             "create_time": 1793000000, "reply_id": "0", "user": {"unique_id": "buyer"}},
            {"cid": "124", "text": "١٢٠٠ ريال", "aweme_id": "9000000000000000001",
             "create_time": 1793000001, "reply_id": "123", "user": {"unique_id": "fixture"}},
            {"cid": "999", "text": "wrong video", "aweme_id": "999"},
        ]}
        rows = comment_items(raw, "9000000000000000001", video["webVideoUrl"])
        result = normalize(rows, "9000000000000000001", collect.config(Path("config.json")))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.rows[1]["parent_id"], "123")
        self.assertEqual(result.rows[0]["text"], "كم سعر الكيلو؟")
        self.assertEqual(len(result.rows), 2)

    def test_limits_preserve_parents_and_drop_duplicate_replies(self):
        rows = [
            {"cid": "1", "repliesToId": None}, {"cid": "2", "repliesToId": None},
            {"cid": "3", "repliesToId": "1"}, {"cid": "3", "repliesToId": "1"},
            {"cid": "4", "repliesToId": "2"}, {"cid": "5", "repliesToId": "99"},
        ]
        result = bounded_comments(rows, dict(top_level_limit=2, replies_per_comment=1, total_limit=4))
        self.assertEqual([row["cid"] for row in result], ["1", "2", "3", "4"])

    def test_profit_requires_explicit_inputs_and_uses_loss_adjusted_purchase(self):
        blank = dict.fromkeys(INPUT_FIELDS, "")
        self.assertEqual(calculate(blank)["status"], "needs_input")
        scenario = blank | dict(currency="CNY", order_kg="10", sale_per_kg="200", purchase_per_kg="90",
                                loss_percent="10", packaging_per_order="20", freight_per_order="100",
                                import_charges_per_order="0", payment_percent="2",
                                acquisition_per_order="30", other_per_order="10",
                                fixed_cost_allocation_per_order="50")
        result = calculate(scenario)
        self.assertEqual(result["product_cost"], "1000.00")
        self.assertEqual(result["estimated_profit"], "750.00")
        self.assertEqual(result["profit_per_kg"], "75.00")
        with self.assertRaises(ValueError):
            calculate(scenario | {"loss_percent": "100"})

    def test_profit_export_keeps_calculated_losses_numeric_and_prefixes_user_text(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            inputs = Path(directory) / "inputs.csv"
            output = Path(directory) / "profit.csv"
            scenario = dict.fromkeys(NUMBER_FIELDS, "0") | dict(
                scenario="=1+1", evidence="+note", currency="CNY", order_kg="1",
                sale_per_kg="100", purchase_per_kg="110",
            )
            with inputs.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, INPUT_FIELDS)
                writer.writeheader()
                writer.writerow(scenario)
            result, = export_profit(inputs, output)
            with output.open(encoding="utf-8-sig", newline="") as stream:
                row, = csv.DictReader(stream)
            self.assertEqual(result["scenario"], "=1+1")
            self.assertEqual(row["scenario"], "[text] =1+1")
            self.assertEqual(row["evidence"], "[text] +note")
            self.assertEqual(row["estimated_profit"], "-10.00")

    def test_discovery_cursor_rotates(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with closing(sqlite3.connect(Path(directory) / "test.sqlite", isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                db.executescript(local_collect.LOCAL_SCHEMA)
                first = local_collect.next_keyword(db)
                second = local_collect.next_keyword(db)
                self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
