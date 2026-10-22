import copy
import io
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import admin
from data_import import prepare_batch
from answer_sheet import sheet
from result_io import export_results, import_results
from server import Handler, connect, init_db, load_manifest, now


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = self.root / "source"
        (source / "question_base/images").mkdir(parents=True)
        (source / "question_base/images/frame.jpg").write_bytes(b"frame")
        (source / "question_base/conditions.jsonl").write_text(json.dumps({
            "source_case_id": "WMB_0001", "track": "risk", "first_frame": "images/frame.jpg",
            "base_condition": {"text": "intersection"}, "preference_condition_A": {"text": "stop"},
            "preference_condition_B": {"text": "go"}}), encoding="utf-8")
        video = source / "question_video/secret-model/risk"
        video.mkdir(parents=True)
        for side in ("A", "B"):
            (video / f"WMB_0001__{side}__policy_v3.mp4").write_bytes(side.encode() * 10)
        self.batch = self.root / "batches/study-test"
        with patch("data_import.REPORTS", self.root / "reports"):
            prepare_batch(source, self.batch)
        self.patches = [patch("admin.BATCHES", self.root / "batches"),
                        patch("result_io.RESULTS", self.root / "evaluation_results"),
                        patch("distribution.PACKAGES", self.root / "evaluation_packages"),
                        patch("admin.batch_path", lambda name: self.root / "batches" / name)]
        for item in self.patches:
            item.start()
        self.db = self.batch / "annotations.sqlite3"
        init_db(self.db)
        load_manifest(self.db, self.batch / "tasks.jsonl")
        with connect(self.db) as db:
            self.task = db.execute("SELECT id FROM tasks").fetchone()[0]
        self.answers = sheet()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def add_answer(self, db_path, person, x_is_a=True, revision=2):
        with connect(db_path) as db:
            db.execute("INSERT INTO annotators VALUES (?,?)", (person, now()))
            db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, self.task, int(x_is_a), now()))
            db.execute("INSERT INTO annotations VALUES (?,?,?,?)", (person, self.task, json.dumps(self.answers), now()))
            db.execute("INSERT INTO annotation_revisions VALUES (?,?,?)", (person, self.task, revision))

    def package(self, folder=None):
        return json.loads(export_results(folder or self.batch)[0].read_text(encoding="utf-8"))

    def test_two_computers_deduplicate_update_and_aggregate(self):
        self.add_answer(self.db, "same-browser-id", True)
        first = self.package()
        self.assertEqual(import_results(self.batch, first)["added"], 1)
        self.assertEqual(import_results(self.batch, first)["unchanged"], 1)
        updated = copy.deepcopy(first)
        updated["records"][0]["revision"] += 1
        updated["records"][0]["answers"]["x_preference_a"] = "uncertain"
        self.assertEqual(import_results(self.batch, updated)["updated"], 1)
        self.assertEqual(import_results(self.batch, first)["older_ignored"], 1)
        # A separate installation starts with no copied database and gets its own device ID.
        second = self.root / "computer-two/study-test"
        second.mkdir(parents=True)
        for name in ("snapshot.json", "tasks.jsonl"):
            shutil.copyfile(self.batch / name, second / name)
        other_db = second / "annotations.sqlite3"
        init_db(other_db)
        load_manifest(other_db, second / "tasks.jsonl")
        self.add_answer(other_db, "same-browser-id", False)
        other = self.package(second)
        self.assertNotEqual(first["device_id"], other["device_id"])
        self.assertEqual(import_results(self.batch, other)["added"], 1)
        tasks = admin.batch_tasks(self.batch)
        self.assertEqual(tasks[0]["annotation_count"], 2)
        self.assertEqual({a["x_is_a"] for a in tasks[0]["answers"]}, {0, 1})
        summary = admin.aggregate_batch(self.batch)
        self.assertEqual(summary["answer_count"], 2)
        self.assertEqual(summary["groups"][0]["scored"], 0)
        raw = [json.loads(line) for line in (self.batch / "aggregate/raw_annotations.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({r["origin_device"] for r in raw}), 2)

    def test_wrong_batch_conflicts_and_partial_file_rollback(self):
        self.add_answer(self.db, "person")
        package = self.package()
        import_results(self.batch, package)
        # A file from another batch is matched task by task: identical content still counts ...
        other = copy.deepcopy(package)
        other["batch_fingerprint"] = "other-batch"
        result = import_results(self.batch, other)
        self.assertTrue(result["cross_batch"])
        self.assertEqual(result["unchanged"], 1)
        # ... but an old-format file (no per-task fingerprints) needs its original batch folder.
        legacy = copy.deepcopy(other)
        for record in legacy["records"]:
            record.pop("task_fingerprint")
        with self.assertRaisesRegex(ValueError, "旧题库"):
            import_results(self.batch, legacy)
        conflict = copy.deepcopy(package)
        conflict["records"][0]["answers"]["x_preference_a"] = "no"
        with self.assertRaisesRegex(ValueError, "同一版本"):
            import_results(self.batch, conflict)
        bad = copy.deepcopy(package)
        bad["records"][0]["annotator_id"] = "new-person"
        bad["records"].append({**package["records"][0], "task_id": "unknown"})
        with self.assertRaises(ValueError):
            import_results(self.batch, bad)
        with connect(self.batch / "merged.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 1)

    def test_admin_http_isolation_download_kit_and_auto_export(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), admin.AdminHandler)
        server.state = admin.AdminState()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, body=None, token=None, headers=None):
            return urlopen(Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Admin-Token": token or "", **(headers or {})}))
        volunteer = None
        try:
            with request("/") as response:
                self.assertIn("研究管理台", response.read().decode())
            with request("/api/status") as response:
                status = json.load(response)
            with self.assertRaises(HTTPError) as denied:
                request("/api/stop", {"batch": "study-test"})
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError):
                request("/api/status", headers={"Host": "attacker.example"})
            with self.assertRaises(HTTPError) as denied:
                request("/api/start", {"batch": "study-test", "host": "0.0.0.0"}, status["token"])
            self.assertEqual(denied.exception.code, 400)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with request("/api/start", {"batch": "study-test", "port": port}, status["token"]) as response:
                started = json.load(response)
            with urlopen(started["url"]) as response:
                self.assertIn("视频盲评", response.read().decode())
            with request("/api/stop", {"batch": "study-test"}, status["token"]) as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertNotIn("study-test", server.state.running)
            admin.build_package(self.batch)
            with request("/api/download-kit?batch=study-test") as response:
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    names = archive.namelist()
                    self.assertIn("evaluation_base/启动管理界面.cmd", names)
                    self.assertFalse(any("sqlite" in n or "/results/" in n for n in names))
            # Real volunteer HTTP submit updates the result file automatically.
            volunteer = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            volunteer.db_path, volunteer.media_root = self.db, self.batch / "media"
            volunteer.batch_dir = self.batch
            volunteer.target_per_task, volunteer.tasks_per_volunteer = 3, 30
            vt = threading.Thread(target=volunteer.serve_forever, daemon=True)
            vt.start()
            url = f"http://127.0.0.1:{volunteer.server_port}"
            with urlopen(Request(url + "/api/session", data=json.dumps({"name":"测试志愿者"}).encode())) as response:
                token = json.load(response)["token"]
            headers = {"Authorization": f"Bearer {token}"}
            with urlopen(Request(url + "/api/next", headers=headers)) as response:
                task = json.load(response)["task"]
            for answer in ("yes", "uncertain"):
                self.answers["x_preference_a"] = answer
                body = {"task_id": task["id"], "answers": self.answers}
                with urlopen(Request(url + "/api/submit", data=json.dumps(body).encode(), headers=headers)) as response:
                    self.assertIsNone(json.load(response)["export_warning"])
            files = list((self.root / "evaluation_results/study-test").glob("*.json"))
            self.assertEqual(len(files), 1)
            package = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(package["records"][0]["revision"], 3)
            self.assertEqual(package["records"][0]["answers"]["x_preference_a"], "uncertain")
            with request("/api/status") as response:
                local = json.load(response)["batches"][0]["local_results"]
            self.assertEqual(local["answers"], 1)
            self.assertTrue(local["current"])
            self.assertEqual(Path(local["files"][0]["path"]), files[0])
            with self.assertRaises(HTTPError) as denied:
                urlopen(url + "/api/status")
            self.assertEqual(denied.exception.code, 404)
        finally:
            for name in list(server.state.running):
                server.state.stop(name)
            if volunteer:
                volunteer.shutdown()
                volunteer.server_close()
                vt.join()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_local_result_status_detects_unsaved_export_and_recovery(self):
        self.assertFalse(admin.local_results(self.batch)["exists"])
        self.add_answer(self.db, "person")
        export_results(self.batch)
        self.assertTrue(admin.local_results(self.batch)["current"])
        with connect(self.db) as db:
            db.execute("UPDATE annotation_revisions SET revision=revision+1")
        self.assertFalse(admin.local_results(self.batch)["current"])
        export_results(self.batch)
        self.assertTrue(admin.local_results(self.batch)["current"])
        status = admin.local_results(self.batch)
        Path(status["files"][0]["path"]).unlink()
        self.assertFalse(admin.local_results(self.batch)["exists"])
        self.assertEqual(admin.local_results(self.batch)["answers"], 1)

    def test_local_start_automatically_skips_occupied_port(self):
        manager = admin.AdminState()
        actual_server = admin.ThreadingHTTPServer
        calls = []
        def bind(address, handler):
            calls.append(address)
            if len(calls) == 1:
                raise OSError(admin.errno.EADDRINUSE, "occupied")
            return actual_server((address[0], 0), handler)
        try:
            with patch("admin.ThreadingHTTPServer", side_effect=bind):
                info = manager.start("study-test", None, "127.0.0.1", 3, 30)
            self.assertEqual(calls, [("127.0.0.1", 8767), ("127.0.0.1", 8768)])
            self.assertEqual(info["host"], "127.0.0.1")
            with urlopen(info["url"]) as response:
                self.assertEqual(response.status, 200)
        finally:
            manager.stop("study-test")

    def test_named_results_are_separate_safe_and_keep_legacy_import(self):
        self.add_answer(self.db, "person-one")
        self.add_answer(self.db, "person-two")
        with connect(self.db) as db:
            db.executemany("INSERT INTO annotator_profiles VALUES (?,?)",
                           [("person-one", "张三/../测试"), ("person-two", "张三/../测试")])
        paths = export_results(self.batch)
        self.assertEqual(len(set(paths)), 2)
        for path in paths:
            self.assertEqual(path.parent, self.root / "evaluation_results/study-test")
            self.assertIn("张三", path.name)
            package = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(package["records"]), 1)
            self.assertEqual(import_results(self.batch, package)["added"], 1)
            self.assertEqual(import_results(self.batch, package)["unchanged"], 1)
        legacy = json.loads(paths[0].read_text(encoding="utf-8"))
        legacy["records"][0].pop("annotator_name")
        self.assertEqual(import_results(self.batch, legacy)["unchanged"], 1)
        self.assertTrue(admin.local_results(self.batch)["current"])
        self.assertEqual(admin.batch_tasks(self.batch)[0]["answers"][0]["annotator_name"], "张三/../测试")

    def test_creation_job_finishes_and_reports_failure(self):
        manager = admin.AdminState()
        def wait(limit=20):
            deadline = time.monotonic() + limit
            while manager.job["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.05)
        with patch("admin.DEFAULT_SOURCE", self.root / "source"):
            manager.prepare("new-bank")
            wait()
        self.assertEqual(manager.job["status"], "complete", manager.job)
        self.assertEqual(manager.job["percent"], 100)
        self.assertEqual(manager.job["kind"], "prepare")
        self.assertTrue((self.root / "batches/new-bank/snapshot.json").is_file())
        # The optional full (unassigned) package is a separate job.
        manager.prepare("new-bank", package_only=True)
        wait()
        self.assertEqual(manager.job["status"], "complete", manager.job)
        target = Path(manager.job["package_path"])
        self.assertEqual(target.parent, self.root / "evaluation_packages")
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("evaluation_base/distribution.py", archive.namelist())
            self.assertIn("evaluation_base/assignment.py", archive.namelist())
            self.assertFalse(any("sqlite" in n or "evaluation_results" in n for n in archive.namelist()))
        with patch("admin.DEFAULT_SOURCE", self.root / "source"):
            manager.prepare("new-bank")
            wait(5)
        self.assertEqual(manager.job["status"], "failed")
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
