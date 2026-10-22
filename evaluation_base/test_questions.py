"""Question set: validity -> base task -> consistency (with reasons) -> preferences, with skipping."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from answer_sheet import sheet, side_answers
from report import build_report
from server import aggregate, connect, init_db, is_legacy_answers, load_manifest, scored_side, validate_answers
from test_server import ServerFixture


def legacy_sheet(**changes):
    values = {"validity": "valid", "retention": "pass", "preference_a": "yes", "preference_b": "no"}
    values.update(changes)
    return {f"{side}_{key}": value for side in ("x", "y") for key, value in values.items()}


class ValidationTest(unittest.TestCase):
    def rejects(self, answers, text=None):
        with self.assertRaises(ValueError) as caught:
            validate_answers(answers)
        if text:
            self.assertIn(text, str(caught.exception))

    def test_complete_sheet_round_trips(self):
        answers = sheet()
        self.assertEqual(validate_answers(answers), answers)

    def test_unusable_video_skips_every_later_question(self):
        for validity in ("invalid", "insufficient"):
            answers = sheet({"validity": validity, "base_task": None, "retention": None,
                             "preference_a": None, "preference_b": None})
            self.assertEqual(validate_answers(answers)["x_preference_a"], None)
            self.rejects(dict(answers, x_preference_a="yes"), "valid")
            self.rejects(dict(answers, x_base_task="yes"), "valid")

    def test_preferences_are_not_asked_when_the_base_task_was_not_done(self):
        answers = sheet({"base_task": "no", "preference_a": None, "preference_b": None})
        self.assertIsNone(validate_answers(answers)["x_preference_b"])
        self.rejects(dict(answers, x_preference_a="yes"), "base task")
        # "uncertain" and "partial" still ask the preferences
        self.rejects(sheet({"base_task": "uncertain", "preference_a": None}), "x_preference_a")
        self.rejects(sheet({"base_task": "partial", "preference_a": None}), "x_preference_a")
        self.assertEqual(validate_answers(sheet({"base_task": "partial"}))["x_base_task"], "partial")
        self.rejects(sheet({"base_task": "mostly"}), "partial")

    def test_failed_consistency_needs_reasons_and_only_then(self):
        failed = {"retention": "fail", "retention_reasons": ["scene", "subject"], "retention_note": "  换了房间 "}
        out = validate_answers(sheet(failed))
        self.assertEqual(out["x_retention_reasons"], ["subject", "scene"])       # canonical order
        self.assertEqual(out["x_retention_note"], "换了房间")
        self.rejects(sheet({"retention": "fail"}), "reason")
        self.rejects(sheet({"retention": "fail", "retention_reasons": []}), "reason")
        self.rejects(sheet({"retention": "fail", "retention_reasons": ["colour"]}), "reason")
        self.rejects(sheet({"retention": "fail", "retention_reasons": ["scene", "scene"]}), "reason")
        self.rejects(sheet({"retention": "pass", "retention_reasons": ["scene"]}), "only allowed")
        self.rejects(sheet({"retention": "fail", "retention_reasons": ["other"], "retention_note": "x" * 201}), "200")

    def test_required_questions_and_legacy_sheets(self):
        self.rejects(sheet({"base_task": None}), "base_task")
        self.rejects(sheet({"retention": None}), "retention")
        old = legacy_sheet()
        self.assertTrue(is_legacy_answers(old))
        self.rejects(old, "base task")                                             # not accepted as a new answer
        self.assertEqual(validate_answers(old, allow_legacy=True), old)            # but still readable
        self.assertFalse(is_legacy_answers(sheet()))


class ScoringTest(unittest.TestCase):
    def test_base_task_failure_counts_preferences_as_no(self):
        answers = sheet({"base_task": "no", "preference_a": None, "preference_b": None})
        scored = scored_side(validate_answers(answers), "x")
        self.assertEqual((scored["preference_a"], scored["preference_b"]), ("no", "no"))
        self.assertEqual(scored_side(answers, "y")["preference_a"], "yes")

    def test_old_sheets_keep_preferences_but_not_the_old_retention(self):
        scored = scored_side(legacy_sheet(), "x")
        self.assertIsNone(scored["retention"])
        self.assertIsNone(scored["base_task"])
        self.assertEqual((scored["preference_a"], scored["preference_b"]), ("yes", "no"))


class ConsensusTest(ServerFixture):
    """Reuses the single-task database and local server of the flow tests."""

    def put(self, sheets):
        with connect(self.db) as db:
            task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
            for i, answers in enumerate(sheets):
                person = f"person-{i}"
                db.execute("INSERT INTO annotators VALUES (?,?)", (person, "time"))
                db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, task_id, 1, "time"))   # X = A
                db.execute("INSERT INTO annotations VALUES (?,?,?,?)", (person, task_id, json.dumps(answers), "time"))
        out = Path(self.tmp.name) / "consensus"
        aggregate(self.db, out)
        return json.loads((out / "consensus.jsonl").read_text(encoding="utf-8")), \
            [json.loads(l) for l in (out / "raw_annotations.jsonl").read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def good(**x):
        return sheet(x, {"preference_a": "no", "preference_b": "yes"})

    def test_task_not_done_cannot_pass_strict_sbps_and_reasons_are_counted(self):
        failed = self.good(base_task="no", preference_a=None, preference_b=None)
        consensus, raw = self.put([failed, failed, self.good()])
        result = consensus["consensus"]
        self.assertEqual(result["video_a_base_task"], "no")
        self.assertEqual((result["video_a_preference_a"], result["video_a_preference_b"]), ("no", "no"))
        self.assertFalse(consensus["strict_sbps"])
        # raw rows keep what was really answered (null), the normalized row the value used for scoring
        row = next(r for r in raw if r["answers"]["x_base_task"] == "no")
        self.assertIsNone(row["answers"]["x_preference_a"])
        self.assertEqual(row["normalized"]["video_a_preference_a"], "no")

    def test_gated_variant_also_needs_task_done_and_consistent_conditions(self):
        consensus, _ = self.put([self.good(), self.good(), self.good()])
        self.assertTrue(consensus["strict_sbps"])
        self.assertTrue(consensus["strict_sbps_with_gates"])
        self.assertTrue(consensus["strict_sbps_task_done"])

    def test_inconsistent_conditions_keep_the_pattern_but_fail_the_gated_variant(self):
        shaky = self.good(retention="fail", retention_reasons=["subject", "scene"])
        consensus, _ = self.put([shaky, shaky, self.good()])
        self.assertTrue(consensus["strict_sbps"])                      # the pattern itself is unchanged ...
        self.assertFalse(consensus["strict_sbps_with_gates"])          # ... but the conditions were not kept
        self.assertEqual(consensus["retention_reasons"]["a"], {"subject": 2, "scene": 2})
        self.assertEqual(consensus["consensus"]["video_a_retention"], "fail")

    def test_partly_done_videos_count_in_the_main_metric_but_not_in_the_stricter_views(self):
        partial = self.good(base_task="partial")
        consensus, _ = self.put([partial, partial, self.good()])
        self.assertTrue(consensus["strict_sbps"])                      # preferences were answered from what is visible
        self.assertFalse(consensus["strict_sbps_task_done"])           # but the task was only partly done
        self.assertFalse(consensus["strict_sbps_with_gates"])
        self.assertEqual(consensus["consensus"]["video_a_base_task"], "partial")
        self.assertEqual(consensus["base_task_counts"]["a"], {"partial": 2, "yes": 1})

    def test_old_sheets_still_aggregate(self):
        consensus, _ = self.put([legacy_sheet(preference_a="yes"), self.good(), self.good()])
        self.assertEqual(consensus["annotation_count"], 3)
        self.assertEqual(consensus["consensus"]["video_a_preference_a"], "yes")

    def test_submit_requires_the_new_questions_and_old_answers_stay_readable(self):
        session, _ = self.request("/api/session", payload={"name": "测试志愿者"})
        token = json.loads(session)["token"]
        task = json.loads(self.request("/api/next", token)[0])["task"]
        with self.assertRaises(HTTPError) as old:
            self.request("/api/submit", token, {"task_id": task["id"], "answers": legacy_sheet()})
        self.assertEqual(old.exception.code, 400)
        with self.assertRaises(HTTPError) as skipped:
            self.request("/api/submit", token, {"task_id": task["id"],
                                                "answers": sheet({"base_task": "no"})})   # preferences still filled
        self.assertEqual(skipped.exception.code, 400)
        answers = sheet({"base_task": "no", "preference_a": None, "preference_b": None})
        self.request("/api/submit", token, {"task_id": task["id"], "answers": answers})
        self.assertEqual(json.loads(self.request(f"/api/tasks/{task['id']}", token)[0])["answers"], answers)
        # an answer stored by an older version (written straight into the database) is returned as it was
        with connect(self.db) as db:
            db.execute("UPDATE annotations SET answers_json=?", (json.dumps(legacy_sheet()),))
        self.assertEqual(json.loads(self.request(f"/api/tasks/{task['id']}", token)[0])["answers"], legacy_sheet())


class ReportTest(unittest.TestCase):
    def test_annotator_who_skips_far_more_often_than_the_others_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = folder / "tasks.jsonl"
            manifest.write_text("".join(json.dumps({
                "source_case_id": f"C{i}", "track": "risk", "base_condition": "b", "preference_a": "a",
                "preference_b": "b", "video_a": "a.mp4", "video_b": "b.mp4"}) + "\n" for i in range(12)), encoding="utf-8")
            db_path = folder / "merged.sqlite3"
            init_db(db_path)
            load_manifest(db_path, manifest)
            skipping = sheet({"base_task": "no", "preference_a": None, "preference_b": None},
                             {"base_task": "no", "preference_a": None, "preference_b": None})
            with connect(db_path) as db:
                tasks = [r[0] for r in db.execute("SELECT id FROM tasks")]
                for person, answers in (("p1", sheet()), ("p2", sheet()), ("p3", sheet()), ("p4", skipping)):
                    db.execute("INSERT INTO annotators VALUES (?,?)", (person, "t"))
                    db.execute("INSERT INTO annotator_profiles VALUES (?,?)", (person, f"name-{person}"))
                    for task in tasks:
                        db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, task, 1, "t"))
                        db.execute("INSERT INTO annotations VALUES (?,?,?,?)", (person, task, json.dumps(answers), "t"))
                        db.execute("INSERT INTO annotation_revisions VALUES (?,?,2)", (person, task))
            warnings = [w["text"] for w in build_report(folder)["warnings"]]
            self.assertEqual(sum("抽查" in w for w in warnings), 1)
            self.assertTrue(any("name-p4" in w and "100%" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
