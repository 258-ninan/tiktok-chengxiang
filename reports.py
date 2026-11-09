"""UTF-8 exports and reviewable Arabic procurement signals."""

import csv
import json
import re
from pathlib import Path
from collections import Counter, defaultdict

from analysis_report import write_analysis
from profit_analysis import export_profit
from report_output import write_csv


RAW = {"chips", "crumbs", "chunks", "mixed_raw"}
TOPICS = {
    "公斤报价": r"(?:سعر|بكم|بكام|كم\s+ثمن|روبيه).{0,24}كيلو|كيلو.{0,24}(?:بكم|بكام|سعر)|^\s*(?:كم\s+الكيلو|الكيلو\s+كم)\s*[؟?]?\s*$|\b(?:price|how much).{0,24}\b(?:per\s+kg|per\s+kilo(?:gram)?|a\s+kilo(?:gram)?)\b",
    "明确批发或供货": r"(?:فيه|في|عندكم|تبيعون|بيع|سعر)\s*جمله|(?<!\w)(?:بالجمله|للجمله|توريد|مورد|تزودونا)(?!\w)|كيلو.{0,12}جمله(?!\w)|\bwholesale\s+(?:price|pricing|supplier)\b",
    "起订量": r"(?:اقل|ادني|الحد الادني|حد ادني|مينيموم).{0,20}(?:كميه|طلب)|\b(?:moq|minimum order quantity)\b",
    "样品": r"(?<!\w)(?:ال)?(?:عينه|عينات)(?!\w)|\bsamples?\b",
    "产地": r"من وين|من فين|المنشا|بلد الاصل|من اي بلد",
    "天然或处理料": r"طبيعي|محسن|معالج|مصبوغ|مغشوش",
    "燃烧表现": r"حرق|يحترق|يدوم|ثبات|دخان|يفوح|فوحان|ريحه",
    "配送": r"شحن|توصيل|توصلون|توصل|تشحنون|يوصل|يوصلون|\b(?:ship|deliver)\s+to\b|\b(?:shipping|delivery)\s+(?:fee|cost)\b",
}
RETAIL = "零售询价"
QUANTITY_REQUEST = "指定数量采购"
TAGS = set(TOPICS) | {RETAIL, QUANTITY_REQUEST}
DELIVERY = {
    "SA": r"السعوديه|الرياض|جده|مكه|الدمام|جازان|جيزان",
    "AE": r"الامارات|دبي|ابو ظبي|ابوظبي|الشارقه|عجمان",
}
SHIP_TO = r"(?:شحن|توصيل|توصلون|توصل|تشحنون|يوصل|يوصلون|واصل)"


def classify(text):
    norm = re.sub(r"[\u064b-\u065f\u0670ـ]", "", text.lower())
    norm = norm.translate(str.maketrans("أإآىة", "ااايه"))
    tags = {topic for topic, pattern in TOPICS.items() if re.search(pattern, norm)}
    if "公斤报价" not in tags and re.search(r"بكم|بكام|كم (?:ال)?سعر|كم (?:ال)?ثمن|كم سعره", norm):
        tags.add(RETAIL)
    destination = []
    for country, place in DELIVERY.items():
        if re.search(rf"{SHIP_TO}\s*(?:الي|لا|ل)?\s*(?:{place})(?!\w)", norm):
            destination.append(country)
    if destination:
        tags.add("配送")
    return tags, "|".join(destination) or "unknown"


def read_annotations(path, valid_keys):
    if path is None:
        return {}
    annotations = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"video_id", "comment_id"} <= set(reader.fieldnames or []):
            raise ValueError("Annotations need video_id and comment_id")
        for row in reader:
            if None in row:
                raise ValueError("Unexpected annotation columns")
            row = {key: (value or "").strip() for key, value in row.items()}
            key = (row["video_id"], row["comment_id"])
            if key not in valid_keys or key in annotations:
                raise ValueError(f"Unknown or duplicate annotation: {key}")
            if row.get("topics") and row["topics"] != "-" and not set(row["topics"].split("|")) <= TAGS:
                raise ValueError(f"Unknown annotation topic: {key}")
            if row.get("video_scope") and (row["video_scope"] != "other" or not row.get("review_note")):
                raise ValueError(f"Video scope exclusion needs a review note: {key}")
            if row.get("destination") not in {None, "", "unknown"} and not row.get("destination_evidence"):
                raise ValueError(f"Destination evidence required: {key}")
            quote = [row.get(field) for field in ("quote_amount", "quote_currency", "quote_unit", "quote_by")]
            if any(quote) and not all(quote):
                raise ValueError(f"Quote needs amount, currency, unit and quoting party: {key}")
            annotations[key] = row
    return annotations


