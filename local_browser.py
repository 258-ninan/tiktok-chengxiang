"""Read TikTok page responses produced by normal local browser navigation."""

import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlsplit

from reports import classify
from sources import Result, canonical, item_id, normalize, utc


class BrowserBlocked(ValueError):
    pass


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def video_items(payload, keep_incomplete=False):
    items = {}
    for obj in dictionaries(payload):
        if not isinstance(obj.get("desc"), str) or (not keep_incomplete and not obj.get("createTime")):
            continue
        try:
            vid = item_id(obj["id"])
            try:
                posted = utc(int(obj["createTime"]))
            except (KeyError, ValueError, TypeError, OSError, OverflowError):
                if not keep_incomplete:
                    continue
                posted = None
            author = obj.get("author")
            handle = author.get("uniqueId") if isinstance(author, dict) else author
            if not isinstance(handle, str):
                continue
            url = f"https://www.tiktok.com/@{handle}/video/{vid}"
            canonical(url)
            stats = obj.get("stats") or obj.get("statsV2") or {}
            count = stats.get("commentCount")
            try:
                count = int(count) if count is not None else None
            except (TypeError,ValueError):
                if not keep_incomplete:
                    continue
                count = None
            items[vid] = dict(webVideoUrl=url, text=obj["desc"], createTimeISO=posted,
                              commentCount=count)
        except (KeyError, ValueError, TypeError, OSError, OverflowError):
            continue
    return list(items.values())


def comment_items(payload, video_id, url, expected_parent=None):
    rows = []
    comments = payload.get("comments")
    if not isinstance(comments, list):
        return rows
    for obj in comments:
        if not isinstance(obj, dict):
            continue
        aweme = obj.get("aweme_id")
        if aweme is not None and str(aweme) != video_id:
            continue
        parent = obj.get("reply_id") or expected_parent
        if parent in ("0", 0, ""):
            parent = expected_parent
        author = obj.get("user") or {}
        if not isinstance(author, dict):
            author = {}
        rows.append({
            "cid": obj.get("cid"), "text": obj.get("text"),
            "createTime": obj.get("create_time"),
            "uniqueId": author.get("unique_id") or author.get("uniqueId"),
            "repliesToId": parent,
            "videoWebUrl": url,
        })
    return rows


def bounded_comments(rows, cfg):
    tops = {}
    replies = []
    for row in rows:
        if row.get("repliesToId"):
            replies.append(row)
        else:
            tops.setdefault(str(row.get("cid")), row)
    selected = list(tops.values())[:cfg["top_level_limit"]]
    parent_ids = {str(row["cid"]) for row in selected}
    per_parent = Counter()
    ids = {str(row["cid"]) for row in selected}
    for row in replies:
        parent = str(row["repliesToId"])
        cid = str(row["cid"])
        if (parent in parent_ids and cid not in ids
                and per_parent[parent] < cfg["replies_per_comment"] and len(selected) < cfg["total_limit"]):
            selected.append(row)
            ids.add(cid)
            per_parent[parent] += 1
    return selected[:cfg["total_limit"]]


