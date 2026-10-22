import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import admin
import assignment
import distribution
import result_io
import server
import video_playback
from assignment import create_plan, load_plan, make_assignments
from data_import import prepare_batch, verify_batch
from answer_sheet import sheet
from report import build_report
from result_io import export_results, import_results, log_rejection

ANSWERS = sheet()


def rows(n_cases, tracks=("risk", "strategy")):
    return [{"task_id": f"{case}-{track}", "source_case_id": case}
            for case in (f"C{i:02d}" for i in range(n_cases)) for track in tracks]


class MakeAssignmentsTest(unittest.TestCase):
    def test_balanced_distinct_and_deterministic(self):
        tasks = rows(26)                                   # 52 tasks
        plan = make_assignments(tasks, 7, 3, seed=5)
        self.assertEqual(plan, make_assignments(tasks, 7, 3, seed=5))
        self.assertNotEqual(plan, make_assignments(tasks, 7, 3, seed=6))
        loads = [len(v) for v in plan.values()]
        self.assertEqual(sum(loads), 52 * 3)
        self.assertLessEqual(max(loads) - min(loads), 1)
        owners = {}
        for slot, entries in plan.items():
            self.assertEqual(len({e["task_id"] for e in entries}), len(entries))   # no task twice for one person
            for e in entries:
                owners.setdefault(e["task_id"], []).append((slot, e["x_is_a"]))
        self.assertEqual(set(owners), {t["task_id"] for t in tasks})
        for task, items in owners.items():
            self.assertEqual(len(items), 3)
            self.assertEqual(len({s for s, _ in items}), 3)            # three different people
            self.assertEqual(len({x for _, x in items}), 2)            # both X/Y orders are used
        flat = Counter(e["x_is_a"] for v in plan.values() for e in v)
        self.assertLess(abs(flat[True] - flat[False]), 52 * 3 * 0.25)

    def test_same_case_not_consecutive_and_input_errors(self):
        plan = make_assignments(rows(12), 4, 2, seed=1)
        case = {t["task_id"]: t["source_case_id"] for t in rows(12)}
        for entries in plan.values():
            ids = [case[e["task_id"]] for e in entries]
            self.assertTrue(all(a != b for a, b in zip(ids, ids[1:])), ids)
        for slots, redundancy in ((3, 4), (0, 1), (201, 1), (3, 0)):
            with self.assertRaises(ValueError):
                make_assignments(rows(3), slots, redundancy, 1)
        with self.assertRaises(ValueError):
            make_assignments([], 3, 3, 1)


