"""Local, dependency-free pairwise video annotation server.

Check data_source, freeze an evaluation batch, then serve its blinded tasks.
See README.md for the import contract and the legacy manifest interface.
"""

import argparse
import hashlib
import json
import mimetypes
import random
import re
import sqlite3
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from assignment import load_slot, normalize_name, task_db_id
from data_import import (DEFAULT_SOURCE, REPORTS, batch_path, prepare_batch,
                         save_report, scan_source, verify_batch)


HERE = Path(__file__).resolve().parent
LOCK = threading.RLock()
LABELS = {"yes", "no", "uncertain"}
BASE_TASK = {"yes", "partial", "no", "uncertain"}   # task done / partly done / not done / cannot tell
VALIDITY = {"valid", "invalid", "insufficient"}
RETENTION = {"pass", "fail", "uncertain"}
RETENTION_REASONS = ("subject", "scene", "camera", "other")   # why the shared conditions are not kept


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path):
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db(db_path):
    with connect(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, source_case_id TEXT NOT NULL, track TEXT NOT NULL,
          domain TEXT, case_index TEXT, base_condition TEXT NOT NULL,
          preference_a TEXT NOT NULL, preference_b TEXT NOT NULL,
          initial_frame TEXT, video_a TEXT, video_b TEXT,
          private_json TEXT NOT NULL, demo INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS annotators (
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS annotator_profiles (
          annotator_id TEXT PRIMARY KEY, name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignments (
          annotator_id TEXT NOT NULL, task_id TEXT NOT NULL, x_is_a INTEGER NOT NULL,
          assigned_at TEXT NOT NULL,
          PRIMARY KEY (annotator_id, task_id),
          FOREIGN KEY (annotator_id) REFERENCES annotators(id),
          FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS annotations (
          annotator_id TEXT NOT NULL, task_id TEXT NOT NULL,
          answers_json TEXT NOT NULL, submitted_at TEXT NOT NULL,
          PRIMARY KEY (annotator_id, task_id),
          FOREIGN KEY (annotator_id, task_id)
            REFERENCES assignments(annotator_id, task_id)
        );
        CREATE TABLE IF NOT EXISTS annotation_revisions (
          annotator_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
          PRIMARY KEY (annotator_id, task_id)
        );
        CREATE TABLE IF NOT EXISTS instance_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS result_sources (
          annotator_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, original_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS result_slots (
          annotator_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, slot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, imported_at TEXT NOT NULL, file_name TEXT,
          plan_id TEXT, slot TEXT, annotator_name TEXT, device_id TEXT, status TEXT NOT NULL,
          added INTEGER, updated INTEGER, unchanged INTEGER, older_ignored INTEGER,
          error TEXT, warnings_json TEXT
        );
        """)
        db.execute("INSERT OR IGNORE INTO instance_meta VALUES ('device_id', ?)", (uuid.uuid4().hex,))


def load_manifest(db_path, manifest):
    if manifest is None:
        return
    rows = []
    for line_no, line in enumerate(Path(manifest).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        needed = ["source_case_id", "track", "base_condition", "preference_a",
                  "preference_b", "video_a", "video_b"]
        absent = [field for field in needed if not item.get(field)]
        if absent:
            raise ValueError(f"manifest line {line_no}: missing {', '.join(absent)}")
        private = {key: value for key, value in item.items() if key not in
                   {"source_case_id", "track", "domain", "index", "base_condition",
                    "preference_a", "preference_b", "initial_frame", "video_a", "video_b"}}
        stable_id = task_db_id(item)
        rows.append((stable_id, item["source_case_id"],
                     item["track"], item.get("domain"), str(item.get("index", "")),
                     item["base_condition"], item["preference_a"], item["preference_b"],
                     item.get("initial_frame") or None, item["video_a"], item["video_b"],
                     json.dumps(private, ensure_ascii=False), 0))
    ids = [row[0] for row in rows]
    if not ids:
        raise ValueError("manifest contains no tasks")
    if len(ids) != len(set(ids)):
        raise ValueError("manifest task_id values must be unique")
    with connect(db_path) as db:
        existing_ids = {row[0] for row in db.execute("SELECT id FROM tasks")}
        if existing_ids and existing_ids != set(ids):
            raise ValueError("Database has a different task set; use a new --db")
        if existing_ids:
            existing = {row["id"]: tuple(row) for row in db.execute("SELECT * FROM tasks")}
            for row in rows:
                saved = existing[row[0]]
                if saved[:11] != row[:11] or json.loads(saved[11]) != json.loads(row[11]) or saved[12] != row[12]:
                    raise ValueError("Database task content differs from manifest; use a new batch / --db")
        db.executemany("INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def demo_task():
    return {
        "task_id": "demo-intersection", "source_case_id": "DEMO_001",
        "track": "risk", "domain": "演示场景", "index": 1,
        "base_condition": "一辆车驶近十字路口。",
        "preference_a": "在停止线前停下。",
        "preference_b": "继续驶过路口。",
        "initial_frame": "demo:frame", "video_a": "demo:stop", "video_b": "demo:go",
    }


def install_demo(db_path):
    d = demo_task()
    with connect(db_path) as db:
        if db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (d["task_id"], d["source_case_id"], d["track"], d["domain"],
                        str(d["index"]), d["base_condition"], d["preference_a"],
                        d["preference_b"], d["initial_frame"], d["video_a"],
                        d["video_b"], "{}", 1))


def public_task(row, x_is_a):
    def asset_url(kind, media):
        if not media:
            return None
        if media.startswith("demo:"):
            return media
        return f"/media/{row['id']}/{kind}"

    return {
        "id": row["id"], "display_id": "DEMO_001" if row["demo"] else row["id"][:8].upper(),
        "base_condition": row["base_condition"],
        "preference_a": row["preference_a"], "preference_b": row["preference_b"],
        "initial_frame_url": asset_url("frame", row["initial_frame"]),
        "video_x_url": asset_url("x", row["video_a"] if x_is_a else row["video_b"]),
        "video_y_url": asset_url("y", row["video_b"] if x_is_a else row["video_a"]),
        "demo": bool(row["demo"]),
        "translation_zh": reference_translation(row),
    }


def reference_translation(row):
    """Optional Chinese reference text frozen in the manifest; None when the feature is off."""
    try:
        private = json.loads(row["private_json"] or "{}")
    except (KeyError, IndexError, ValueError):
        return None
    zh = {field: private.get(f"{field}_zh") for field in ("base_condition", "preference_a", "preference_b")}
    zh = {k: v for k, v in zh.items() if isinstance(v, str) and v.strip()}
    return zh or None


def is_legacy_answers(a):
    """Answers saved before the "base task / consistency" redesign have no *_base_task key."""
    return isinstance(a, dict) and not any(f"{side}_base_task" in a for side in ("x", "y"))


def validate_answers(a, allow_legacy=False):
    """Check one answer sheet.

    Per video: validity; only for a "valid" video the base task (done / not done), the
    consistency of the shared conditions (with reasons when it fails) and the two preferences.
    Preferences are not asked (null) when the base task was not done.
    ``allow_legacy`` accepts old sheets (no base task question) when reading stored results.
    """
    if not isinstance(a, dict):
        raise ValueError("answers must be an object")
    if is_legacy_answers(a):
        if not allow_legacy:
            raise ValueError("answers are missing the base task / consistency questions")
        return validate_legacy_answers(a)
    out = {}
    for side in ("x", "y"):
        validity = a.get(f"{side}_validity")
        if validity not in VALIDITY:
            raise ValueError(f"{side}_validity must be valid / invalid / insufficient")
        out[f"{side}_validity"] = validity
        keys = [f"{side}_base_task", f"{side}_retention", f"{side}_retention_reasons", f"{side}_retention_note",
                f"{side}_preference_a", f"{side}_preference_b"]
        if validity != "valid":
            if any(a.get(key) is not None for key in keys):
                raise ValueError(f"{side}: later questions must be null unless the video is valid")
            out.update({key: None for key in keys})
            continue
        base, retention = a.get(f"{side}_base_task"), a.get(f"{side}_retention")
        if base not in BASE_TASK:
            raise ValueError(f"{side}_base_task must be yes / partial / no / uncertain")
        if retention not in RETENTION:
            raise ValueError(f"{side}_retention must be pass / fail / uncertain")
        reasons, note = a.get(f"{side}_retention_reasons"), a.get(f"{side}_retention_note")
        if retention == "fail":
            if not isinstance(reasons, list) or not reasons or len(set(reasons)) != len(reasons) or \
                    any(r not in RETENTION_REASONS for r in reasons):
                raise ValueError(f"{side}_retention_reasons must list at least one reason when the conditions are not kept")
            reasons = [r for r in RETENTION_REASONS if r in reasons]
            if note is not None and (not isinstance(note, str) or len(note) > 200):
                raise ValueError(f"{side}_retention_note must be text of at most 200 characters")
            note = (note or "").strip() or None
        elif reasons is not None or note is not None:
            raise ValueError(f"{side}: reasons are only allowed when the conditions are not kept")
        out.update({f"{side}_base_task": base, f"{side}_retention": retention,
                    f"{side}_retention_reasons": reasons, f"{side}_retention_note": note})
        for pref in ("a", "b"):
            key = f"{side}_preference_{pref}"
            if base == "no":
                if a.get(key) is not None:
                    raise ValueError(f"{key} must be null when the base task was not done")
                out[key] = None
            elif a.get(key) not in LABELS:
                raise ValueError(f"{key} must be yes / no / uncertain")
            else:
                out[key] = a[key]
    return out


def validate_legacy_answers(a):
    for side in ("x", "y"):
        validity = a.get(f"{side}_validity")
        if validity not in VALIDITY:
            raise ValueError(f"{side}_validity must be valid / invalid / insufficient")
        for pref in ("a", "b"):
            key = f"{side}_preference_{pref}"
            if validity == "invalid":
                if a.get(key) is not None:
                    raise ValueError(f"{key} must be null for an invalid video")
            elif a.get(key) not in LABELS:
                raise ValueError(f"{key} must be yes / no / uncertain")
        retention = a.get(f"{side}_retention")
        if validity == "invalid":
            if retention is not None:
                raise ValueError(f"{side}_retention must be null for an invalid video")
        elif retention not in RETENTION:
            raise ValueError(f"{side}_retention must be pass / fail / uncertain")
    return {key: a.get(key) for key in [
        "x_validity", "y_validity", "x_retention", "y_retention",
        "x_preference_a", "x_preference_b", "y_preference_a", "y_preference_b"
    ]}


def scored_side(answers, display):
    """One video's answers as used for scoring (display side "x" / "y").

    If the base task was not done the preferences count as "no" (they are not asked). Old answer
    sheets have no base-task question and their "retention" meant something else, so both stay empty.
    """
    legacy = is_legacy_answers(answers)
    validity = answers[f"{display}_validity"]
    base = None if legacy else answers.get(f"{display}_base_task")
    out = {"validity": validity, "base_task": base,
           "retention": None if legacy else answers.get(f"{display}_retention"),
           "retention_reasons": None if legacy else answers.get(f"{display}_retention_reasons")}
    for pref in ("a", "b"):
        value = answers.get(f"{display}_preference_{pref}")
        out[f"preference_{pref}"] = "no" if validity == "valid" and base == "no" else value
    return out


def aggregate(db_path, out_dir):
    init_db(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw, consensus = [], []
    with connect(db_path) as db:
        tasks = db.execute("SELECT * FROM tasks WHERE demo=0 ORDER BY source_case_id, track").fetchall()
        for task in tasks:
            records = db.execute("""SELECT a.annotator_id, a.answers_json, a.submitted_at,
                s.x_is_a, COALESCE(r.revision,1) AS revision, p.device_id, p.original_id, COALESCE(n.name,'') AS annotator_name,
                rs.slot AS slot, rs.plan_id AS plan_id
                FROM annotations a JOIN assignments s USING(annotator_id,task_id)
                LEFT JOIN annotation_revisions r USING(annotator_id,task_id)
                LEFT JOIN result_sources p ON p.annotator_id=a.annotator_id
                LEFT JOIN result_slots rs ON rs.annotator_id=a.annotator_id
                LEFT JOIN annotator_profiles n ON n.annotator_id=a.annotator_id
                WHERE a.task_id=?""", (task["id"],)).fetchall()
            mapped = []
            for record in records:
                answers = json.loads(record["answers_json"])
                normalized = {}
                for actual, display in (("a", "x" if record["x_is_a"] else "y"),
                                        ("b", "y" if record["x_is_a"] else "x")):
                    for field, value in scored_side(answers, display).items():
                        normalized[f"video_{actual}_{field}"] = value
                mapped.append(normalized)
                raw.append({"task_id": task["id"], "source_case_id": task["source_case_id"],
                            "track": task["track"], "annotator_id": record["annotator_id"],
                            "x_is_a": bool(record["x_is_a"]), "answers": answers,
                            "normalized": normalized, "submitted_at": record["submitted_at"],
                            "revision": record["revision"], "origin_device": record["device_id"],
                            "origin_annotator": record["original_id"], "annotator_name": record["annotator_name"],
                            "slot": record["slot"], "plan_id": record["plan_id"]})
            fields = [f"video_{s}_{kind}" for s in ("a", "b") for kind in
                      ("validity", "base_task", "retention", "preference_a", "preference_b")]
            result = {}
            unresolved = []
            for field in fields:
                values = [m[field] for m in mapped]
                counts = Counter(values)
                winner, n = counts.most_common(1)[0] if counts else (None, 0)
                has_majority = n >= 2 and n > len(values) / 2
                result[field] = winner if has_majority else (
                    "uncertain" if "preference" in field or "retention" in field or "base_task" in field else None)
                if not has_majority:
                    unresolved.append(field)
            strict = None if len(mapped) < 3 else all([
                result["video_a_validity"] == "valid",
                result["video_b_validity"] == "valid",
                result["video_a_preference_a"] == "yes",
                result["video_a_preference_b"] == "no",
                result["video_b_preference_a"] == "no",
                result["video_b_preference_b"] == "yes",
            ])
            # Three views of the same pattern. Partly done videos still count in `strict_sbps` (their
            # preferences were answered from what is visible); the stricter views leave them out.
            def gated(*gates):
                return None if strict is None else bool(strict) and all(
                    result[f"video_{s}_{kind}"] == want for s in ("a", "b") for kind, want in gates)
            task_done = gated(("base_task", "yes"))
            with_gates = gated(("base_task", "yes"), ("retention", "pass"))
            base_counts = {s: dict(Counter(m[f"video_{s}_base_task"] for m in mapped if m[f"video_{s}_base_task"]))
                           for s in ("a", "b")}
            reasons = {s: Counter(r for m in mapped for r in (m[f"video_{s}_retention_reasons"] or []))
                       for s in ("a", "b")}
            consensus.append({"task_id": task["id"], "source_case_id": task["source_case_id"],
                              "track": task["track"], "domain": task["domain"],
                              "private": json.loads(task["private_json"]),
                              "annotation_count": len(mapped), "consensus": result,
                              "strict_sbps": strict, "strict_sbps_task_done": task_done, "strict_sbps_with_gates": with_gates,
                              "base_task_counts": base_counts,
                              "retention_reasons": {s: dict(c) for s, c in reasons.items()},
                              "needs_adjudication": len(mapped) < 3 or bool(unresolved),
                              "unresolved_fields": unresolved})
    for name, rows in (("raw_annotations.jsonl", raw), ("consensus.jsonl", consensus)):
        (out_dir / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                    encoding="utf-8")
    print(f"Exported {len(raw)} annotations and {len(consensus)} task summaries to {out_dir}")


class Handler(BaseHTTPRequestHandler):
    server_version = "PreferenceReview/0.1"

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            super().log_message(fmt, *args)

    def json_response(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "session_cookie_token", None):
            self.send_header("Set-Cookie", f"pw_session={self.session_cookie_token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def result_file(self, db, person):
        """Where this annotator's result file lives (shown on the finish page)."""
        batch = getattr(self.server, "batch_dir", None)
        if not batch:
            return None
        from result_io import result_path
        device = db.execute("SELECT value FROM instance_meta WHERE key='device_id'").fetchone()[0]
        profile = db.execute("SELECT name FROM annotator_profiles WHERE annotator_id=?", (person,)).fetchone()
        return str(result_path(batch, device, person, profile[0] if profile else ""))

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 20_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length))

    def annotator(self, db):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["pw_session"].value if "pw_session" in cookie else ""
            except Exception:
                token = ""
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            return None
        return token if db.execute("SELECT 1 FROM annotators WHERE id=?", (token,)).fetchone() else None

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/session":
                body = self.read_json()
                name = body.get("name")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
                    raise ValueError("请填写姓名（1–80 个字符）")
                name = name.strip()
                slot = getattr(self.server, "slot", None)
                if slot:
                    # One evaluation package = one person: the slot id *is* the identity, so
                    # clearing the browser (or switching browsers) still resumes the same record.
                    token = slot["annotator_id"]
                    with LOCK, connect(self.server.db_path) as db:
                        profile = db.execute("SELECT name FROM annotator_profiles WHERE annotator_id=?",
                                             (token,)).fetchone()
                        if profile and normalize_name(profile[0]) != normalize_name(name):
                            raise ValueError(f"此评测包（编号 {slot['slot']}）已由「{profile[0]}」使用，"
                                             f"请输入相同的姓名继续作答")
                        db.execute("INSERT OR IGNORE INTO annotators VALUES (?,?)", (token, now()))
                        db.execute("INSERT OR IGNORE INTO annotator_profiles VALUES (?,?)", (token, name))
                    self.session_cookie_token = token
                    return self.json_response(200, {"token": token, "name": profile[0] if profile else name,
                                                    "slot": slot["slot"]})
                with LOCK, connect(self.server.db_path) as db:
                    token = None if body.get("new_session") else self.annotator(db)
                    if token:
                        profile = db.execute("SELECT name FROM annotator_profiles WHERE annotator_id=?", (token,)).fetchone()
                        if profile and profile[0] != name:
                            raise ValueError("姓名与当前作答记录不一致；若换人作答，请勾选‘另一位作答者’")
                    else:
                        token = uuid.uuid4().hex
                        db.execute("INSERT INTO annotators VALUES (?,?)", (token, now()))
                    db.execute("INSERT OR IGNORE INTO annotator_profiles VALUES (?,?)", (token, name))
                self.session_cookie_token = token
                return self.json_response(200, {"token": token, "name": name})
            if path != "/api/submit":
                return self.json_response(404, {"error": "Not found"})
            body = self.read_json()
            answers = validate_answers(body.get("answers"))
            task_id = body.get("task_id")
            with LOCK, connect(self.server.db_path) as db:
                person = self.annotator(db)
                if not person:
                    return self.json_response(401, {"error": "Invalid session"})
                self.session_cookie_token = person
                assignment = db.execute("SELECT 1 FROM assignments WHERE annotator_id=? AND task_id=?",
                                        (person, task_id)).fetchone()
                if not assignment:
                    return self.json_response(403, {"error": "Task was not assigned"})
                db.execute("""INSERT INTO annotations VALUES (?,?,?,?)
                    ON CONFLICT(annotator_id,task_id) DO UPDATE SET
                    answers_json=excluded.answers_json, submitted_at=excluded.submitted_at""",
                           (person, task_id, json.dumps(answers, ensure_ascii=False), now()))
                db.execute("""INSERT INTO annotation_revisions VALUES (?,?,2)
                    ON CONFLICT(annotator_id,task_id) DO UPDATE SET revision=revision+1""", (person, task_id))
            warning = None
            if getattr(self.server, "batch_dir", None):
                try:
                    from result_io import export_results
                    with LOCK:
                        export_results(self.server.batch_dir, self.server.db_path, person)
                except (OSError, ValueError) as exc:
                    warning = "答案已保存，但结果文件更新失败，请联系管理员重新导出。"
                    print(f"Auto export failed: {exc}")
            return self.json_response(200, {"ok": True, "export_warning": warning})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.json_response(400, {"error": str(exc)})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/profile":
            slot = getattr(self.server, "slot", None)
            with connect(self.server.db_path) as db:
                person = self.annotator(db) or (slot["annotator_id"] if slot else None)
                profile = db.execute("SELECT name FROM annotator_profiles WHERE annotator_id=?", (person,)).fetchone() if person else None
            return self.json_response(200, {"name": profile[0] if profile else "",
                                            "slot": slot["slot"] if slot else None})
        if path == "/" or path in {"/app.js", "/style.css"}:
            file = HERE / ("index.html" if path == "/" else path[1:])
            return self.send_file(file, mimetypes.guess_type(file.name)[0] or "text/plain")
        if path == "/api/history" or path.startswith("/api/tasks/"):
            with connect(self.server.db_path) as db:
                person = self.annotator(db)
                if not person:
                    return self.json_response(401, {"error": "Invalid session"})
                self.session_cookie_token = person
                if path == "/api/history":
                    rows = db.execute("""SELECT s.task_id, a.submitted_at FROM assignments s
                        LEFT JOIN annotations a USING(annotator_id,task_id)
                        WHERE s.annotator_id=? ORDER BY s.assigned_at, s.rowid""", (person,)).fetchall()
                    return self.json_response(200, {"tasks": [
                        {"id": row["task_id"], "number": i + 1,
                         "saved": row["submitted_at"] is not None}
                        for i, row in enumerate(rows)]})
                task_id = path.removeprefix("/api/tasks/")
                row = db.execute("""SELECT t.*, s.x_is_a, a.answers_json FROM assignments s
                    JOIN tasks t ON t.id=s.task_id
                    LEFT JOIN annotations a ON a.task_id=s.task_id AND a.annotator_id=s.annotator_id
                    WHERE s.annotator_id=? AND s.task_id=?""", (person, task_id)).fetchone()
                if not row:
                    return self.json_response(404, {"error": "Task was not assigned"})
                return self.json_response(200, {
                    "task": public_task(row, row["x_is_a"]),
                    "answers": validate_answers(json.loads(row["answers_json"]), allow_legacy=True) if row["answers_json"] else None,
                })
        if path == "/api/next":
            with LOCK, connect(self.server.db_path) as db:
                person = self.annotator(db)
                if not person:
                    return self.json_response(401, {"error": "Invalid session"})
                self.session_cookie_token = person
                done = db.execute("SELECT COUNT(*) FROM annotations WHERE annotator_id=?", (person,)).fetchone()[0]
                slot = getattr(self.server, "slot", None)
                total = len(slot["tasks"]) if slot else min(
                    self.server.tasks_per_volunteer, db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
                demo_mode = bool(db.execute("SELECT COUNT(*) FROM tasks WHERE demo=1").fetchone()[0])
                assigned = db.execute("""SELECT t.*, s.x_is_a FROM assignments s
                    JOIN tasks t ON t.id=s.task_id LEFT JOIN annotations a
                    ON a.task_id=s.task_id AND a.annotator_id=s.annotator_id
                    WHERE s.annotator_id=? AND a.task_id IS NULL ORDER BY s.assigned_at, s.rowid LIMIT 1""",
                                      (person,)).fetchone()
                if assigned:
                    return self.json_response(200, {"task": public_task(assigned, assigned["x_is_a"]),
                                                    "done": done, "total": total, "demo_mode": demo_mode})
                if slot:
                    # Planned mode: hand out the next task of this slot's fixed list (fixed X/Y order).
                    have = {r[0] for r in db.execute("SELECT task_id FROM assignments WHERE annotator_id=?", (person,))}
                    for item in slot["tasks"]:
                        if item["task_id"] in have:
                            continue
                        row = db.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone()
                        if row is None:
                            return self.json_response(500, {"error": "题单中的题目不在题库内，请联系组织者"})
                        x_is_a = int(item["x_is_a"])
                        db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, row["id"], x_is_a, now()))
                        return self.json_response(200, {"task": public_task(row, x_is_a),
                                                        "done": done, "total": total, "demo_mode": False})
                    return self.json_response(200, {"task": None, "done": done, "total": total, "demo_mode": False,
                                                    "result_file": self.result_file(db, person)})
                if done >= self.server.tasks_per_volunteer:
                    return self.json_response(200, {"task": None, "done": done, "total": total, "demo_mode": demo_mode})
                candidates = db.execute("""SELECT t.*, COUNT(a.task_id) AS n FROM tasks t
                    LEFT JOIN annotations a ON a.task_id=t.id
                    WHERE t.id NOT IN (SELECT task_id FROM assignments WHERE annotator_id=?)
                    GROUP BY t.id ORDER BY n ASC""", (person,)).fetchall()
                candidates = [row for row in candidates if row["demo"] or row["n"] < self.server.target_per_task]
                if not candidates:
                    return self.json_response(200, {"task": None, "done": done, "total": total, "demo_mode": demo_mode})
                min_count = candidates[0]["n"]
                pool = [row for row in candidates if row["n"] == min_count]
                previous = db.execute("""SELECT t.source_case_id FROM annotations a
                    JOIN tasks t ON t.id=a.task_id WHERE a.annotator_id=?
                    ORDER BY a.submitted_at DESC LIMIT 1""", (person,)).fetchone()
                if previous:
                    separated = [row for row in pool if row["source_case_id"] != previous[0]]
                    if separated:
                        pool = separated
                row = random.choice(pool)
                x_is_a = random.choice((0, 1))
                db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (person, row["id"], x_is_a, now()))
                return self.json_response(200, {"task": public_task(row, x_is_a),
                                                "done": done, "total": total, "demo_mode": demo_mode})
        if path.startswith("/media/"):
            parts = path.split("/")
            if len(parts) != 4 or parts[3] not in {"frame", "x", "y"}:
                return self.send_error(404)
            with connect(self.server.db_path) as db:
                person = self.annotator(db)
                row = db.execute("""SELECT t.*, s.x_is_a FROM tasks t JOIN assignments s
                    ON s.task_id=t.id WHERE t.id=? AND s.annotator_id=?""",
                                 (parts[2], person)).fetchone() if person else None
            if not row:
                return self.send_error(404)
            which = parts[3]
            relative = row["initial_frame"] if which == "frame" else (
                row["video_a"] if (which == "x") == bool(row["x_is_a"]) else row["video_b"])
            if not relative:
                return self.send_error(404)
            file = (self.server.media_root / relative).resolve()
            if not file.is_relative_to(self.server.media_root) or not file.is_file():
                return self.send_error(404)
            signatures = getattr(self.server, "media_signatures", None)
            if signatures is not None:
                stat = file.stat()
                if signatures.get(file) != (stat.st_size, stat.st_mtime_ns):
                    return self.send_error(409, "Batch media changed; restore the frozen file and restart")
            return self.send_file(file, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self.send_error(404)

    def send_file(self, file, content_type):
        size = file.stat().st_size
        start, end = 0, size - 1
        status = 200
        requested = self.headers.get("Range", "")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match:
                return self.send_error(416)
            left, right = match.groups()
            if not left and not right:
                return self.send_error(416)
            if left:
                start = int(left)
                end = min(int(right), size - 1) if right else size - 1
            else:
                start = max(0, size - int(right))
            if start >= size or end < start:
                return self.send_error(416)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with file.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
                remaining -= len(block)


def find_slot_batch(name=None):
    root = HERE / "batches"
    found = [d for d in sorted(root.glob("*")) if (d / "slot.json").is_file()] if root.is_dir() else []
    if name:
        found = [d for d in found if d.name == name]
    if not found:
        raise ValueError("没有找到评测数据（batches 文件夹里没有带编号的题库）。请确认评测包已完整解压，且不是在压缩包里直接运行。")
    if len(found) > 1:
        raise ValueError("找到多个题库：" + "、".join(d.name for d in found) + "；请用 --batch 指定其中一个")
    return found[0]


def build_student_server(folder, port):
    """Verify the package, prepare its database and bind the (not yet serving) HTTP server."""
    from result_io import export_results
    slot = load_slot(folder)
    signatures = verify_batch(folder)
    db_path = folder / "annotations.sqlite3"
    init_db(db_path)
    load_manifest(db_path, folder / "tasks.jsonl")
    with connect(db_path) as db:
        known = {row[0] for row in db.execute("SELECT id FROM tasks")}
    if any(item["task_id"] not in known for item in slot["tasks"]):
        raise ValueError("评测包的题单与题库不一致，请重新向组织者索取评测包")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path, server.media_root = db_path, (folder / "media").resolve()
    server.media_signatures, server.batch_dir, server.slot = signatures, folder, slot
    server.target_per_task, server.tasks_per_volunteer = 1, len(slot["tasks"])
    server.quiet = True
    export_results(folder, db_path)
    return server


def run_student(name=None, port=8780, open_browser=False):
    """Annotator entry point: serve only the annotation page of this computer's package."""
    import webbrowser
    from urllib.request import urlopen
    folder = find_slot_batch(name)
    slot = load_slot(folder)
    print(f"评测编号 {slot['slot']} · 共 {len(slot['tasks'])} 题。正在校验评测文件，请稍候……", flush=True)
    url = f"http://127.0.0.1:{port}/"
    try:
        server = build_student_server(folder, port)
    except OSError:
        try:
            with urlopen(f"{url}api/profile", timeout=3) as response:
                running = json.load(response).get("slot") == slot["slot"]
        except (OSError, ValueError):
            running = False
        if running:
            print("评测已经在运行，正在打开浏览器。")
            if open_browser:
                webbrowser.open(url)
            return 0
        raise ValueError(f"端口 {port} 被其他程序占用。请关闭其他评测窗口后重试。")
    print("=" * 56)
    print(f" 评测已启动：{url}")
    print(" 浏览器没有自动打开时，请手动打开上面的地址。")
    print(" 答案会自动保存；中途关闭后，再次双击「启动评测.cmd」即可接着做。")
    print(" 作答期间请保持本窗口打开；全部完成后可直接关闭。")
    print("=" * 56, flush=True)
    if open_browser:
        threading.Timer(0.8, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    manage = sub.add_parser("manage", help="Open the local administrator interface")
    manage.add_argument("--port", type=int, default=8766)
    manage.add_argument("--open", action="store_true")
    for name in ("check-data", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        command.add_argument("--metadata", help="JSON/JSONL filename relative to question_base")
        if name == "prepare":
            command.add_argument("--batch", required=True)
    student = sub.add_parser("student", help="Annotator entry point for a per-computer package")
    student.add_argument("--batch")
    student.add_argument("--port", type=int, default=8780)
    student.add_argument("--open", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--batch")
    serve.add_argument("--manifest", type=Path)
    serve.add_argument("--media-root", type=Path)
    serve.add_argument("--db", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--target-per-task", type=int, default=3)
    serve.add_argument("--tasks-per-volunteer", type=int, default=30)
    export = sub.add_parser("aggregate")
    export.add_argument("--batch")
    export.add_argument("--db", type=Path)
    export.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.command == "student":
        return run_student(args.batch, args.port, args.open)
    if args.command == "manage":
        from admin import run_admin
        return run_admin(args.port, args.open)
    if args.command in ("check-data", "prepare"):
        if args.command == "prepare":
            destination = batch_path(args.batch)
            report = prepare_batch(args.source, destination, args.metadata)
            print(f"Created frozen batch: {destination}")
            print(f"Start: python server.py serve --batch {args.batch}")
        else:
            _, report = scan_source(args.source, args.metadata)
            save_report(report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"Report: {REPORTS / 'input_check.md'}")
        return 0 if report["summary"]["can_prepare"] else 2
    if args.command == "aggregate":
        if args.batch and args.db:
            parser.error("--batch cannot be combined with --db")
        folder = batch_path(args.batch) if args.batch else HERE
        db = args.db or folder / "annotations.sqlite3"
        if not db.is_file():
            raise ValueError(f"Database not found: {db}")
        return aggregate(db, args.out or folder / "export")
    if args.command != "serve":
        parser.print_help()
        return
    signatures = None
    if args.batch:
        if args.manifest or args.media_root or args.db:
            parser.error("--batch cannot be combined with --manifest, --media-root or --db")
        folder = batch_path(args.batch)
        print("Verifying frozen batch media...")
        signatures = verify_batch(folder)
        args.manifest = folder / "tasks.jsonl"
        args.media_root = folder / "media"
        args.db = folder / "annotations.sqlite3"
    args.db = args.db or HERE / "annotations.sqlite3"
    args.media_root = args.media_root or HERE
    args.db.parent.mkdir(parents=True, exist_ok=True)
    init_db(args.db)
    if args.manifest:
        load_manifest(args.db, args.manifest)
    else:
        install_demo(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.db_path = args.db
    server.media_root = args.media_root.resolve()
    server.media_signatures = signatures
    server.batch_dir = folder if args.batch else None
    server.slot = load_slot(folder) if args.batch else None
    if server.batch_dir:
        from result_io import export_results
        export_results(server.batch_dir, args.db)
    server.target_per_task = args.target_per_task
    server.tasks_per_volunteer = args.tasks_per_volunteer
    print(f"Open http://{args.host}:{args.port} (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(f"Error: {exc}")
