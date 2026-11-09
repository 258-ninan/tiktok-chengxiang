"""Render an evidence-led Chinese report from the current export snapshot."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone


MATERIAL_NAMES = {
    "chips": "木片",
    "crumbs": "碎料",
    "chunks": "块料",
    "mixed_raw": "木料形态未细分或混合",
}
TOPIC_ORDER = (
    "零售询价", "公斤报价", "明确批发或供货", "起订量", "样品", "指定数量采购",
    "产地", "天然或处理料", "燃烧表现", "配送",
)
REVIEWED_INTENTS = {
    "wholesale_inquiry": "明确批发或起订量询问",
    "quantity_request": "指定数量的采购请求",
    "sample_request": "明确索样",
    "delivery_inquiry": "配送或到货价询问",
    "quality_inquiry": "天然、处理或燃烧表现询问",
    "purchase_feedback": "自述购买或收样",
}


def counts(rows):
    return len(rows), len({row["video_id"] for row in rows}), len({row["account"].casefold() for row in rows})


def markdown_text(text):
    return " ".join((text or "").split()).replace("\\", "\\\\").replace("|", "\\|").replace(
        "<", "&lt;"
    ).replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")


def counts_row(name, rows):
    comments, videos, accounts = counts(rows)
    return f"| {name} | {comments} | {videos} | {accounts} |"


def write_analysis(path, comments, leads, stats, scenarios):
    raw = [row for row in comments if row["sample_group"] == "raw"]
    candidates = [row for row in raw if row["topics"]]
    other = [row for row in comments if row["sample_group"] != "raw"]
    materials = defaultdict(list)
    topics = defaultdict(list)
    intents = defaultdict(list)
    replies = defaultdict(list)
    for row in raw:
        materials[row["material"]].append(row)
        if row["reviewed_intent"] in REVIEWED_INTENTS:
            intents[row["reviewed_intent"]].append(row)
        if row["role"] == "merchant" and row["parent_id"]:
            replies[row["video_id"], row["parent_id"]].append(row)
    for row in leads:
        if row["sample_group"] == "raw":
            topics[row["question"]].append(row)

    total, videos, accounts = counts(raw)
    _, signal_videos, signal_accounts = counts(candidates)
    reviewed = sum(row["review_status"] == "reviewed" for row in comments)
    unclassified = len(materials["mixed_raw"])
    prices = [row for row in candidates if row["kind"] == "top_level"
              and {"零售询价", "公斤报价"}.intersection(row["topics"].split("|"))]
    quotes = [row for row in raw if row["role"] == "merchant"
              and all(row[field] for field in ("quote_amount", "quote_currency", "quote_unit", "quote_by"))]
    price_keys = {(row["video_id"], row["comment_id"]) for row in prices}
    answered = sum(key in replies for key in price_keys)
    quoted = len({(row["video_id"], row["parent_id"]) for row in quotes} & price_keys)
    bulk = len(intents["quantity_request"])
    wholesale = len(intents["wholesale_inquiry"])
    current_date = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 沉香原料出海验证报告",
        "",
        f"{current_date}（北京时间）生成。库内 {stats['videos']} 个去重视频链接、{stats['comments']} 条评论。"
        f"原料候选口径纳入 {total} 条评论，覆盖 {videos} 个视频、{accounts} 个发布账号。"
        f"{len(other)} 条评论归入其他商品或已排除视频。",
        "",
        "## 当前值得验证的机会",
        "",
    ]
    if materials["crumbs"]:
        _, crumb_videos, crumb_accounts = counts(materials["crumbs"])
        lines.append(f"- **先验证碎料规格。** 可辨识的碎料样本覆盖 {crumb_videos} 个视频、{crumb_accounts} 个发布账号。"
                     "将直接熏烧与制作熏香的用途分开记录，并按粒径、天然或处理状态和等级分别询价。")
    if bulk or wholesale:
        lines.append(f"- **核对批量采购条件。** 定向复核已确认 {bulk} 条指定数量的采购请求和 {wholesale} 条明确批发或起订量询问。"
                     "用规格、数量、MOQ、样品和交付条件核实意向，保留原评论和卖家回复。")
    if prices:
        lines.append(f"- **报价需要补证。** {len(prices)} 条一级价格询问中，采到卖家回复的有 {answered} 条，"
                     f"其中 {quoted} 条已关联原文核对过的数字报价。优先补齐币种、等级、计价单位、折扣和运费条件。")
    if not total:
        lines.append("- 当前缺少有评论的原料候选视频，待有样本后再比较细分机会。")
    lines += [
        "", "## 已核对的原文与用途", "",
        f"本次导出有 {reviewed} 条原文核对记录。核对集中在高意向、报价和易误判评论，属于定向抽样。",
        "", "| 证据类型 | 评论 | 视频 | 发布账号 |", "| --- | ---: | ---: | ---: |",
    ]
    for key, label in REVIEWED_INTENTS.items():
        lines.append(counts_row(label, intents[key]))
    if bulk:
        processing = sum(row["usage"] == "processing" for row in intents["quantity_request"])
        direct = sum(row["usage"] == "direct_burning" for row in intents["quantity_request"])
        lines += ["", f"指定数量的请求中，{processing} 条明确用于加工，{direct} 条明确用于直接熏烧，"
                  f"{bulk - processing - direct} 条用途待核。每条复核评论只计一个主要意图。"]
    lines.append("")
    for key in REVIEWED_INTENTS:
        examples = [row for row in intents[key] if row["translation"]]
        examples.sort(key=lambda row: (row["usage"] != "processing", row["posted_at"], row["comment_id"]))
        for row in examples[:2 if key in {"wholesale_inquiry", "quantity_request", "delivery_inquiry"} else 1]:
            note = markdown_text(row["review_note"])
            lines.append(f"- **{REVIEWED_INTENTS[key]}。** {markdown_text(row['translation'])}"
                         f"{' ' + note if note else ''} 原文「{markdown_text(row['text'])}」。 [原视频]({row['url']})")

    lines += ["", "## 卖家原话与报价口径", ""]
    if quotes:
        lines += [
            f"已核对 {len(quotes)} 条卖家报价，来自 {counts(quotes)[2]} 个发布账号。"
            "按原币种和单位列示。不同等级、规格和运费口径需分别核价。",
            "", "| 形态 | 卖家 | 原报价 | 上下文 | 来源 |", "| --- | --- | --- | --- | --- |",
        ]
        for row in quotes:
            amount = f"{markdown_text(row['quote_amount'])} {markdown_text(row['quote_currency'])} / {markdown_text(row['quote_unit'])}"
            lines.append(f"| {MATERIAL_NAMES[row['material']]} | {markdown_text(row['quote_by'])} | {amount} | "
                         f"{markdown_text(row['review_note'] or row['translation'])} | [原视频]({row['url']}) |")
    else:
        lines.append("当前暂无已核对的数字报价。优先查看公斤询价下的发布账号回复并补全报价口径。")

    lines += ["", "## 沙特和阿联酋的证据", "",
              "| 配送地 | 非发布者明确提及 | 发布者明确提及 | 已核实所在地的视频 |",
              "| --- | ---: | ---: | ---: |"]
    for code, label in (("SA", "沙特"), ("AE", "阿联酋")):
        to_country = [row for row in raw if code in row["delivery_destination"].split("|")]
        merchant = sum(row["role"] == "merchant" for row in to_country)
        located = len({row["video_id"] for row in raw if row["merchant_country"] == code})
        lines.append(f"| {label} | {len(to_country) - merchant} | {merchant} | {located} |")
    lines += ["", "配送地来自评论提及，所在地来自单独核实。地区优先级仍需要独立商家和同规格报价补证。",
              "", "## 样本覆盖与关键词统计", "",
              f"采购主题候选共 {len(candidates)} 条，覆盖 {signal_videos} 个视频、{signal_accounts} 个发布账号。"
              f"其中 {unclassified} 条原料评论的形态仍待细分。评论可以涉及多个主题，结果受选样和单视频采样上限影响。",
              "", "| 原料形态 | 评论 | 视频 | 发布账号 | 采购候选 |", "| --- | ---: | ---: | ---: | ---: |"]
    for material, name in MATERIAL_NAMES.items():
        rows = materials[material]
        n, v, a = counts(rows)
        lines.append(f"| {name} | {n} | {v} | {a} | {sum(bool(row['topics']) for row in rows)} |")
    lines += ["", "| 关键词和复核标注主题 | 评论 | 视频 | 发布账号 |", "| --- | ---: | ---: | ---: |"]
    topic_labels = {"零售询价": "一般询价（规模待核）", "公斤报价": "公斤量级询价"}
    lines.extend(counts_row(topic_labels.get(topic, topic), topics[topic]) for topic in TOPIC_ORDER)
    second_round = stats["attempts_by_round"]["2"]
    second_count = second_round.get("success", 0) + second_round.get("empty_confirmed", 0)
    lines += ["", f"二轮有效观察覆盖 {second_count} 个视频。"
              + (f"二轮新增采购候选 {stats['round2_procurement_comments']} 条。" if second_count else "三天补采收益仍待验证。"),
              "", "## 利润核算与待补数据", ""]
    calculated = sum(row["status"] == "calculated" for row in scenarios)
    lines += [
        f"利润表中 {calculated}/{len(scenarios)} 个情景具备完整输入。单条公开询价或卖家回帖可用于确定核价对象。",
        "每个目标规格需要同等级实际销售报价、采购价、订单重量和损耗，以及包装、运输、进口、收款和获客费用。"
        "当前样本用于选择验证对象和问题，市场销量及利润需要另行核算。",
        "", "[评论与复核标注](comments.csv) · [采购候选](market_leads.csv) · [利润情景](profit_scenarios.csv)", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