def export(db, states, directory, annotation_path):
    videos = {video["video_id"]: dict(video) | {"state": state, "next_due": due}
              for video, state, due, _ in states}
    sources = defaultdict(list)
    discoveries = [dict(row) for row in db.execute("SELECT * FROM discoveries ORDER BY found_at, keyword")
                   if row["video_id"] in videos]
    for row in discoveries:
        sources[row["video_id"]].append(row["keyword"])
    for video in videos.values():
        video["keywords"] = "|".join(sorted(set(sources[video["video_id"]]) - {""}))
        video["published_at"] = video["published_at"] or "unknown"
    comments = [dict(row) for row in db.execute("SELECT * FROM comments ORDER BY first_round, video_id, comment_id")
                if row["video_id"] in videos]
    comment_keys = {(row["video_id"], row["comment_id"]) for row in comments}
    annotations = read_annotations(annotation_path, comment_keys)
    excluded_videos = {
        video_id: note["review_note"]
        for (video_id, _), note in annotations.items()
        if note.get("video_scope") == "other" and note.get("review_note")
    }
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_reviews'").fetchone():
        for video_id, evidence in db.execute(
            "SELECT video_id, evidence FROM candidate_reviews WHERE decision='exclude'"
        ):
            excluded_videos.setdefault(video_id, evidence)
    attempts = [dict(row) for row in db.execute("SELECT * FROM attempts ORDER BY id") if row["video_id"] in videos]
    leads = []
    groups = defaultdict(list)
    round2 = defaultdict(list)
    for row in comments:
        vid = row["video_id"]
        video = videos[vid]
        note = annotations.get((vid, row["comment_id"]), {})
        body = row["text"] or ""
        tags, auto_destination = classify(body)
        if note.get("topics"):
            tags = set(note["topics"].split("|")) if note["topics"] != "-" else set()
        role = note.get("role") or (
            "merchant" if row["author"] and row["author"].lstrip("@").casefold()
            == video["account"].lstrip("@").casefold() else "unknown"
        )
        buyer_topics = tags if role != "merchant" else set()
        destination = note.get("destination") or auto_destination
        evidence = note.get("destination_evidence") or (
            body if not note.get("destination") and auto_destination != "unknown" else ""
        )
        parent = row["parent_id"]
        row.update(
            url=video["url"], account=video["account"], keywords=video["keywords"],
            material=video["material"],
            sample_group="raw" if video["material"] in RAW and vid not in excluded_videos else "other",
            scope_note=excluded_videos.get(vid, ""),
            merchant_country=video["merchant_country"], location_evidence=video["location_evidence"],
            language=note.get("language") or (
                "ar_candidate" if re.search(r"[\u0621-\u064a]", body) else "unknown"
            ),
            delivery_destination=destination, delivery_evidence=evidence,
            market_signal=f"merchant={video['merchant_country']};delivery={destination}",
            kind="reply" if parent else "top_level",
            parent_status=("captured" if (vid, parent) in comment_keys else "not_captured") if parent else "",
            role=role, topics="|".join(sorted(buyer_topics)),
            usage=note.get("usage") or (
                "processing" if re.search(r"معمول|مبسوس|خلط|مسقى|زيت عطري", body) else
                "direct_burning" if re.search(r"تبخير|للحرق|احرق", body) else "unknown"
            ),
            procurement_signal=("wholesale" if buyer_topics & {"明确批发或供货", "起订量"} else
                                "quantity_request" if QUANTITY_REQUEST in buyer_topics else
                                "bulk" if "公斤报价" in buyer_topics else
                                "purchase_question" if buyer_topics else ""),
            review_status="reviewed" if note.get("topics") else "heuristic",
            reviewed_intent=note.get("reviewed_intent", ""),
            translation=note.get("translation", ""), review_note=note.get("review_note", ""),
            posted_at=row["posted_at"] or "unknown",
        )
        for field in ("quote_amount", "quote_currency", "quote_unit", "quote_by"):
            row[field] = note.get(field, "")
        for topic in sorted(buyer_topics):
            leads.append(row | {"question": topic})
            groups[(row["sample_group"], row["market_signal"], row["material"], topic)].append(row)
        if row["first_round"] == 2:
            round2[(row["sample_group"], row["timing"])].append(row)

    directory.mkdir(parents=True, exist_ok=True)
    video_fields = """video_id url account published_at discovered_at followup_due archived_at material
                      merchant_country location_evidence keywords stage state next_due""".split()
    comment_fields = """video_id url account keywords material sample_group merchant_country location_evidence
                        language delivery_destination delivery_evidence market_signal comment_id kind parent_id
                        parent_status text posted_at collected_at first_round timing author role topics
                        procurement_signal review_status reviewed_intent translation review_note scope_note usage
                        quote_amount quote_currency quote_unit quote_by attempt_id""".split()
    attempt_fields = """id video_id stage source started_at lease_until finished_at status detail
                        rows_received new_comments partial_rows""".split()
    write_csv(directory / "videos.csv", video_fields, videos.values())
    write_csv(directory / "discoveries.csv", ["video_id", "source_url", "keyword", "found_at"], discoveries)
    write_csv(directory / "comments.csv", comment_fields, comments)
    write_csv(directory / "attempts.csv", attempt_fields, attempts)
    write_csv(directory / "market_leads.csv", ["question"] + comment_fields, leads)
    summaries = [
        dict(sample_group=sample, market_signal=market, material=material, question=topic,
             comments=len(rows), videos=len({row["video_id"] for row in rows}),
             accounts=len({row["account"].casefold() for row in rows}))
        for (sample, market, material, topic), rows in sorted(groups.items())
    ]
    write_csv(directory / "market_summary.csv",
              "sample_group market_signal material question comments videos accounts".split(), summaries)
    demand_groups = defaultdict(list)
    for row in leads:
        if row["sample_group"] == "raw":
            demand_groups[(row["market_signal"], row["material"], row["usage"], row["question"])].append(row)
    demand = [
        dict(market_signal=market, material=material, usage=usage, question=topic,
             comments=len(rows), videos=len({row["video_id"] for row in rows}),
             accounts=len({row["account"].casefold() for row in rows}))
        for (market, material, usage, topic), rows in sorted(demand_groups.items())
    ]
    write_csv(directory / "market_demand.csv",
              "market_signal material usage question comments videos accounts".split(), demand)
    scenarios = export_profit(Path(__file__).with_name("profit_inputs.csv"), directory / "profit_scenarios.csv")
    timing_counts = [
        dict(sample_group=sample, timing=timing, new_comments=len(rows),
             procurement_comments=sum(bool(row["topics"]) for row in rows),
             procurement_videos=len({row["video_id"] for row in rows if row["topics"]}),
             procurement_accounts=len({row["account"].casefold() for row in rows if row["topics"]}))
        for (sample, timing), rows in sorted(round2.items())
    ]
    write_csv(directory / "round2_new.csv",
              "sample_group timing new_comments procurement_comments procurement_videos procurement_accounts".split(),
              timing_counts)
    stats = {
        "videos": len(videos),
        "video_states": dict(Counter(video["state"] for video in videos.values())),
        "attempts_by_round": {str(stage): dict(Counter(row["status"] for row in attempts if row["stage"] == stage))
                              for stage in (1, 2)},
        "attempt_sources": dict(Counter(row["source"] for row in attempts)),
        "comments": len(comments),
        "replies": sum(row["kind"] == "reply" for row in comments),
        "round2_new_comments": sum(map(len, round2.values())),
        "round2_procurement_comments": sum(row["procurement_comments"] for row in timing_counts),
        "round2_breakdown": timing_counts,
    }
    (directory / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_analysis(directory / "market_analysis.md", comments, leads, stats, scenarios)
