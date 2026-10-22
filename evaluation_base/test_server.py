import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from answer_sheet import sheet, side_answers
from server import Handler, aggregate, connect, init_db, load_manifest


class ServerFixture(unittest.TestCase):
    """One-task database plus a local server (shared by several test modules; has no tests itself)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "study.sqlite3"
        (root / "frame.jpg").write_bytes(b"frame-data")
        (root / "a.mp4").write_bytes(b"A-video-contents")
        (root / "b.mp4").write_bytes(b"B-video-contents")
        manifest = root / "tasks.jsonl"
        manifest.write_text(json.dumps({
            "source_case_id": "WMB_1", "track": "risk", "domain": "driving",
            "base_condition": "approach", "preference_a": "stop", "preference_b": "go",
            "initial_frame": "frame.jpg", "video_a": "a.mp4", "video_b": "b.mp4",
            "world_model": "secret-model", "stage": "secret-stage",
        }) + "\n", encoding="utf-8")
        init_db(self.db)
        load_manifest(self.db, manifest)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.db_path = self.db
        self.server.media_root = root
        self.server.target_per_task = 3
        self.server.tasks_per_volunteer = 30
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, token=None, payload=None, headers=None):
        all_headers = dict(headers or {})
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=all_headers)
        with urlopen(req) as response:
            return response.read(), response.headers



class EvaluationFlowTest(ServerFixture):
    def test_task_without_initial_frame_can_be_answered(self):
        with connect(self.db) as db:
            db.execute("UPDATE tasks SET initial_frame=NULL")
        session, _ = self.request("/api/session", payload={"name":"测试志愿者"})
        token = json.loads(session)["token"]
        data, _ = self.request("/api/next", token)
        task = json.loads(data)["task"]
        self.assertIsNone(task["initial_frame_url"])
        with self.assertRaises(HTTPError) as missing:
            self.request(f"/media/{task['id']}/frame", token)
        self.assertEqual(missing.exception.code, 404)
        self.assertTrue(self.request(task["video_x_url"], token)[0])
        answers = sheet({"retention": "uncertain"}, {"retention": "uncertain"})
        self.request("/api/submit", token, payload={"task_id": task["id"], "answers": answers})
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 1)

    def test_names_required_resume_and_explicit_new_person(self):
        with self.assertRaises(HTTPError) as missing:
            self.request("/api/session", payload={"name": "   "})
        self.assertEqual(missing.exception.code, 400)
        data, _ = self.request("/api/session", payload={"name": "张三"})
        token = json.loads(data)["token"]
        data, _ = self.request("/api/session", token, payload={"name": "张三"})
        self.assertEqual(json.loads(data)["token"], token)
        with self.assertRaises(HTTPError):
            self.request("/api/session", token, payload={"name": "李四"})
        data, _ = self.request("/api/session", token, payload={"name": "张三", "new_session": True})
        self.assertNotEqual(json.loads(data)["token"], token)
        profile, _ = self.request("/api/profile", token)
        self.assertEqual(json.loads(profile)["name"], "张三")

    def test_blind_pair_submission_and_consensus(self):
        for _ in range(3):
            session, session_headers = self.request("/api/session", payload={"name":"测试志愿者"})
            token = json.loads(session)["token"]
            task_data, _ = self.request("/api/next", token)
            self.assertNotIn(b"secret-model", task_data)
            self.assertNotIn(b"secret-stage", task_data)
            task = json.loads(task_data)["task"]
            self.assertNotIn("track", task)
            self.assertNotIn("source_case_id", task)
            repeated, _ = self.request("/api/next", token)
            self.assertEqual(task, json.loads(repeated)["task"])
            x_bytes, _ = self.request(task["video_x_url"], token)
            cookie_bytes, _ = self.request(task["video_x_url"],
                                           headers={"Cookie": session_headers["Set-Cookie"].split(";", 1)[0]})
            self.assertEqual(cookie_bytes, x_bytes)
            y_bytes, _ = self.request(task["video_y_url"], token)
            self.assertEqual({x_bytes, y_bytes}, {b"A-video-contents", b"B-video-contents"})
            partial, headers = self.request(task["video_x_url"], token,
                                            headers={"Range": "bytes=1-3"})
            self.assertEqual(partial, x_bytes[1:4])
            self.assertTrue(headers["Content-Range"].startswith("bytes 1-3/"))
            x_is_a = x_bytes.startswith(b"A")
            a = {"reason": "visible action"}
            for side, is_a in (("x", x_is_a), ("y", not x_is_a)):
                a.update(side_answers(side, preference_a="yes" if is_a else "no",
                                      preference_b="no" if is_a else "yes"))
            self.request("/api/submit", token, {"task_id": task["id"], "answers": a})
            done, _ = self.request("/api/next", token)
            self.assertIsNone(json.loads(done)["task"])
            self.request("/api/submit", token, {"task_id": task["id"], "answers": a})
        fourth, _ = self.request("/api/session", payload={"name":"测试志愿者"})
        exhausted, _ = self.request("/api/next", json.loads(fourth)["token"])
        self.assertIsNone(json.loads(exhausted)["task"])
        out = Path(self.tmp.name) / "export"
        aggregate(self.db, out)
        consensus = json.loads((out / "consensus.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(consensus["annotation_count"], 3)
        self.assertTrue(consensus["strict_sbps"])
        self.assertFalse(consensus["needs_adjudication"])

    def test_history_edit_and_owner_isolation(self):
        with connect(self.db) as db:
            row = list(db.execute("SELECT * FROM tasks").fetchone())
            row[0], row[1] = "second-task", "WMB_2"
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        session, _ = self.request("/api/session", payload={"name":"测试志愿者"})
        token = json.loads(session)["token"]
        first_data, _ = self.request("/api/next", token)
        first = json.loads(first_data)["task"]
        media_before, _ = self.request(first["video_x_url"], token)
        answers = sheet()
        self.request("/api/submit", token, {"task_id": first["id"], "answers": answers})
        second_data, _ = self.request("/api/next", token)
        second = json.loads(second_data)["task"]
        self.assertNotEqual(first["id"], second["id"])
        pending, _ = self.request(f"/api/tasks/{second['id']}", token)
        self.assertIsNone(json.loads(pending)["answers"])
        history, _ = self.request("/api/history", token)
        self.assertEqual(json.loads(history)["tasks"], [
            {"id": first["id"], "number": 1, "saved": True},
            {"id": second["id"], "number": 2, "saved": False},
        ])
        self.request("/api/submit", token, {"task_id": second["id"], "answers": answers})
        done, _ = self.request("/api/next", token)
        self.assertIsNone(json.loads(done)["task"])
        review, _ = self.request(f"/api/tasks/{first['id']}", token)
        self.assertEqual(json.loads(review), {"task": first, "answers": answers})

        outsider, _ = self.request("/api/session", payload={"name":"测试志愿者"})
        outsider_token = json.loads(outsider)["token"]
        outsider_history, _ = self.request("/api/history", outsider_token)
        self.assertEqual(json.loads(outsider_history)["tasks"], [])
        with self.assertRaises(HTTPError) as denied:
            self.request(f"/api/tasks/{first['id']}", outsider_token)
        self.assertEqual(denied.exception.code, 404)
        with self.assertRaises(HTTPError) as denied:
            self.request("/api/submit", outsider_token, {"task_id": first["id"], "answers": answers})
        self.assertEqual(denied.exception.code, 403)

        answers["x_preference_a"] = "uncertain"
        self.request("/api/submit", token, {"task_id": first["id"], "answers": answers})
        review, _ = self.request(f"/api/tasks/{first['id']}", token)
        self.assertEqual(json.loads(review)["answers"], answers)
        self.assertNotIn("reason", json.loads(review)["answers"])
        media_after, _ = self.request(first["video_x_url"], token)
        self.assertEqual(media_before, media_after)
        done, _ = self.request("/api/next", token)
        self.assertEqual(json.loads(done)["done"], 2)
        out = Path(self.tmp.name) / "edited"
        aggregate(self.db, out)
        exported = [json.loads(line) for line in (out / "raw_annotations.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(exported), 2)
        edited = next(row for row in exported if row["task_id"] == first["id"])
        self.assertEqual(edited["answers"]["x_preference_a"], "uncertain")

    def test_three_way_disagreement_requires_adjudication(self):
        with connect(self.db) as db:
            task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
            for i, label in enumerate(("yes", "no", "uncertain")):
                person = f"person-{i}"
                answers = {**side_answers("x", preference_a=label),
                           **side_answers("y", preference_a="no", preference_b="yes"), "reason": ""}
                db.execute("INSERT INTO annotators VALUES (?,?)", (person, "time"))
                db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, task_id, 1, "time"))
                db.execute("INSERT INTO annotations VALUES (?,?,?,?)",
                           (person, task_id, json.dumps(answers), "time"))
        out = Path(self.tmp.name) / "disagreement"
        aggregate(self.db, out)
        consensus = json.loads((out / "consensus.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(consensus["consensus"]["video_a_preference_a"], "uncertain")
        self.assertFalse(consensus["strict_sbps"])
        self.assertTrue(consensus["needs_adjudication"])

    def test_changed_frozen_media_is_not_served(self):
        root = Path(self.tmp.name)
        self.server.media_signatures = {
            path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (root / "frame.jpg", root / "a.mp4", root / "b.mp4")}
        session, _ = self.request("/api/session", payload={"name":"测试志愿者"})
        token = json.loads(session)["token"]
        data, _ = self.request("/api/next", token)
        task = json.loads(data)["task"]
        frame, _ = self.request(task["initial_frame_url"], token)
        self.assertEqual(frame, b"frame-data")
        (root / "frame.jpg").write_bytes(b"changed frozen frame")
        with self.assertRaises(HTTPError) as denied:
            self.request(task["initial_frame_url"], token)
        self.assertEqual(denied.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
