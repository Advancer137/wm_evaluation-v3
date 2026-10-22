"""Results of an older batch can be merged into a newer one, task by task."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assignment import create_plan
from data_import import prepare_batch
from answer_sheet import sheet
from report import build_report
from result_io import export_results, import_results, seed_local
from server import connect, init_db, load_manifest, now

ANSWERS = sheet()


class CrossBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "data_source"
        (self.source / "question_base/images").mkdir(parents=True)
        self.rows = {}
        for case in ("C0", "C1", "C2", "C3", "C5"):
            self.add_case(case, frame=case != "C3")
        self.patches = [patch("data_import.REPORTS", self.root / "reports"),
                        patch("result_io.RESULTS", self.root / "evaluation_results")]
        for p in self.patches:
            p.start()
        self.v1 = self.root / "batches/v1"
        prepare_batch(self.source, self.v1)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def add_case(self, case, frame=True, text=None):
        self.rows[case] = {"source_case_id": case, "track": "risk", "first_frame": f"images/{case}.jpg",
                           "base_condition": {"text": text or f"base {case}"},
                           "preference_condition_A": {"text": "stop"}, "preference_condition_B": {"text": "go"}}
        (self.source / "question_base/conditions.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in self.rows.values()), encoding="utf-8")
        if frame:
            (self.source / "question_base/images" / f"{case}.jpg").write_bytes(f"frame {case}".encode())
        folder = self.source / "question_video/model/risk"
        folder.mkdir(parents=True, exist_ok=True)
        for side in "AB":
            (folder / f"{case}__{side}__v3.mp4").write_bytes(f"{case}-{side}".encode() * 20)

    def answer_all(self, batch, person="p1", name="Alice"):
        db_path = batch / "annotations.sqlite3"
        init_db(db_path)
        load_manifest(db_path, batch / "tasks.jsonl")
        with connect(db_path) as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks")]
            db.execute("INSERT INTO annotators VALUES (?,?)", (person, now()))
            db.execute("INSERT INTO annotator_profiles VALUES (?,?)", (person, name))
            for i, task in enumerate(ids):
                db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, task, i % 2, now()))
                db.execute("INSERT INTO annotations VALUES (?,?,?,?)", (person, task, json.dumps(ANSWERS), now()))
                db.execute("INSERT INTO annotation_revisions VALUES (?,?,2)", (person, task))
        return json.loads(export_results(batch)[0].read_text(encoding="utf-8"))

    def make_v2(self):
        """C0 removed, C1 text reworded, C5 video re-generated, C3 frame added, C4 new, C2 unchanged."""
        for side in "AB":
            (self.source / f"question_video/model/risk/C0__{side}__v3.mp4").unlink()
        del self.rows["C0"]
        self.add_case("C1", text="base C1, reworded")
        (self.source / "question_video/model/risk/C5__B__v3.mp4").write_bytes(b"regenerated" * 20)
        (self.source / "question_base/images/C3.jpg").write_bytes(b"frame C3 added later")
        self.add_case("C4")
        v2 = self.root / "batches/v2"
        prepare_batch(self.source, v2)
        return v2

    def test_shared_tasks_are_imported_and_changes_are_skipped(self):
        package = self.answer_all(self.v1)
        self.assertTrue(all("task_fingerprint" in r for r in package["records"]))
        v2 = self.make_v2()
        result = import_results(v2, package, "old.json", log=True)
        self.assertTrue(result["cross_batch"])
        self.assertEqual(result["source_batch"], "v1")
        self.assertEqual(result["added"], 2)                 # C2 and C3
        self.assertEqual(result["skipped_removed"], 1)       # C0
        self.assertEqual(result["skipped_changed"], 2)       # C1 (text) and C5 (video)
        self.assertEqual(result["frame_changed"], 1)         # C3 got its frame later, still counted
        self.assertTrue(any("v1" in w for w in result["warnings"]))
        with connect(v2 / "merged.sqlite3") as db:
            rows = db.execute("""SELECT t.source_case_id, s.x_is_a FROM annotations a
                JOIN tasks t ON t.id=a.task_id JOIN assignments s USING(annotator_id,task_id)""").fetchall()
        self.assertEqual({r[0] for r in rows}, {"C2", "C3"})
        old_mapping = {r["task_id"]: r["x_is_a"] for r in package["records"]}
        with connect(v2 / "merged.sqlite3") as db:
            for task, x in db.execute("SELECT task_id, x_is_a FROM assignments"):
                self.assertEqual(bool(x), old_mapping[task])  # same task id, same hidden X/Y order
        # importing twice does not double count
        again = import_results(v2, package)
        self.assertEqual((again["added"], again["unchanged"]), (0, 2))

    def test_old_format_files_use_the_old_batch_folder(self):
        package = self.answer_all(self.v1)
        for record in package["records"]:
            record.pop("task_fingerprint")
            record.pop("frame_sha256")
        v2 = self.make_v2()
        result = import_results(v2, package)
        self.assertEqual((result["added"], result["skipped_removed"], result["skipped_changed"]), (2, 1, 2))
        # without the old batch folder an old-format file cannot be checked task by task
        shutil.rmtree(self.v1)
        with self.assertRaisesRegex(ValueError, "旧题库"):
            import_results(self.root / "batches/v2", package)

    def test_new_format_files_work_without_the_old_batch(self):
        package = self.answer_all(self.v1)
        v2 = self.make_v2()
        shutil.rmtree(self.v1)
        result = import_results(v2, package)
        self.assertEqual((result["added"], result["skipped_changed"]), (2, 2))
        self.assertEqual(result["source_batch"], "v1")

    def test_slot_results_from_an_old_plan_count_in_the_new_report(self):
        plan = create_plan(self.v1, 2, 1, seed=3)
        slot = next(iter(plan["slots"]))
        package = self.answer_all(self.v1)
        mine = {e["task_id"]: e["x_is_a"] for e in plan["slots"][slot]}
        package["records"] = [dict(r, x_is_a=mine[r["task_id"]]) for r in package["records"] if r["task_id"] in mine]
        package.update(slot=slot, plan_id=plan["plan_id"])
        v2 = self.make_v2()
        result = import_results(v2, package)
        self.assertFalse(any("无法核对" in w for w in result["warnings"]))   # old plan found in v1
        create_plan(v2, 2, 1, seed=4)
        report = build_report(v2)
        self.assertGreater(result["added"] + result["skipped_changed"] + result["skipped_removed"], 0)
        if result["added"]:
            self.assertTrue(any(o["plan_id"] == plan["plan_id"] for o in report["others"]))
            self.assertTrue(any(plan["plan_id"] in w["text"] for w in report["warnings"]))


    def test_results_of_the_old_question_set_are_still_accepted(self):
        package = self.answer_all(self.v1)
        self.assertEqual(package["format"], "pwb-results-v2")
        old = {f"{side}_{k}": v for side in ("x", "y") for k, v in
               (("validity", "valid"), ("retention", "pass"), ("preference_a", "yes"), ("preference_b", "no"))}
        package["format"] = "pwb-results-v1"
        for record in package["records"]:
            record["answers"] = dict(old)
        result = import_results(self.v1, package)
        self.assertEqual(result["added"], 5)
        with connect(self.v1 / "merged.sqlite3") as db:
            stored = [json.loads(r[0]) for r in db.execute("SELECT answers_json FROM annotations")]
        self.assertTrue(all(a == old for a in stored))        # kept as answered, not converted
        # the old file also seeds a reviewing annotator, who then re-answers the new questions
        seeded = seed_local(self.v1, package)
        self.assertEqual(seeded["seeded"], 5)

    def test_seed_local_review_starts_from_existing_answers(self):
        package = self.answer_all(self.v1, name="Claude")
        # same batch: every answer becomes a saved answer of a new local annotator
        result = seed_local(self.v1, package)
        self.assertEqual((result["seeded"], result["total_tasks"], result["name"]), (5, 5, "Claude"))
        with connect(self.v1 / "annotations.sqlite3") as db:
            rows = db.execute("SELECT task_id, x_is_a FROM assignments WHERE annotator_id=?", (result["token"],)).fetchall()
            revs = {r[0] for r in db.execute("SELECT revision FROM annotation_revisions WHERE annotator_id=?", (result["token"],))}
        self.assertEqual({r[0]: bool(r[1]) for r in rows}, {r["task_id"]: r["x_is_a"] for r in package["records"]})
        self.assertEqual(revs, {2})
        # new batch: only identical tasks are carried over, new ones remain to be answered
        v2 = self.make_v2()
        result = seed_local(v2, package)
        self.assertEqual((result["seeded"], result["skipped_removed"], result["skipped_changed"], result["total_tasks"]),
                         (2, 1, 2, 5))
        # its export is an ordinary result file of the reviewing person
        files = [json.loads(p.read_text(encoding="utf-8")) for p in export_results(v2)]
        self.assertEqual(sorted(len(f["records"]) for f in files), [2])
        with self.assertRaises(ValueError):
            seed_local(v2, {"format": "pwb-results-v1", "records": []})


if __name__ == "__main__":
    unittest.main()
