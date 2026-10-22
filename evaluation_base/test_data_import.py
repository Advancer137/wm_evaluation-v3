import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data_import import prepare_batch, scan_source, verify_batch
from server import connect, init_db, load_manifest, public_task


class DataImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "data_source"
        self.base = self.source / "question_base"
        (self.base / "images").mkdir(parents=True)
        self.videos = self.source / "question_video"
        self.videos.mkdir()
        self.metadata = self.base / "conditions.jsonl"
        self.rows = []
        self.report_patch = patch("data_import.REPORTS", self.root / "reports")
        self.report_patch.start()

    def tearDown(self):
        self.report_patch.stop()
        self.tmp.cleanup()

    def add_case(self, case="WMB_0001", track="risk", frame=True):
        self.rows.append({"source_case_id": case, "track": track, "index": 1,
                          "domain": "driving", "first_frame": f"images/{case}.jpg",
                          "image_path": "/obsolete/server/path.jpg",
                          "base_condition": {"text": "approach intersection"},
                          "preference_condition_A": {"text": "stop"},
                          "preference_condition_B": {"text": "continue"}})
        self.metadata.write_text("".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8-sig")
        if frame:
            (self.base / "images" / f"{case}.jpg").write_bytes(b"test frame")

    def add_video(self, case="WMB_0001", track="risk", side="A", variant="policy_v3", seed=None, model="model-one"):
        folder = self.videos / model / track
        folder.mkdir(parents=True, exist_ok=True)
        suffix = f"__seed_{seed}" if seed is not None else ""
        file = folder / f"{case}__{side}__{variant}{suffix}.mp4"
        file.write_bytes(f"test video {model}/{track}/{side}/{variant}/{seed}".encode())
        return file

    def pair(self, **kwargs):
        return [self.add_video(side=side, **kwargs) for side in ("A", "B")]

    def test_matching_tracks_versions_models_and_seeds(self):
        self.add_case(track="risk")
        self.add_case(track="strategy")
        self.pair(track="risk", variant="raw_prompt")
        self.pair(track="risk", variant="policy_v3", seed=42)
        self.pair(track="strategy", variant="policy_v3", seed=42)
        self.pair(track="risk", variant="policy_v3", seed=42, model="model-two")
        (self.source / "input_config.json").write_text(json.dumps({
            "variant_methods": {"raw_prompt": "raw", "policy_v3": "alignx"}}), encoding="utf-8")
        tasks, report = scan_source(self.source)
        self.assertTrue(report["summary"]["can_prepare"])
        self.assertEqual(len(tasks), 4)
        self.assertEqual(len({t["task_id"] for t in tasks}), 4)
        self.assertEqual({t["generation_seed"] for t in tasks}, {None, 42})
        self.assertEqual({t["method"] for t in tasks}, {"raw", "alignx"})
        self.assertTrue(all(t["initial_frame"] == "question_base/images/WMB_0001.jpg" for t in tasks))

    def test_never_pairs_across_versions_or_seed_or_track(self):
        self.add_case()
        self.add_case(track="strategy")
        self.add_video(side="A", variant="policy_v1")
        self.add_video(side="B", variant="policy_v3")
        self.add_video(side="A", seed=1)
        self.add_video(side="B", seed=2)
        self.add_video(side="B", variant="policy_v1", track="strategy")
        tasks, report = scan_source(self.source)
        self.assertEqual(tasks, [])
        self.assertEqual(report["summary"]["issue_counts"]["incomplete_pair"], 5)

    def test_missing_frames_allowed_but_unmatched_metadata_excluded(self):
        self.add_case(frame=False)
        self.pair()
        self.pair(case="UNKNOWN")
        tasks, report = scan_source(self.source)
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0]["initial_frame"])
        self.assertTrue(report["summary"]["can_prepare"])
        self.assertEqual(report["summary"]["tasks_without_frame"], 1)
        warning = next(i for i in report["issues"] if i["code"] == "missing_frame")
        self.assertEqual(warning["severity"], "warning")
        self.assertFalse(warning["fatal"])
        self.assertEqual(report["summary"]["complete_pairs"], 2)
        self.assertEqual(report["summary"]["missing_frame_rows"], 1)
        self.assertEqual(report["summary"]["issue_counts"]["unmatched_metadata"], 1)
        destination = self.root / "batch"
        prepare_batch(self.source, destination)
        self.assertEqual(len(verify_batch(destination)), 2)
        db = destination / "annotations.sqlite3"
        init_db(db)
        load_manifest(db, destination / "tasks.jsonl")
        with connect(db) as connection:
            task = connection.execute("SELECT * FROM tasks").fetchone()
            self.assertIsNone(public_task(task, True)["initial_frame_url"])

    def test_optional_frame_field_directory_and_empty_file(self):
        self.add_case(frame=False)
        self.pair()
        (self.base / "images").rmdir()
        for value in (None, "", "   ", "images/missing.jpg"):
            with self.subTest(value=value):
                self.rows[0]["first_frame"] = value
                self.metadata.write_text(json.dumps(self.rows[0]), encoding="utf-8")
                tasks, report = scan_source(self.source)
                self.assertTrue(report["summary"]["can_prepare"])
                self.assertIsNone(tasks[0]["initial_frame"])
        del self.rows[0]["first_frame"]
        self.metadata.write_text(json.dumps(self.rows[0]), encoding="utf-8")
        self.assertTrue(scan_source(self.source)[1]["summary"]["can_prepare"])
        (self.base / "images").mkdir()
        (self.base / "images/empty.jpg").touch()
        self.rows[0]["first_frame"] = "images/empty.jpg"
        self.metadata.write_text(json.dumps(self.rows[0]), encoding="utf-8")
        self.assertTrue(scan_source(self.source)[1]["summary"]["can_prepare"])

    def test_duplicate_metadata_and_path_escape_are_fatal(self):
        self.add_case()
        self.add_case()
        self.pair()
        _, report = scan_source(self.source)
        self.assertFalse(report["summary"]["can_prepare"])
        self.assertIn("duplicate_metadata", report["summary"]["issue_counts"])
        self.rows = self.rows[:1]
        self.rows[0]["first_frame"] = "../../outside.jpg"
        self.metadata.write_text(json.dumps(self.rows[0]), encoding="utf-8")
        tasks, report = scan_source(self.source)
        self.assertEqual(tasks, [])
        self.assertGreater(report["summary"]["fatal_errors"], 0)

    def test_json_array_and_ambiguous_metadata_selection(self):
        self.add_case()
        self.pair()
        (self.base / "conditions.json").write_text(json.dumps(self.rows), encoding="utf-8")
        _, report = scan_source(self.source)
        self.assertFalse(report["summary"]["can_prepare"])
        tasks, _ = scan_source(self.source, "conditions.json")
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0]["method"])
        self.assertIsNone(tasks[0]["generation_seed"])

    def test_playback_copy_and_original_are_both_frozen(self):
        self.add_case()
        self.pair()
        def playback_copy(original, cache):
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / (original.stem + ".webm")
            target.write_bytes(b"test browser copy")
            return target
        batch = self.root / "playback-batch"
        with patch("video_playback.browser_asset", side_effect=playback_copy):
            prepare_batch(self.source, batch)
        self.assertEqual(len(verify_batch(batch)), 5)
        task = json.loads((batch / "tasks.jsonl").read_text(encoding="utf-8"))
        self.assertTrue(task["video_a"].endswith(".webm"))
        self.assertTrue(task["original_video_a"].endswith(".mp4"))
        (batch / "media" / task["original_video_a"]).write_bytes(b"changed original")
        with self.assertRaisesRegex(ValueError, "媒体缺失或已修改"):
            verify_batch(batch)

    def test_frozen_batch_independent_from_source_and_detects_tampering(self):
        self.add_case()
        original, _ = self.pair()
        self.add_video(case="WMB_0002")
        batch = self.root / "batch"
        report = prepare_batch(self.source, batch)
        self.assertEqual(report["summary"]["eligible_tasks"], 1)
        self.assertEqual(report["summary"]["excluded_groups"], 1)
        signatures = verify_batch(batch)
        self.assertEqual(len(signatures), 3)
        original.write_bytes(b"source modified after import")
        self.metadata.write_text("not JSON anymore", encoding="utf-8")
        self.assertEqual(signatures, verify_batch(batch))
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            prepare_batch(self.source, batch)
        tasks = [json.loads(line) for line in (batch / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
        db = batch / "annotations.sqlite3"
        init_db(db)
        load_manifest(db, batch / "tasks.jsonl")
        load_manifest(db, batch / "tasks.jsonl")
        with connect(db) as connection:
            task = connection.execute("SELECT * FROM tasks").fetchone()
            public = json.dumps(public_task(task, True))
            for secret in ("model-one", "policy_v3", "WMB_0001", "question_video"):
                self.assertNotIn(secret, public)
        (batch / "media" / tasks[0]["video_a"]).write_bytes(b"tampered media")
        with self.assertRaisesRegex(ValueError, "媒体缺失或已修改"):
            verify_batch(batch)
        tasks[0]["preference_a"] = "changed question"
        (batch / "tasks.jsonl").write_text(json.dumps(tasks[0]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "题单已被修改"):
            verify_batch(batch)
        with self.assertRaisesRegex(ValueError, "content differs"):
            load_manifest(db, batch / "tasks.jsonl")


class TranslationTest(DataImportTest):
    def write_translations(self, text):
        (self.base / "translations_zh.tsv").write_text(text, encoding="utf-8-sig")

    def test_without_file_feature_is_off(self):
        self.add_case()
        self.pair()
        tasks, report = scan_source(self.source)
        self.assertEqual(report["summary"]["translation"]["status"], "missing")
        self.assertFalse(any(k.endswith("_zh") for k in tasks[0]))
        self.assertFalse(any(i["code"].startswith("translation") for i in report["issues"]))
        self.assertIsNone(public_task_for(self, tasks)["translation_zh"])

    def test_good_file_is_checked_and_frozen_into_batch(self):
        self.add_case()
        self.add_case(case="WMB_0002")
        self.pair()
        self.pair(case="WMB_0002")
        self.write_translations("source\tzh_reference\n"
                                "approach  intersection\t驶近路口\n"     # whitespace differences are tolerated
                                "stop\t停下\n"
                                "continue\t\n"                          # empty -> dropped
                                "Lane 2\t第二车道\n"                     # number mismatch -> warning only
                                "broken line without tab\n"
                                "stop\t停止\n")                          # conflict -> first wins
        tasks, report = scan_source(self.source)
        info = report["summary"]["translation"]
        self.assertEqual(info["status"], "enabled")
        self.assertEqual(info["covered_texts"], 2)
        codes = {i["code"] for i in report["issues"]}
        self.assertTrue({"translation_empty", "translation_malformed", "translation_conflict",
                         "translation_number_mismatch", "translation_missing"} <= codes)
        self.assertTrue(all(i["severity"] == "warning" and not i["fatal"] for i in report["issues"]
                            if i["code"].startswith("translation")))
        self.assertTrue(report["summary"]["can_prepare"])
        task = tasks[0]
        self.assertEqual((task["base_condition_zh"], task["preference_a_zh"]), ("驶近路口", "停下"))
        self.assertNotIn("preference_b_zh", task)
        self.assertEqual(public_task_for(self, tasks)["translation_zh"],
                         {"base_condition": "驶近路口", "preference_a": "停下"})
        batch = self.root / "batches/zh"
        prepare_batch(self.source, batch)
        frozen = [json.loads(l) for l in (batch / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(t["base_condition_zh"] == "驶近路口" for t in frozen))
        self.assertEqual(json.loads((batch / "snapshot.json").read_text(encoding="utf-8"))["translation"]["status"], "enabled")
        self.assertIn("参考译文：已启用", (batch / "input_check.md").read_text(encoding="utf-8"))
        verify_batch(batch)

    def test_unusable_files_switch_the_feature_off(self):
        self.add_case()
        self.pair()
        for content in ("no tabs here\nnor here\nstop\t停下\n",         # mostly malformed
                        "something else\t别的\n",                          # matches nothing
                        "stop\tstop\napproach intersection\t\n"):        # nothing usable
            self.write_translations(content)
            tasks, report = scan_source(self.source)
            self.assertEqual(report["summary"]["translation"]["status"], "disabled", content)
            self.assertFalse(any(k.endswith("_zh") for k in tasks[0]))
            self.assertTrue(report["summary"]["can_prepare"])
        (self.base / "translations_zh.tsv").write_bytes(b"\xff\xfe\x00bad")
        _, report = scan_source(self.source)
        self.assertEqual(report["summary"]["translation"]["status"], "disabled")


def public_task_for(test, tasks):
    db = test.root / f"t{len(list(test.root.glob('t*.sqlite3')))}.sqlite3"
    manifest = test.root / "m.jsonl"
    manifest.write_text("".join(json.dumps(t) + "\n" for t in tasks), encoding="utf-8")
    init_db(db)
    load_manifest(db, manifest)
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM tasks").fetchone()
        return public_task(row, 1)


if __name__ == "__main__":
    unittest.main()