class LocalBrowser:
    def __init__(self, profile, settings, cfg, headless=True):
        self.profile = profile
        self.settings = settings
        self.cfg = cfg
        self.headless = headless
        self.payloads = []
        self.network_problem = None
        self.context = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(
                str(self.profile), channel=self.settings["channel"], headless=self.headless,
                accept_downloads=False, viewport={"width": 1280, "height": 900},
            )
            self.context.route("**/*", self.route)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(5000)
            self.page.set_default_navigation_timeout(25000)
            self.page.on("response", self.response)
            return self
        except BaseException:
            if self.context:
                self.context.close()
            self.runtime.stop()
            raise

    def __exit__(self, *args):
        self.context.close()
        self.runtime.stop()

    def route(self, route):
        request = route.request
        if request.resource_type in {"media", "font"} or (self.headless and request.resource_type == "image"):
            route.abort()
        else:
            route.continue_()

    def response(self, response):
        parts = urlsplit(response.url)
        if parts.hostname not in {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}:
            return
        if not parts.path.startswith(("/api/search/", "/api/comment/", "/api/post/")):
            return
        if response.status in {401, 403, 429}:
            self.network_problem = f"Page returned HTTP {response.status}"
            return
        try:
            data = response.json()
            if isinstance(data, dict):
                self.payloads.append((parts.path, parse_qs(parts.query), data))
        except Exception:
            # Empty/non-JSON page responses are validated by the calling operation.
            return

    def signed_in(self):
        return any(cookie["name"] in {"sessionid", "sessionid_ss", "sid_tt"} and cookie.get("value")
                   for cookie in self.context.cookies("https://www.tiktok.com"))

    def check_access(self):
        if self.network_problem:
            raise BrowserBlocked(self.network_problem)
        if self.page.locator("iframe[src*='captcha'], #captcha_container, [id*='captcha-verify']").count():
            raise BrowserBlocked("Verification is required in the local browser")
        text = self.page.locator("body").inner_text(timeout=5000)[:8000].lower()
        if any(word in text for word in ("access denied", "verify to continue", "verify you are human")):
            raise BrowserBlocked("The page requires verification")
        if "/login" in urlsplit(self.page.url).path:
            raise BrowserBlocked("Login is required")

    def navigate(self, url):
        self.payloads.clear()
        self.network_problem = None
        response = self.page.goto(url, wait_until="domcontentloaded")
        if response and response.status in {401, 403, 429}:
            raise BrowserBlocked(f"Page returned HTTP {response.status}")
        self.page.wait_for_timeout(2000)
        self.check_access()

    def bootstrap(self):
        result = []
        for selector in ("#__UNIVERSAL_DATA_FOR_REHYDRATION__", "#SIGI_STATE"):
            node = self.page.locator(selector)
            if node.count():
                try:
                    result.extend(video_items(json.loads(node.first.text_content())))
                except (ValueError, TypeError):
                    continue
        return result

    def scan(self, url, scrolls, deadline):
        self.navigate(url)
        for _ in range(scrolls):
            if time.monotonic() >= deadline:
                break
            self.page.mouse.wheel(0, 900)
            self.page.wait_for_timeout(1600)
            self.check_access()
        result = {canonical(item["webVideoUrl"])[0]: item for item in self.bootstrap()}
        for path, _, payload in self.payloads:
            if path.startswith(("/api/search/", "/api/post/")):
                for item in video_items(payload):
                    result[canonical(item["webVideoUrl"])[0]] = item
        return list(result.values())

    def search(self, keyword, scrolls, deadline):
        return self.scan("https://www.tiktok.com/search/video?q=" + quote(keyword), scrolls, deadline)

    def profile_videos(self, account, scrolls, deadline):
        items = self.scan("https://www.tiktok.com/" + account, scrolls, deadline)
        return [item for item in items
                if canonical(item["webVideoUrl"])[2].casefold() == account.casefold()]

    def captured_comments(self, video):
        result = []
        for path, query, payload in self.payloads:
            if not path.startswith("/api/comment/"):
                continue
            request_video = query.get("aweme_id", [None])[0]
            if request_video and request_video != video["video_id"]:
                continue
            parent = query.get("comment_id", [None])[0] if "reply" in path else None
            result.extend(comment_items(payload, video["video_id"], video["url"], parent))
        return result

    def scroll_comments(self):
        self.page.evaluate("""() => {
            const item = document.querySelector('[data-e2e="comment-item"]');
            let node = item;
            while (node && node !== document.body) {
                if (node.scrollHeight > node.clientHeight + 30 &&
                    /auto|scroll/.test(getComputedStyle(node).overflowY)) {
                    node.scrollBy(0, 650);
                    return;
                }
                node = node.parentElement;
            }
            window.scrollBy(0, 650);
        }""")
        self.page.wait_for_timeout(1200)

    def fetch(self, video, stage, attempt):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            self.navigate(video["url"])
            deadline = time.monotonic() + self.settings["video_timeout_seconds"]
            unchanged = 0
            previous = 0
            while time.monotonic() < deadline:
                self.check_access()
                rows = self.captured_comments(video)
                tops = {str(row["cid"]) for row in rows if not row.get("repliesToId")}
                if len(tops) >= self.cfg["top_level_limit"]:
                    break
                unchanged = unchanged + 1 if len(tops) == previous else 0
                if unchanged >= 5:
                    break
                previous = len(tops)
                self.scroll_comments()
            # Only expand replies beneath procurement questions.
            if self.cfg["replies_per_comment"]:
                for row in bounded_comments(self.captured_comments(video), self.cfg):
                    if time.monotonic() >= deadline:
                        break
                    if row.get("repliesToId") or not classify(row.get("text") or "")[0]:
                        continue
                    node = self.page.get_by_text(row.get("text") or "", exact=True).first
                    if not node.count():
                        continue
                    block = node.locator("xpath=ancestor::*[@data-e2e='comment-item'][1]")
                    buttons = block.get_by_text(re.compile(r"view.*repl|عرض.*رد|عرض.*الردود", re.I))
                    if buttons.count() and buttons.first.is_visible():
                        buttons.first.click(timeout=2000)
                        self.page.wait_for_timeout(900)
            self.check_access()
            result = normalize(bounded_comments(self.captured_comments(video), self.cfg),
                               video["video_id"], self.cfg)
            result.detail = "Local page observation. " + result.detail
            return result
        except BrowserBlocked:
            raise
        except PlaywrightTimeout:
            return Result("timeout", detail="Local page timed out")