class PlannedStudyTest(unittest.TestCase):
    """organizer batch -> plan -> small per-computer packages -> student runs -> merge -> report"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = self.root / "source"
        (source / "question_base/images").mkdir(parents=True)
        cases = []
        for i in range(6):
            case = f"WMB_{i:04d}"
            (source / "question_base/images" / f"{case}.jpg").write_bytes(f"frame-{case}".encode())
            cases.append({"source_case_id": case, "track": "risk", "first_frame": f"images/{case}.jpg",
                          "base_condition": {"text": f"base {case}"},
                          "preference_condition_A": {"text": "stop"}, "preference_condition_B": {"text": "go"}})
        (source / "question_base/conditions.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
        video = source / "question_video/model-x/risk"
        video.mkdir(parents=True)
        for c in cases:
            for side in "AB":
                (video / f"{c['source_case_id']}__{side}__policy_v3.mp4").write_bytes(
                    f"{c['source_case_id']}-{side}".encode() * 50)
        self.batch = self.root / "batches/study-t"
        with patch("data_import.REPORTS", self.root / "reports"):
            prepare_batch(source, self.batch)
        self.patches = [patch("result_io.RESULTS", self.root / "evaluation_results"),
                        patch("distribution.PACKAGES", self.root / "evaluation_packages")]
        for item in self.patches:
            item.start()
        self.plan = create_plan(self.batch, 4, 2, seed=11)
        self.results = distribution.build_slot_packages(self.batch, self.plan["plan_id"])

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------------
    def unpack(self, slot, name):
        target = self.root / name
        with zipfile.ZipFile(distribution.slot_package_path(self.batch, self.plan["plan_id"], slot)) as z:
            z.extractall(target)
        return target / f"评测-{slot}"

    def start_student(self, package, results_dir):
        """Start the student server of an unpacked package the way `server.py student` does."""
        base = package / "evaluation_base"
        stack = [patch("server.HERE", base), patch("result_io.RESULTS", results_dir)]
        for p in stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        srv = server.build_student_server(server.find_slot_batch(), 0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        def stop():
            srv.shutdown(); srv.server_close(); thread.join(timeout=3)
        return srv, f"http://127.0.0.1:{srv.server_port}", stop

    @staticmethod
    def call(url, path, token=None, payload=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(url + path, data=data, headers=headers)) as response:
            return json.loads(response.read()) if response.headers["Content-Type"].startswith("application/json") else response.read()

    def run_student(self, slot, name="Alice", results_dir=None, limit=None):
        package = self.unpack(slot, f"pc-{slot}")
        results_dir = results_dir or self.root / f"results-{slot}"
        srv, url, stop = self.start_student(package, results_dir)
        try:
            token = self.call(url, "/api/session", payload={"name": name})["token"]
            seen = []
            while limit is None or len(seen) < limit:
                data = self.call(url, "/api/next", token)
                if data["task"] is None:
                    break
                task = data["task"]
                seen.append(task["id"])
                self.call(url, "/api/submit", token, {"task_id": task["id"], "answers": ANSWERS})
        finally:
            stop()
        return package, results_dir, seen

    # -- tests -----------------------------------------------------------------------
    def test_plan_is_complete_and_packages_are_small_and_self_contained(self):
        plan = load_plan(self.batch, self.plan["plan_id"])
        self.assertEqual(len(self.results), 4)
        total_per_task = Counter(e["task_id"] for v in plan["slots"].values() for e in v)
        self.assertEqual(set(total_per_task.values()), {2})
        self.assertEqual(create_plan(self.batch, 4, 2, seed=11)["plan_id"], plan["plan_id"])   # idempotent
        full = {p.relative_to(self.batch / "media").as_posix() for p in (self.batch / "media").rglob("*") if p.is_file()}
        for item in self.results:
            with zipfile.ZipFile(item["path"]) as z:
                names = z.namelist()
                top = f"评测-{item['slot']}/"
                self.assertIn(top + "启动评测.cmd", names)
                self.assertIn(top + "学生说明.txt", names)
                self.assertIn(top + "evaluation_base/server.py", names)
                self.assertFalse(any(n.endswith(("admin.py", "admin.js", "report.py", "distribution.py")) for n in names))
                self.assertFalse(any("input_check" in n or "/plans/" in n or "sqlite" in n for n in names))
                media = [n for n in names if "/media/" in n]
                self.assertLess(len(media), len(full))           # only this slot's own videos
                launcher = z.read(top + "启动评测.cmd")
                self.assertIn(b"server.py student", launcher)
                self.assertIn(b"\r\n", launcher)
                self.assertNotIn(b"\n", launcher.replace(b"\r\n", b""))
                doc = json.loads(z.read(top + "evaluation_base/batches/study-t/slot.json"))
                self.assertEqual(doc["slot"], item["slot"])
                self.assertEqual(len(doc["tasks"]), item["tasks"])

    def test_student_flow_uses_plan_order_resumes_and_locks_identity(self):
        slot = "A02"
        planned = self.plan["slots"][slot]
        package = self.unpack(slot, "pc")
        results_dir = self.root / "r"
        srv, url, stop = self.start_student(package, results_dir)
        try:
            self.assertEqual(self.call(url, "/api/profile"), {"name": "", "slot": slot})
            token = self.call(url, "/api/session", payload={"name": "  Alice "})["token"]
            first = self.call(url, "/api/next", token)
            self.assertEqual(first["total"], len(planned))
            self.assertEqual(first["done"], 0)
            self.call(url, "/api/submit", token, {"task_id": first["task"]["id"], "answers": ANSWERS})
            # the half-way student leaves; the media of tasks that are not theirs do not exist on disk
            second = self.call(url, "/api/next", token)
            self.assertEqual([first["task"]["id"], second["task"]["id"]], [e["task_id"] for e in planned[:2]])
            self.assertTrue(self.call(url, second["task"]["video_x_url"], token))
            mine = {e["task_id"] for e in planned}
            theirs = next(e["task_id"] for v in self.plan["slots"].values() for e in v if e["task_id"] not in mine)
            with self.assertRaises(HTTPError) as other:
                self.call(url, f"/media/{theirs}/x", token)
            self.assertEqual(other.exception.code, 404)
        finally:
            stop()
        # new process, wiped browser (no token): same name -> same record, continues at task 2
        srv, url, stop = self.start_student(package, results_dir)
        try:
            self.assertEqual(self.call(url, "/api/profile")["name"], "Alice")
            with self.assertRaises(HTTPError) as wrong:
                self.call(url, "/api/session", payload={"name": "Mallory"})
            self.assertEqual(wrong.exception.code, 400)
            body = json.loads(wrong.exception.read())
            self.assertIn("Alice", body["error"])
            again = self.call(url, "/api/session", payload={"name": "alice"})
            self.assertEqual(again["slot"], slot)
            nxt = self.call(url, "/api/next", again["token"])
            self.assertEqual(nxt["task"]["id"], planned[1]["task_id"])
            self.assertEqual(nxt["done"], 1)
            # fixed X/Y order: X is video A exactly when the plan says so
            task = next(t for t in planned if t["task_id"] == nxt["task"]["id"])
            with connect_db(package) as db:
                self.assertEqual(bool(db.execute("SELECT x_is_a FROM assignments WHERE task_id=?",
                                                 (task["task_id"],)).fetchone()[0]), task["x_is_a"])
        finally:
            stop()

    def test_finish_page_names_result_file_and_import_report(self):
        results, packages = {}, {}
        for slot, name in (("A01", "Alice"), ("A03", "Carol")):
            package, results_dir, seen = self.run_student(slot, name)
            packages[slot] = package
            self.assertEqual(seen, [e["task_id"] for e in self.plan["slots"][slot]])
            files = list(results_dir.rglob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertIn(slot, files[0].name)
            results[slot] = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual((results[slot]["slot"], results[slot]["plan_id"]), (slot, self.plan["plan_id"]))
        # finish page tells the student which file to send
        srv, url, stop = self.start_student(packages["A01"], self.root / "results-A01")
        try:
            token = self.call(url, "/api/session", payload={"name": "Alice"})["token"]
            done = self.call(url, "/api/next", token)
            self.assertIsNone(done["task"])
            self.assertIn("results-A01-Alice", done["result_file"])
        finally:
            stop()
        # organizer side: import + report
        a = import_results(self.batch, results["A01"], "a.json", log=True)
        self.assertEqual((a["added"], a["slot"], a["planned"], a["warnings"]), (len(self.plan["slots"]["A01"]), "A01", len(self.plan["slots"]["A01"]), []))
        import_results(self.batch, results["A03"], "c.json", log=True)
        self.assertEqual(import_results(self.batch, results["A01"], "a-again.json", log=True)["unchanged"], a["added"])
        report = build_report(self.batch)
        by_slot = {s["slot"]: s for s in report["slots"]}
        self.assertEqual(by_slot["A01"]["status"], "已完成")
        self.assertEqual(by_slot["A01"]["name"], "Alice")
        self.assertEqual(by_slot["A02"]["status"], "未收到")
        self.assertEqual(report["summary"]["slots_received"], 2)
        self.assertTrue(any("未收到" in w["text"] or "尚未收到" in w["text"] for w in report["warnings"]))
        under = {u["task_id"]: u for u in report["coverage"]["under"]}
        self.assertTrue(under, "tasks missing a second answer must be listed")
        for u in under.values():
            self.assertTrue(set(u["missing_slots"]) <= {"A02", "A04"} | {"A01", "A03"})
        self.assertEqual(len(report["imports"]), 3)
        # a rejected (tampered) file shows up too
        bad = copy.deepcopy(results["A01"])
        bad["records"][0]["x_is_a"] = not bad["records"][0]["x_is_a"]
        with self.assertRaisesRegex(ValueError, "映射"):
            import_results(self.batch, bad, "bad.json", log=True)
        log_rejection(self.batch, bad, "bad.json", "X/Y 映射与分配方案不一致")
        report = build_report(self.batch)
        self.assertEqual(report["imports"][0]["status"], "rejected")
        self.assertTrue(any(w["level"] == "error" for w in report["warnings"]))

    def test_import_rejects_tasks_outside_the_slot_and_merges_same_slot_from_two_pcs(self):
        package, _, _ = self.run_student("A04", "Dan", limit=3)
        _, _, _ = self.run_student("A04", "Dan", limit=0)
        files = list((self.root / "results-A04").rglob("*.json"))
        pkg = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(len(pkg["records"]), 3)
        foreign = copy.deepcopy(pkg)
        outside = next(e["task_id"] for e in self.plan["slots"]["A01"]
                       if e["task_id"] not in {x["task_id"] for x in self.plan["slots"]["A04"]})
        foreign["records"][0]["task_id"] = outside
        with self.assertRaisesRegex(ValueError, "分配清单"):
            import_results(self.batch, foreign)
        self.assertEqual(import_results(self.batch, pkg)["added"], 3)
        # same package copied to a second computer (other device id) and continued there
        second = copy.deepcopy(pkg)
        second["device_id"] = "b" * 32
        second["records"][0]["revision"] += 1
        second["records"][0]["answers"]["x_preference_a"] = "uncertain"
        result = import_results(self.batch, second)
        self.assertEqual((result["updated"], result["unchanged"]), (1, 2))   # merged into one slot record
        report = build_report(self.batch)
        row = next(s for s in report["slots"] if s["slot"] == "A04")
        self.assertEqual(row["done"], 3)
        self.assertEqual(len(row["devices"]), 0)        # devices come from the (un-logged) imports
        import_results(self.batch, second, "dev2.json", log=True)
        self.assertEqual(len(next(s for s in build_report(self.batch)["slots"] if s["slot"] == "A04")["devices"]), 1)
        # a missing plan file does not block the import but is flagged
        orphan = copy.deepcopy(pkg)
        orphan["plan_id"] = "0" * 10
        res = import_results(self.batch, orphan)
        self.assertTrue(res["warnings"])

    def test_same_name_in_two_slots_is_flagged(self):
        for slot in ("A01", "A02"):
            self.run_student(slot, "Same Person", limit=1)
        for slot in ("A01", "A02"):
            for f in (self.root / f"results-{slot}").rglob("*.json"):
                import_results(self.batch, json.loads(f.read_text(encoding="utf-8")), f.name, log=True)
        report = build_report(self.batch)
        self.assertTrue(any("A01、A02" in w["text"] for w in report["warnings"]))

    def test_student_package_verifies_and_detects_tampering(self):
        package = self.unpack("A01", "pc")
        folder = package / "evaluation_base/batches/study-t"
        self.assertTrue(verify_batch(folder))
        medias = sorted((folder / "media").rglob("*.mp4"))
        medias[0].write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "缺失或已修改"):
            verify_batch(folder)
        with patch("server.HERE", package / "evaluation_base"):
            self.assertEqual(server.find_slot_batch().name, "study-t")
            with self.assertRaises(ValueError):
                server.find_slot_batch("nope")
        with patch("server.HERE", self.root / "nowhere"), self.assertRaisesRegex(ValueError, "没有找到评测数据"):
            server.find_slot_batch()


def connect_db(package):
    return server.connect(package / "evaluation_base/batches/study-t/annotations.sqlite3")


@unittest.skipUnless(video_playback.ffmpeg_executable(), "FFmpeg not available")
class PlaybackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.exe = video_playback.ffmpeg_executable()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, codec):
        path = self.root / name
        out = subprocess.run([self.exe, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                              "testsrc=size=320x180:rate=10:duration=2", "-c:v", codec,
                              *(["-pix_fmt", "yuv420p"] if codec == "libx264" else []), str(path)],
                             capture_output=True)
        if out.returncode:
            self.skipTest(f"cannot encode {codec} here")
        return path

    def test_h264_is_left_alone_and_mpeg4_becomes_h264(self):
        good = self.make("good.mp4", "libx264")
        self.assertFalse(video_playback.needs_copy(good))
        self.assertEqual(video_playback.browser_asset(good, self.root / "cache"), good)
        bad = self.make("old.mp4", "mpeg4")
        self.assertEqual(video_playback.probe_video(bad)["codec"], "mpeg4")
        self.assertTrue(video_playback.needs_copy(bad))
        copy_path = video_playback.browser_asset(bad, self.root / "cache")
        self.assertNotEqual(copy_path, bad)
        info = video_playback.probe_video(copy_path)
        self.assertIn(info["codec"], ("h264", "vp9"))
        self.assertTrue(info["yuv420p"])
        self.assertEqual(video_playback.browser_asset(bad, self.root / "cache"), copy_path)   # cached

    def test_lossless_high444_h264_is_converted(self):
        path = self.root / "cosmos.mp4"
        out = subprocess.run([self.exe, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                              "testsrc=size=320x180:rate=10:duration=2", "-c:v", "libx264", "-qp", "0",
                              "-pix_fmt", "yuv420p", str(path)], capture_output=True)
        if out.returncode:
            self.skipTest("cannot encode lossless H.264 here")
        info = video_playback.probe_video(path)
        self.assertEqual((info["codec"], info["profile"], info["yuv420p"]), ("h264", "high 4:4:4 predictive", True))
        self.assertTrue(video_playback.needs_copy(path))
        copy_path = video_playback.browser_asset(path, self.root / "cache")
        self.assertIn(video_playback.probe_video(copy_path)["profile"], ("high", "main", None, "vp9"))
        self.assertFalse(video_playback.needs_copy(copy_path) and copy_path.suffix == ".mp4")

    def test_unreadable_files_are_passed_through(self):
        fake = self.root / "fake.mp4"
        fake.write_bytes(b"not a video" * 10)
        self.assertIsNone(video_playback.probe_video(fake))
        self.assertFalse(video_playback.needs_copy(fake))
        fake.write_bytes(b"....mp4v...." * 10)
        self.assertTrue(video_playback.needs_copy(fake) or video_playback.ffmpeg_executable() is not None)


if __name__ == "__main__":
    unittest.main()
