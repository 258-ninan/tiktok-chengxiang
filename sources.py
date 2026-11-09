"""Replaceable comment sources. The scheduler only consumes Result objects."""

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def utc(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return result.astimezone(UTC).isoformat()


HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
VIDEO = re.compile(r"/(@[A-Za-z0-9_.-]+)/video/([0-9]+)/?")


def tiktok_url(url):
    if not isinstance(url, str):
        raise ValueError("Expected a TikTok URL string")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() not in HOSTS:
        raise ValueError("Expected an HTTPS TikTok link")
    return parts


class TikTokRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        tiktok_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def canonical(url, resolve=False):
    parts = tiktok_url(url)
    match = VIDEO.fullmatch(parts.path)
    if not match and resolve and (
        parts.hostname in {"vm.tiktok.com", "vt.tiktok.com"} or parts.path.startswith("/t/")
    ):
        opener = build_opener(TikTokRedirects)
        with opener.open(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as response:
            return canonical(response.geturl())
    if not match:
        raise ValueError("Expected /@account/video/ID. Open short links manually or pass --resolve-short")
    account, video_id = match.groups()
    return video_id, f"https://www.tiktok.com/{account}/video/{video_id}", account


@dataclass
class Result:
    status: str
    rows: list = field(default_factory=list)
    detail: str = ""


def item_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid ID")
    value = str(value)
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Invalid ID")
    return value


def normalize(items, video_id, cfg):
    if not isinstance(items, list):
        return Result("error", detail="Unexpected dataset structure")
    if not items:
        return Result("empty_unverified", detail="Dataset is empty without explicit verification")
    rows = {}
    bad = 0
    for item in items:
        try:
            if not isinstance(item, dict) or item.get("error") or item.get("success") is False:
                raise ValueError("Source error row")
            link = item.get("videoWebUrl") or item.get("submittedVideoUrl")
            if canonical(link or "")[0] != video_id:
                raise ValueError("Video mismatch")
            cid = item_id(item.get("cid"))
            parent = item.get("repliesToId")
            parent = item_id(parent) if parent not in (None, "") else None
            body = item["text"]
            if (body is not None and not isinstance(body, str)) or cid == parent:
                raise ValueError("Invalid comment")
            posted = None
            for value in (item.get("createTimeISO"), item.get("createTime")):
                if value:
                    try:
                        posted = utc(value)
                        break
                    except (ValueError, TypeError, OverflowError, OSError):
                        continue
            author = item.get("uniqueId")
            row = dict(comment_id=cid, parent_id=parent, text=body, posted_at=posted,
                       author=author if isinstance(author, str) else None)
            if cid in rows and rows[cid] != row:
                raise ValueError("Conflicting comment rows")
            rows[cid] = row
        except (KeyError, ValueError, TypeError):
            bad += 1
    tops = [row for row in rows.values() if row["parent_id"] is None]
    replies = Counter(row["parent_id"] for row in rows.values() if row["parent_id"])
    exceeded = cfg is not None and (
        len(tops) > cfg["top_level_limit"] or len(rows) > cfg["total_limit"]
        or any(n > cfg["replies_per_comment"] for n in replies.values())
    )
    if bad or exceeded or (cfg is not None and not tops):
        return Result("partial" if rows else "error", list(rows.values()),
                      f"Invalid rows={bad}; over limit={exceeded}; top-level={len(tops)}")
    return Result("success", list(rows.values()))


class JsonSource:
    def __init__(self, path, cfg):
        self.data = json.loads(path.read_text(encoding="utf-8-sig"))
        self.cfg = cfg

    def fetch(self, video, stage, attempt):
        outcomes = self.data.get(video["video_id"], {}).get(str(stage), [])
        if not outcomes:
            return Result("error", detail="No offline result for this video and round")
        entry = outcomes[min(attempt - 1, len(outcomes) - 1)]
        if "status" not in entry:
            return normalize(entry["items"], video["video_id"], self.cfg)
        status = entry["status"]
        if status not in {"timeout", "error", "partial", "empty_confirmed"}:
            raise ValueError("Invalid explicit offline status")
        detail = entry.get("evidence", "").strip()
        if status == "empty_confirmed" and (not detail or entry.get("items")):
            return Result("empty_unverified", detail="Empty result requires explicit evidence")
        rows = normalize(entry["items"], video["video_id"], self.cfg).rows if entry.get("items") else []
        return Result(status, rows, detail)


class ActorFailure(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class ApifySource:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = os.environ.get("APIFY_TOKEN", "").strip()
        if not self.token:
            raise ValueError("Set APIFY_TOKEN in your local environment")

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request("https://api.apify.com/v2/" + path, data=data, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        })
        with build_opener(NoRedirects).open(req, timeout=20) as response:
            return json.load(response)

    def require_free_plan(self):
        try:
            account = self.request("users/me")["data"]
            plan = account["plan"]
            if (plan.get("tier") != "FREE" or plan.get("monthlyBasePriceUsd") != 0
                    or account.get("organizationOwnerUserId")):
                raise ValueError("Use an Apify Free personal account token")
        except HTTPError as exc:
            raise ValueError(f"Could not verify Apify Free plan (HTTP {exc.code})") from None
        except (KeyError, TypeError, AttributeError, URLError):
            raise ValueError("Could not verify Apify Free plan") from None

    def run_actor(self, actor, payload, budget, limit):
        timeout = self.cfg["actor_timeout_seconds"]
        deadline = time.monotonic() + timeout + 30
        run_id = ""
        try:
            query = urlencode({"timeout": timeout, "maxTotalChargeUsd": budget})
            run = self.request(f"actors/clockworks~{actor}/runs?" + query, payload)["data"]
            run_id = self.resource_id(run["id"])
            while run["status"] in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
                if time.monotonic() >= deadline:
                    raise ActorFailure("timeout", f"Polling deadline; run={run_id}")
                time.sleep(3)
                run = self.request(f"actor-runs/{run_id}")["data"]
            status = run["status"]
            if status != "SUCCEEDED":
                raise ActorFailure("timeout" if status == "TIMED-OUT" else "error",
                                   f"Actor status={status}; run={run_id}")
            dataset = self.resource_id(run["defaultDatasetId"])
            items = self.request(f"datasets/{dataset}/items?limit={limit}")
            return items, run, f"run={run_id}; dataset={dataset}"
        except ActorFailure:
            raise
        except HTTPError as exc:
            status = "account_blocked" if exc.code in {401, 402, 403, 429} else "error"
            detail = ""
            try:
                error = json.loads(exc.read(8192)).get("error", {})
                message = error.get("message")
                if isinstance(message, str):
                    detail = "; " + " ".join(message.replace(self.token, "[redacted]").split())[:350]
            except (ValueError, AttributeError, OSError):
                pass
            raise ActorFailure(status, f"Apify HTTP {exc.code}; run={run_id}{detail}") from None
        except (TimeoutError, URLError) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            raise ActorFailure("timeout" if timed_out else "error", f"Apify connection; run={run_id}") from None
        except (KeyError, ValueError, TypeError):
            raise ActorFailure("error", f"Unexpected Apify response; run={run_id}") from None

    def fetch(self, video, stage, attempt):
        cfg = self.cfg
        try:
            items, run, detail = self.run_actor("tiktok-comments-scraper", {
                "postURLs": [video["url"]],
                "topLevelCommentsPerPost": cfg["top_level_limit"],
                "maxRepliesPerComment": cfg["replies_per_comment"],
                "commentsPerPost": cfg["total_limit"],
            }, cfg["max_run_usd"], cfg["total_limit"] + 1)
        except ActorFailure as exc:
            return Result(exc.status, detail=str(exc))
        result = normalize(items, video["video_id"], cfg)
        if (result.status == "success" and run.get("chargedEventCounts")
                and isinstance(run.get("usageTotalUsd"), (int, float))
                and run["usageTotalUsd"] >= cfg["max_run_usd"]):
            result.status = "partial"
            result.detail = "Run reached configured spend ceiling; verify dataset"
        result.detail = f"{result.detail} {detail}".strip()
        return result

    def search(self, keywords, per_keyword):
        items, _, detail = self.run_actor("tiktok-scraper", {
            "searchQueries": keywords,
            "searchSection": "/video",
            "resultsPerPage": per_keyword,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadAvatars": False,
        }, self.cfg["max_run_usd"], len(keywords) * per_keyword)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or item.get("error") or item.get("success") is False
            for item in items
        ):
            raise ValueError(f"Video search returned an invalid dataset; {detail}")
        return items, detail

    @staticmethod
    def resource_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]+", value):
            raise ValueError("Invalid Apify resource ID")
        return value
