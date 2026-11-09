import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import chrome_bridge
import collect
import discovery
import local_collect
from local_browser import video_items


STAMP='2026-11-05T12:00:00+00:00'
FLOOR='2025-12-31T20:00:00+00:00'


class DiscoveryWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        self.db=sqlite3.connect(self.root/'test.sqlite',isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.row_factory=sqlite3.Row
        self.db.executescript(collect.SCHEMA+local_collect.LOCAL_SCHEMA)
        discovery.initialize(self.db)
        self.cfg=collect.config(Path('config.json'))
        self.options=local_collect.settings()

    def item(self,vid='900000000000000001',caption='عود مروكي طبيعي',account='fixture',posted='2026-02-01T00:00:00+00:00',count=10):
        return dict(webVideoUrl=f'https://www.tiktok.com/@{account}/video/{vid}',text=caption,createTimeISO=posted,commentCount=count)

    def job(self,jobid='a',kind='search',seed='عود مروكي',resume=0):
        return dict(id=jobid,kind=kind,seed=seed,started_at=STAMP,resumeDepth=resume,targetDepth=resume+20)

    def record(self,job,items,depth=20):
        return discovery.record_page(self.db,self.cfg,job,items,self.options,
                                     {'protocol':2,'reachedDepth':depth,'stopReason':'target_depth'},STAMP)

    def test_local_product_aliases_and_non_raw_exclusions(self):
        for text in ['عود طبيعي','عود مروكي طبيعي','عود موروكي','عود فيتنامي طبيعي','عود محسن',
                     'العود المروكي (تربل سوبر)','اطلب الآن عود #موروكي_طبيعي !!']:
            self.assertEqual(collect.raw_material(text),'mixed_raw',text)
        for text in ['دقة مروكي','دقه مروكي','دقة كلمنتان','كسر مباخر']:
            self.assertEqual(collect.raw_material(text),'crumbs',text)
        for text in ['دهن عود طبيعي','زيت العود','عطر عود مروكي','عزف العود الطبيعي']:
            self.assertIsNone(collect.raw_material(text),text)
        for text, material in (
            ("Agarwood wood chips for burning", "chips"),
            ("Agarwood powder supplier", "crumbs"),
            ("Oud chunks wholesale", "chunks"),
            ("Agarwood essential oil", None),
        ):
            self.assertEqual(collect.raw_material(text), material)

    def test_local_report_bundle_keeps_previous_batch_on_render_or_publish_failure(self):
        with patch.object(local_collect, "ROOT", self.root):
            local_collect.export_local(self.db, self.cfg)
            archive = self.root / "out" / "auto" / "reports.zip"
            previous = archive.read_bytes()
            with ZipFile(archive) as bundle:
                self.assertIsNone(bundle.testzip())
                self.assertIn("chrome_comment_coverage.csv", bundle.namelist())
                self.assertIn("discovery_candidates.csv", bundle.namelist())
                self.assertEqual(json.loads(bundle.read("stats.json"))["videos"], 0)

            self.record(self.job(), [self.item()])
            for target in ("discovery.export_audit", "report_output.Path.replace"):
                with self.subTest(target=target), patch(target, side_effect=PermissionError("busy")):
                    with self.assertRaises(PermissionError):
                        local_collect.export_local(self.db, self.cfg)
                    self.assertEqual(archive.read_bytes(), previous)
                    self.assertEqual(list(archive.parent.iterdir()), [archive])

            local_collect.export_local(self.db, self.cfg)
            with ZipFile(archive) as bundle:
                self.assertIsNone(bundle.testzip())
                self.assertEqual(json.loads(bundle.read("stats.json"))["videos"], 1)

    def test_new_seed_attribution_does_not_reset_existing_archive_or_time(self):
        item=self.item()
        self.record(self.job(),[item])
        self.db.execute('UPDATE videos SET stage=3,archived_at=?',(STAMP,))
        original=dict(self.db.execute('SELECT * FROM videos').fetchone())
        self.record(self.job('b',seed='عود طبيعي'),[item])
        current=dict(self.db.execute('SELECT * FROM videos').fetchone())
        self.assertEqual(original,current)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM discoveries').fetchone()[0],2)

    def test_page_continuation_persists_and_repeated_results_cool_down(self):
        item=self.item()
        self.assertEqual(self.record(self.job(),[item])['unseen'],1)
        with patch.object(chrome_bridge,'ROOT',self.root):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            bridge.active_seeds['search']='عود مروكي'
            with patch.object(local_collect,'now',return_value=STAMP):
                job=bridge.discovery_job('search')
        self.assertEqual((job['resumeDepth'],job['targetDepth']),(20,40))
        self.record(self.job('b',resume=20),[item],40)
        final=self.record(self.job('c',resume=40),[item],60)
        self.assertTrue(final['cooldown'])
        progress=self.db.execute('SELECT * FROM discovery_progress').fetchone()
        self.assertEqual(progress['next_due'],'2026-11-05T18:00:00+00:00')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],1)

    def test_all_excluded_candidates_retained_and_review_can_recover(self):
        items=[self.item('901',caption='عود بالجملة'),self.item('902',posted='2025-01-01T00:00:00+00:00'),
               self.item('903',posted=None),self.item('904',count=0)]
        self.record(self.job(),items)
        reasons={r['reason'] for r in self.db.execute('SELECT * FROM discovery_observations')}
        self.assertEqual(reasons,{'product_not_confirmed_from_caption','outside_publish_window','missing_publish_time','no_comments_observed'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        discovery.export_audit(self.db,self.root)
        with (self.root/'candidate_review_sample.csv').open(encoding='utf-8-sig',newline='') as stream:
            sample=list(csv.DictReader(stream))
        raw=next(x for x in sample if x['video_id']=='901')
        raw.update(review_decision='raw',review_material='chunks',review_evidence='Human page inspection: solid wood for burning')
        path=self.root/'review.csv'
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=raw.keys());writer.writeheader();writer.writerow(raw)
        self.assertEqual(discovery.import_reviews(self.db,self.cfg,path,FLOOR,collect.after(STAMP,hours=1)),1)
        self.assertEqual(self.db.execute('SELECT material FROM videos').fetchone()[0],'chunks')
        self.assertEqual(self.db.execute('SELECT discovered_at FROM videos').fetchone()[0],STAMP)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM discovery_observations').fetchone()[0],4)

    def test_profile_jobs_use_raw_account_and_exclude_other_accounts(self):
        self.record(self.job(),[self.item()])
        with patch.object(chrome_bridge,'ROOT',self.root),patch.object(local_collect,'now',return_value=STAMP):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            job=bridge.discovery_job('profile')
        self.assertEqual(job['url'],'https://www.tiktok.com/@fixture')
        self.record(self.job('profile',kind='profile',seed='@fixture'),
                    [self.item('905',account='fixture'),self.item('906',account='different')])
        self.assertEqual(self.db.execute("SELECT reason FROM discovery_observations WHERE video_id='906'").fetchone()[0],'different_profile_account')
        self.assertFalse(self.db.execute("SELECT 1 FROM videos WHERE video_id='906'").fetchone())

    def test_incomplete_metadata_is_audited_instead_of_silently_disappearing(self):
        items=video_items([{'id':'901','desc':'عود طبيعي','author':{'uniqueId':'fixture'},'stats':{'commentCount':'unknown'}}],keep_incomplete=True)
        self.assertEqual(len(items),1)
        self.assertIsNone(items[0]['createTimeISO'])
        self.record(self.job(),items)
        self.assertEqual(self.db.execute('SELECT reason FROM discovery_observations').fetchone()[0],'missing_publish_time')

    def test_discovery_is_interleaved_after_three_videos_and_due_followup_wins(self):
        self.record(self.job(),[self.item(str(900+i)) for i in range(4)])
        with patch.object(chrome_bridge,'ROOT',self.root),patch.object(local_collect,'export_local'):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            for index in range(3):
                job=bridge.job()
                self.assertEqual(job['kind'],'video')
                bridge.checkpoint(dict(jobId=job['id'],number=1,parentId=None,cursor="0",
                    hasMore=False,nextCursor=None,valid=True,final=True,comments=[
                    dict(cid=str(100+index),text='كم سعر الكيلو؟',aweme_id=job['url'].split('/')[-1],
                         reply_total=0,create_time=1793000000,user={'unique_id':'buyer'})]))
                bridge.result(dict(jobId=job['id'],status='observed'))
            self.assertEqual(bridge.job()['kind'],'search')
            bridge.current=None
            bridge.video_streak=3
            self.db.execute("UPDATE videos SET stage=2,followup_due='2026-01-01T00:00:00+00:00' WHERE video_id='903'")
            self.assertEqual(bridge.job()['kind'],'video')

    def test_restoring_run_preserves_deadline_and_baselines(self):
        from datetime import UTC,datetime,timedelta
        stamp=datetime.now(UTC)
        previous=dict(started_at=(stamp-timedelta(minutes=10)).isoformat(),
                      planned_end_at=(stamp+timedelta(minutes=30)).isoformat(),
                      duration_minutes=40,baseline_comments=0,baseline_videos=0)
        with patch.object(chrome_bridge,'ROOT',self.root):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            bridge.restore(previous)
            bridge.save_status()
        self.assertEqual(bridge.status['planned_end_at'],previous['planned_end_at'])
        self.assertEqual(bridge.status['baseline_videos'],0)
        self.assertTrue(bridge.continuous)

    def test_profile_link_without_date_is_verified_before_comment_queue(self):
        self.record(self.job('profile-link',kind='profile',seed='@fixture'),[self.item('907',posted=None)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
        with patch.object(chrome_bridge,'ROOT',self.root),patch.object(local_collect,'export_local'):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            job=bridge.job()
            self.assertEqual(job['kind'],'metadata')
            bridge.result(dict(jobId=job['id'],status='observed',diagnostics={'protocol':int(chrome_bridge.PROTOCOL),'reachedDepth':0},
                               videos=[dict(id='907',desc='عود مروكي طبيعي',author={'uniqueId':'fixture'},
                                            createTime=1793000000,stats={'commentCount':30})]))
            self.assertEqual(self.db.execute('SELECT discovered_at FROM videos').fetchone()[0],STAMP)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comments').fetchone()[0],0)
            self.assertIsNone(bridge.metadata_job())

    def test_missing_detail_metadata_gets_cooldown_without_creating_video_task(self):
        self.record(self.job('profile-link',kind='profile',seed='@fixture'),[self.item('908',posted=None)])
        with patch.object(chrome_bridge,'ROOT',self.root),patch.object(local_collect,'export_local'):
            bridge=chrome_bridge.Bridge(self.db,self.cfg,self.options)
            job=bridge.job()
            self.assertEqual(job['kind'],'metadata')
            bridge.result(dict(jobId=job['id'],status='observed',diagnostics={'protocol':int(chrome_bridge.PROTOCOL)},videos=[]))
            self.assertIsNone(bridge.metadata_job())
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)
            self.assertEqual(self.db.execute("SELECT status FROM discovery_pages WHERE kind='metadata'").fetchone()[0],'metadata_missing')


if __name__=='__main__':
    unittest.main()
