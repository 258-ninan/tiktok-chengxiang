"""Auditable local discovery: metadata only, no video media or credentials."""

import argparse
import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import collect
from report_output import write_csv
from sources import canonical, utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_observations (
 job_id TEXT NOT NULL, kind TEXT NOT NULL, seed TEXT NOT NULL,
 video_id TEXT NOT NULL, url TEXT NOT NULL, account TEXT NOT NULL,
 caption TEXT NOT NULL, published_at TEXT, comment_count INTEGER,
 decision TEXT NOT NULL, reason TEXT NOT NULL, material TEXT,
 observed_at TEXT NOT NULL, PRIMARY KEY(job_id,video_id)
);
CREATE INDEX IF NOT EXISTS discovery_observation_video ON discovery_observations(video_id);
CREATE TABLE IF NOT EXISTS candidate_reviews (
 video_id TEXT PRIMARY KEY, decision TEXT NOT NULL, material TEXT,
 evidence TEXT NOT NULL, reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_progress (
 kind TEXT NOT NULL, seed TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
 seen_ids TEXT NOT NULL DEFAULT '[]', no_growth INTEGER NOT NULL DEFAULT 0,
 last_checked TEXT, next_due TEXT, last_status TEXT NOT NULL DEFAULT 'new',
 PRIMARY KEY(kind,seed)
);
CREATE TABLE IF NOT EXISTS discovery_pages (
 job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, seed TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
 requested_depth INTEGER NOT NULL, reached_depth INTEGER NOT NULL,
 returned_count INTEGER NOT NULL, unseen_count INTEGER NOT NULL,
 eligible_count INTEGER NOT NULL, added_count INTEGER NOT NULL,
 diagnostics TEXT NOT NULL
);
"""


def initialize(db):
    db.executescript(SCHEMA)


def decision(item, floor, timestamp, review=None):
    if not item.get('createTimeISO'):
        return 'review', 'missing_publish_time', None
    if not floor <= item['createTimeISO'] <= timestamp:
        return 'excluded', 'outside_publish_window', None
    if review and review['decision'] == 'exclude':
        return 'excluded', 'review_excluded', None
    if item.get('commentCount') == 0:
        return 'excluded', 'no_comments_observed', None
    material = review['material'] if review and review['decision'] == 'raw' else collect.raw_material(item['text'])
    if not material:
        return 'review', 'product_not_confirmed_from_caption', None
    # Unknown comment counts remain candidates; an empty page will not count as success.
    return 'accepted', 'review_confirmed_raw' if review else 'raw_caption_signal', material


def record_page(db, cfg, job, items, options, diagnostics, timestamp, *, on_finish=None):
    """One atomic commit for candidates, queue entries, cursor and coverage log."""
    floor = utc(options['history_floor'])
    seed = job['seed']
    ids = set()
    eligible = []
    reached = max(0, min(int(diagnostics.get('reachedDepth', 0)), job['targetDepth']))
    with collect.transaction(db):
        old = db.execute('SELECT * FROM discovery_progress WHERE kind=? AND seed=?',
                         (job['kind'], seed)).fetchone()
        seen = set(json.loads(old['seen_ids'])) if old else set()
        before = db.execute('SELECT COUNT(*) FROM videos').fetchone()[0]
        for item in items:
            vid, url, account = canonical(item['webVideoUrl'])
            ids.add(vid)
            review = db.execute('SELECT * FROM candidate_reviews WHERE video_id=?', (vid,)).fetchone()
            status, reason, material = decision(item, floor, timestamp, review)
            if job['kind']=='profile' and account.casefold()!=seed.casefold():
                status,reason,material='excluded','different_profile_account',None
            if job['kind']=='metadata' and account.casefold()!=job['account'].casefold():
                status,reason,material='excluded','different_profile_account',None
            db.execute('INSERT OR IGNORE INTO discovery_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job['id'], job['kind'], seed, vid, url, account, item['text'],
                        item.get('createTimeISO'), item.get('commentCount'), status, reason, material, timestamp))
            if status == 'accepted':
                first=db.execute('SELECT MIN(observed_at) FROM discovery_observations WHERE video_id=?',(vid,)).fetchone()[0]
                eligible.append((vid, url, account, item['createTimeISO'], first or timestamp, material,
                                 'unknown', '', {'url': url, 'keywords': seed}))
        collect.store_seeds(db, cfg, eligible, automatic=True)
        added = db.execute('SELECT COUNT(*) FROM videos').fetchone()[0] - before
        unseen = len(ids - seen)
        streak = 0 if unseen else (old['no_growth'] if old else 0) + 1
        # Empty/slow pages get a cooldown, never a claim of complete platform coverage.
        exhausted = (reached >= options.get('max_discovery_scrolls', 80) or streak >= 2 or
                     (unseen==0 and diagnostics.get('stopReason')=='no_scroll_or_result_growth'))
        next_due = collect.after(timestamp, hours=6 if exhausted else 0)
        cursor_depth = 0 if exhausted else reached
        seen.update(ids)
        db.execute('''INSERT INTO discovery_progress VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(kind,seed) DO UPDATE SET depth=excluded.depth,seen_ids=excluded.seen_ids,
          no_growth=excluded.no_growth,last_checked=excluded.last_checked,
          next_due=excluded.next_due,last_status=excluded.last_status''',
                   (job['kind'], seed, cursor_depth, json.dumps(sorted(seen)), streak, timestamp,
                    next_due, 'cooldown' if exhausted else 'continue'))
        db.execute('INSERT INTO discovery_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (job['id'], job['kind'], seed, job['started_at'], timestamp, 'observed' if ids else 'empty_unverified',
                    job['targetDepth'], reached, len(ids), unseen, len(eligible), added,
                    json.dumps(diagnostics, ensure_ascii=False)))
        recent = collect.after(timestamp, days=-7)
        for name, subset in (
            ('recent_7_days', [x for x in items if x.get('createTimeISO') and max(floor,recent) <= x['createTimeISO'] <= timestamp]),
            ('history_since_floor', [x for x in items if x.get('createTimeISO') and floor <= x['createTimeISO'] < recent]),
        ):
            if job['kind'] == 'profile' and name == 'recent_7_days':
                continue
            # Native coverage is canonical in discovery_pages; legacy rows retain zero additions.
            times = [x['createTimeISO'] for x in subset]
            db.execute('''INSERT INTO local_searches(keyword,window_name,started_at,finished_at,status,
              returned_count,eligible_count,new_count,observed_oldest,detail)
              VALUES (?,?,?,?,'success',?,?,0,?,?)''',
                       (seed, 'account_history' if job['kind'] == 'profile' else name,
                        job['started_at'], timestamp, len(subset),
                        sum(decision(x,floor,timestamp)[0]=='accepted' for x in subset),
                        min(times) if times else None,
                        'See discovery_pages.csv for canonical additions, actual depth and duplicate yield.'))
        if job['kind'] == 'profile':
            db.execute("""INSERT INTO local_profiles VALUES (?,?)
                          ON CONFLICT(account) DO UPDATE SET last_checked=excluded.last_checked""",
                       (seed, timestamp))
        if on_finish:
            on_finish()
    collect.log(f"Discovery {job['kind']} seed={seed} depth={reached}/{job['targetDepth']} "
                f"observed={len(ids)} unseen={unseen} new={added}")
    return {'observed': len(ids), 'unseen': unseen, 'added': added, 'reached': reached, 'cooldown': exhausted}


def export_audit(db, directory):
    observations = [dict(r) for r in db.execute('SELECT * FROM discovery_observations ORDER BY observed_at,video_id')]
    columns = 'job_id kind seed video_id url account caption published_at comment_count decision reason material observed_at'.split()
    write_csv(directory/'discovery_candidates.csv', columns, observations)
    excluded = [r for r in observations if r['decision'] != 'accepted']
    write_csv(directory/'excluded_candidates.csv', columns, excluded)
    # Sample independently within each rejection reason. No model-labelled accuracy claims.
    latest = {}
    latest_all={r['video_id']:r for r in observations}
    for row in excluded:
        latest[row['video_id']] = row
    reviews={r['video_id']:r for r in db.execute('SELECT * FROM candidate_reviews')}
    sample = sorted(latest.values(), key=lambda r:(r['reason'],hashlib.sha256(r['video_id'].encode()).hexdigest()))
    buckets = {}
    for row in sample:
        buckets.setdefault(row['reason'], []).append(row)
    chosen = []
    sample_buckets={key:[row for row in bucket if row['video_id'] not in reviews and
                        latest_all[row['video_id']]['decision']!='accepted'] for key,bucket in buckets.items()}
    for index in range(50):
        for bucket in sample_buckets.values():
            if index < len(bucket) and len(chosen) < 50:
                row=bucket[index]
                chosen.append(row | {'review_decision':'','review_material':'','review_evidence':''})
    write_csv(directory/'candidate_review_sample.csv', columns + ['review_decision','review_material','review_evidence'], chosen)
    for table in ('discovery_pages','discovery_progress','candidate_reviews'):
        cursor = db.execute(f'SELECT * FROM {table}')
        write_csv(directory/f'{table}.csv', [c[0] for c in cursor.description], [dict(r) for r in cursor])
    summary=[]
    for reason in sorted(buckets):
        group=buckets[reason]
        checked=[reviews[r['video_id']] for r in group if r['video_id'] in reviews]
        summary.append(dict(reason=reason,candidates=len(group),reviewed=len(checked),
                            confirmed_raw=sum(r['decision']=='raw' for r in checked),
                            confirmed_exclude=sum(r['decision']=='exclude' for r in checked)))
    write_csv(directory/'candidate_review_summary.csv',
              ['reason','candidates','reviewed','confirmed_raw','confirmed_exclude'],summary)


def import_reviews(db, cfg, path, floor, timestamp):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        entries = list(csv.DictReader(stream))
    count = 0
    with collect.transaction(db):
        for row in entries:
            action = row.get('review_decision','').strip()
            if not action:
                continue
            material = row.get('review_material','').strip()
            evidence = row.get('review_evidence','').strip()
            if action not in {'raw','exclude'} or not evidence or (action=='raw' and material not in {'chips','crumbs','chunks','mixed_raw'}):
                raise ValueError('Review requires raw/exclude, evidence and a raw material for raw decisions')
            candidate = db.execute('SELECT * FROM discovery_observations WHERE video_id=? ORDER BY observed_at DESC LIMIT 1', (row['video_id'],)).fetchone()
            if not candidate:
                raise ValueError('Review video is absent from recorded candidates')
            db.execute('INSERT INTO candidate_reviews VALUES (?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET decision=excluded.decision,material=excluded.material,evidence=excluded.evidence,reviewed_at=excluded.reviewed_at',
                       (row['video_id'],action,material or None,evidence,timestamp))
            item = dict(text=candidate['caption'],createTimeISO=candidate['published_at'],commentCount=candidate['comment_count'])
            review = dict(decision=action,material=material)
            if decision(item,floor,timestamp,review)[0]=='accepted':
                first=db.execute('SELECT MIN(observed_at) FROM discovery_observations WHERE video_id=?',(candidate['video_id'],)).fetchone()[0]
                collect.store_seeds(db,cfg,[(candidate['video_id'],candidate['url'],candidate['account'],candidate['published_at'],first or timestamp,material,'unknown','',{'url':candidate['url'],'keywords':candidate['seed']})],automatic=True)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['export','review'])
    parser.add_argument('--file',type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    import local_collect
    (root / 'data').mkdir(exist_ok=True)
    with local_collect.run_lock(), closing(sqlite3.connect(
        root / 'data' / 'comments.sqlite', timeout=20, isolation_level=None
    )) as db:
        db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(collect.SCHEMA + local_collect.LOCAL_SCHEMA)
        initialize(db)
        if args.command=='review':
            if not args.file:
                parser.error('review requires --file')
            print('Reviews imported:',import_reviews(db,collect.config(root/'config.json'),args.file,utc(local_collect.settings()['history_floor']),datetime.now(UTC).isoformat()))
        local_collect.export_local(db,collect.config(root/'config.json'))


if __name__=='__main__':
    main()
