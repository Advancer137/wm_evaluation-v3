"""Local-only management UI, isolated from the volunteer HTTP server."""
import io
import errno
import json
import secrets
import threading
import webbrowser
import zipfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

from data_import import (BATCHES, DEFAULT_SOURCE, HERE, REPORTS, VIDEO_NAME, batch_path,
                         inside, prepare_batch, save_report, scan_source, verify_batch)
from assignment import PLAN_RE, SLOT_RE, create_plan, list_plans, load_plan, load_slot
from report import build_report
from result_io import (export_results, import_results, log_rejection, seed_local, sync_local,
                       result_directory, result_path)
from distribution import (build_package, build_slot_packages, package_path, slot_package_path,
                          slot_packages_status, PACKAGES)
from server import Handler, LOCK, aggregate, connect, init_db, load_manifest


def source_preview():
    _, report = scan_source(DEFAULT_SOURCE)
    save_report(report)
    rows = []
    metadata = report.get("metadata_file")
    if metadata:
        path = inside(DEFAULT_SOURCE, metadata)
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".jsonl":
            for line in text.splitlines():
                try:
                    if line.strip():
                        rows.append(json.loads(line))
                except ValueError:
                    pass
        else:
            rows = json.loads(text)
    bases = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("source_case_id"), str) and isinstance(row.get("track"), str):
            bases[(row["source_case_id"], row["track"])] = row
    groups = {}
    for path in sorted((DEFAULT_SOURCE / "question_video").rglob("*.mp4")):
        relative = path.relative_to(DEFAULT_SOURCE / "question_video")
        match = VIDEO_NAME.fullmatch(path.name)
        if len(relative.parts) != 3 or not match:
            continue
        key = (match["case"], relative.parts[1], relative.parts[0], match["variant"], match["seed"])
        groups.setdefault(key, {})[match["side"].upper()] = path.relative_to(DEFAULT_SOURCE).as_posix()
    covered = {(key[0], key[1]) for key in groups}
    for key in bases.keys() - covered:
        groups[(*key, "", "", None)] = {}
    methods = {}
    config = DEFAULT_SOURCE / "input_config.json"
    if config.is_file():
        try:
            methods = json.loads(config.read_text(encoding="utf-8-sig")).get("variant_methods", {})
            if not isinstance(methods, dict):
                methods = {}
        except (ValueError, AttributeError):
            pass
    tasks = []
    for key, videos in sorted(groups.items(), key=lambda item: str(item[0])):
        case, track, model, variant, seed = key
        row = bases.get((case, track), {})
        def field(name):
            value = row.get(name)
            return value.get("text", "") if isinstance(value, dict) else ""
        frame = None
        try:
            candidate = inside(DEFAULT_SOURCE / "question_base", row.get("first_frame"))
            if candidate.is_file() and candidate.stat().st_size > 0:
                frame = candidate.relative_to(DEFAULT_SOURCE).as_posix()
        except ValueError:
            pass
        issues = []
        if not row:
            issues.append("缺基础信息")
        if not frame:
            issues.append("警告：未提供初始帧（允许入库）")
        if not videos.get("A"):
            issues.append("缺视频 A")
        if not videos.get("B"):
            issues.append("缺视频 B")
        tasks.append({"id": str(len(tasks)), "source_case_id": case, "track": track,
                      "world_model": model, "stage": variant, "method": methods.get(variant),
                      "generation_seed": seed, "domain": row.get("domain"), "index": row.get("index"),
                      "base_condition": field("base_condition"),
                      "preference_a": field("preference_condition_A"), "preference_b": field("preference_condition_B"),
                      "initial_frame": frame, "video_a": videos.get("A"), "video_b": videos.get("B"),
                      "status": " / ".join(issues) or "素材齐备", "annotation_count": 0})
    return {"report": report, "tasks": tasks}


def batch_tasks(folder):
    with LOCK:
        sync_local(folder)
        with connect(folder / "merged.sqlite3") as db:
            rows = db.execute("""SELECT t.*, COUNT(a.task_id) AS annotation_count FROM tasks t
                LEFT JOIN annotations a ON a.task_id=t.id GROUP BY t.id
                ORDER BY t.source_case_id,t.track,t.private_json""").fetchall()
            tasks = []
            for row in rows:
                task = dict(row)
                task.update(json.loads(task.pop("private_json")))
                # Public database ID is used by both result packages and media routes.
                task["id"] = row["id"]
                task["status"] = "待作答" if not row["annotation_count"] else f"{row['annotation_count']} 份答案"
                task["answers"] = []
                for answer in db.execute("""SELECT a.*, s.x_is_a, r.revision, p.device_id,p.original_id,COALESCE(n.name,'') AS annotator_name,
                    rs.slot AS slot
                    FROM annotations a JOIN assignments s USING(annotator_id,task_id)
                    JOIN annotation_revisions r USING(annotator_id,task_id)
                    JOIN result_sources p USING(annotator_id) LEFT JOIN annotator_profiles n USING(annotator_id)
                    LEFT JOIN result_slots rs USING(annotator_id) WHERE a.task_id=? ORDER BY a.annotator_id""", (row["id"],)):
                    record = dict(answer)
                    record["answers"] = json.loads(record.pop("answers_json"))
                    normalized = {}
                    for actual, display in (("a", "x" if record["x_is_a"] else "y"),
                                            ("b", "y" if record["x_is_a"] else "x")):
                        normalized[actual] = {field: record["answers"].get(f"{display}_{field}")
                                             for field in ("validity", "base_task", "retention", "retention_reasons",
                                                           "retention_note", "preference_a", "preference_b")}
                        normalized[actual]["legacy"] = f"{display}_base_task" not in record["answers"]
                    record["normalized"] = normalized
                    task["answers"].append(record)
                tasks.append(task)
    return tasks


def aggregate_batch(folder):
    with LOCK:
        sync_local(folder)
        aggregate(folder / "merged.sqlite3", folder / "aggregate")
    consensus = [json.loads(line) for line in (folder / "aggregate" / "consensus.jsonl").read_text(encoding="utf-8").splitlines()]
    groups = {}
    for row in consensus:
        private = row["private"]
        key = (private.get("world_model"), row["track"], private.get("stage"), private.get("method"))
        group = groups.setdefault(key, {"model": key[0], "track": key[1], "variant": key[2], "method": key[3],
                                        "tasks": 0, "answers": 0, "scored": 0, "passed": 0, "passed_task_done": 0,
                                        "passed_with_gates": 0, "needs_adjudication": 0})
        group["tasks"] += 1
        group["answers"] += row["annotation_count"]
        group["scored"] += row["strict_sbps"] is not None
        group["passed"] += row["strict_sbps"] is True
        group["passed_task_done"] += row.get("strict_sbps_task_done") is True
        group["passed_with_gates"] += row.get("strict_sbps_with_gates") is True
        group["needs_adjudication"] += row["needs_adjudication"]
    for group in groups.values():
        group["strict_sbps_rate"] = group["passed"] / group["scored"] if group["scored"] else None
        for extra in ("task_done", "with_gates"):
            group[f"strict_sbps_{extra}_rate"] = group[f"passed_{extra}"] / group["scored"] if group["scored"] else None
    result = {"task_count": len(consensus), "answer_count": sum(r["annotation_count"] for r in consensus),
              "needs_adjudication": sum(r["needs_adjudication"] for r in consensus), "groups": list(groups.values())}
    (folder / "aggregate" / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def report_batch(folder, plan_id=None):
    with LOCK:
        sync_local(folder)
        return build_report(folder, plan_id)


def plans_overview(folder):
    out = []
    for summary in list_plans(folder):
        plan = load_plan(folder, summary["plan_id"])
        out.append({**summary, "packages": slot_packages_status(folder, plan),
                    "package_dir": str(slot_package_path(folder, plan["plan_id"], next(iter(plan["slots"]))).parent)})
    return out


class AdminState:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.running = {}
        self.mutex = threading.RLock()
        self.source = None
        self.job = None

    def prepare(self, name, package_only=False):
        folder = batch_path(name)
        if self.job and self.job["status"] == "running":
            raise ValueError("已有题库正在处理，请等待完成")
        job = {"id": secrets.token_hex(16), "batch": name, "status": "running", "percent": 0,
               "message": "准备开始", "kind": "package" if package_only else "prepare",
               "package_path": str(package_path(folder)) if package_only else None}
        self.job = job
        def update(percent, message):
            with self.mutex:
                job.update(percent=round(percent, 1), message=message)
        def run():
            try:
                if package_only:
                    build_package(folder, progress=update)
                    done = "完整分发包已准备完成"
                else:
                    job["report"] = prepare_batch(DEFAULT_SOURCE, folder, progress=update)
                    done = "题库已创建。下一步：设置标注人数，生成各编号的评测包"
                with self.mutex:
                    job.update(status="complete", percent=100, message=done)
            except Exception as exc:
                with self.mutex:
                    job.update(status="failed", message=str(exc))
        threading.Thread(target=run, daemon=True).start()
        return dict(job)

    def package_plan(self, name, slots=None, redundancy=None, seed=None, plan_id=None):
        """Create a balanced plan (or reuse `plan_id`) and build one small package per slot."""
        folder = batch_path(name)
        if self.job and self.job["status"] == "running":
            raise ValueError("已有题库正在处理，请等待完成")
        if plan_id is not None:
            if not isinstance(plan_id, str) or not PLAN_RE.fullmatch(plan_id) or load_plan(folder, plan_id) is None:
                raise ValueError("找不到该分配方案")
        else:
            if type(slots) is not int or type(redundancy) is not int:
                raise ValueError("标注人数与每题份数应为整数")
            if seed is not None and type(seed) is not int:
                raise ValueError("随机种子应为整数，或留空")
            verify_batch(folder)
            plan_id = create_plan(folder, slots, redundancy, seed)["plan_id"]
        job = {"id": secrets.token_hex(16), "batch": name, "status": "running", "percent": 0, "message": "准备开始",
               "kind": "plan", "plan_id": plan_id, "package_path": None,
               "package_dir": str(slot_package_path(folder, plan_id, "A01").parent)}
        self.job = job
        def update(percent, message):
            with self.mutex:
                job.update(percent=round(percent, 1), message=message)
        def run():
            try:
                packages = build_slot_packages(folder, plan_id, progress=update)
                with self.mutex:
                    job.update(status="complete", percent=100, packages=packages,
                               message=f"已生成 {len(packages)} 个评测包")
            except Exception as exc:
                with self.mutex:
                    job.update(status="failed", message=str(exc))
        threading.Thread(target=run, daemon=True).start()
        return dict(job)

    def start(self, name, port, host, target, limit):
        if name in self.running:
            return self.running[name][1]
        if host != "127.0.0.1" or (port is not None and (type(port) is not int or not 1024 <= port <= 65535)):
            raise ValueError("评测仅在本机运行；端口须为 1024–65535")
        if type(target) is not int or not 1 <= target <= 100 or type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("每题份数应为 1–100，每人题数应为 1–10000")
        folder = batch_path(name)
        signatures = verify_batch(folder)
        db = folder / "annotations.sqlite3"
        init_db(db)
        load_manifest(db, folder / "tasks.jsonl")
        export_results(folder, db)
        # Prefer a stable origin so browser sessions survive restarts. The demo
        # uses 8765 and the manager uses 8766; avoid both by default.
        for candidate in ([port] if port is not None else range(8767, 8788)):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                break
            except OSError as exc:
                if port is not None or exc.errno not in {errno.EADDRINUSE, 10048}:
                    raise
        else:
            raise ValueError("本机评测端口均被占用，请关闭之前开启的评测程序后重试")
        port = server.server_port
        server.db_path, server.media_root = db, (folder / "media").resolve()
        server.media_signatures, server.batch_dir = signatures, folder
        server.slot = load_slot(folder)
        server.target_per_task, server.tasks_per_volunteer = target, limit
        info = {"batch": name, "port": port, "host": host, "url": f"http://127.0.0.1:{port}/"}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.running[name] = (server, info, thread)
        return info

    def stop(self, name):
        item = self.running.pop(name, None)
        if item:
            server, _, thread = item
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def local_results(folder):
    info = {"path": str(result_directory(folder)), "answers": 0, "people": 0,
            "exists": False, "current": False, "exported_at": None, "files": []}
    db_path = folder / "annotations.sqlite3"
    if not db_path.is_file():
        return info
    with LOCK:
        init_db(db_path)
        with connect(db_path) as db:
            device = db.execute("SELECT value FROM instance_meta WHERE key='device_id'").fetchone()[0]
            rows = db.execute("""SELECT a.annotator_id,a.task_id,COALESCE(r.revision,1) AS revision,
                COALESCE(p.name,'') AS name FROM annotations a
                LEFT JOIN annotation_revisions r USING(annotator_id,task_id)
                LEFT JOIN annotator_profiles p USING(annotator_id)
                JOIN tasks t ON t.id=a.task_id WHERE t.demo=0""").fetchall()
        people = sorted({r["annotator_id"] for r in rows})
        info.update(answers=len(rows), people=len(people))
        for person in people:
            own = [r for r in rows if r["annotator_id"] == person]
            name = own[0]["name"]
            path = result_path(folder, device, person, name)
            entry = {"name": name or "未填写姓名（历史记录）", "path": str(path), "current": False,
                     "exists": path.is_file(), "answers": len(own)}
            if path.is_file():
                try:
                    package = json.loads(path.read_text(encoding="utf-8"))
                    expected = {(r["annotator_id"],r["task_id"]):r["revision"] for r in own}
                    actual = {(r["annotator_id"],r["task_id"]):r["revision"] for r in package["records"]}
                    entry["current"] = expected == actual and len(actual) == len(package["records"])
                    info["exported_at"] = package["exported_at"]
                except (ValueError, OSError, KeyError, TypeError):
                    pass
            info["files"].append(entry)
        info["exists"] = any(f["exists"] for f in info["files"])
        info["current"] = bool(people) and all(f["current"] for f in info["files"])
    return info


class AdminHandler(Handler):
    def allowed(self, mutation=False):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            self.json_response(403, {"error": "管理界面仅允许本机访问"})
            return False
        if mutation and (self.headers.get("X-Admin-Token") != self.server.state.token or
                         self.headers.get("Origin", f"http://{host}") != f"http://{host}"):
            self.json_response(403, {"error": "请从本机管理页面执行操作"})
            return False
        return True

    def folder(self, name):
        folder = batch_path(name)
        if not (folder / "snapshot.json").is_file():
            raise ValueError("找不到评测题库")
        return folder

    def send_download(self, content, name, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f"attachment; filename=download; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self.allowed():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        name = query.get("batch", [""])[0]
        state = self.server.state
        try:
            if parsed.path in {"/", "/admin.js", "/admin.css"}:
                file = HERE / ("admin.html" if parsed.path == "/" else parsed.path[1:])
                return self.send_file(file, {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css"}[file.suffix])
            if parsed.path == "/api/status":
                batches = []
                for folder in sorted(BATCHES.glob("*")):
                    if not (folder / "snapshot.json").is_file():
                        continue
                    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
                    batches.append({"name": folder.name, "task_count": snapshot["task_count"],
                                    "local_results": local_results(folder),
                                    "plans": plans_overview(folder),
                                    "package_path": str(package_path(folder)), "package_exists": package_path(folder).is_file(),
                                    "created_at": snapshot["created_at"], "running": state.running.get(folder.name, (None, None))[1]})
                return self.json_response(200, {"token": state.token, "batches": batches, "source": str(DEFAULT_SOURCE), "packages_directory": str(PACKAGES), "job": state.job})
            if parsed.path == "/api/job":
                if not state.job or state.job["id"] != query.get("id", [""])[0]:
                    raise ValueError("找不到创建任务，请刷新管理页面")
                return self.json_response(200, dict(state.job))
            if parsed.path == "/api/tasks":
                tasks = batch_tasks(self.folder(name))
                return self.json_response(200, {"tasks": tasks})
            if parsed.path == "/api/media":
                root = self.folder(name) / "media" if name else DEFAULT_SOURCE
                file = inside(root, query.get("path", [""])[0])
                if file.suffix.lower() not in {".mp4", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"} or not file.is_file():
                    return self.send_error(404)
                import mimetypes
                from video_playback import browser_asset
                file = browser_asset(file, HERE / "media_cache")
                return self.send_file(file, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            if parsed.path == "/api/download-results":
                with LOCK:
                    paths = export_results(self.folder(name))
                    if not paths:
                        raise ValueError("本机尚无已提交答案")
                    if len(paths) == 1:
                        return self.send_download(paths[0].read_bytes(), paths[0].name)
                    stream = io.BytesIO()
                    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                        for path in paths:
                            archive.write(path, path.name)
                    return self.send_download(stream.getvalue(), f"results-{name}.zip", "application/zip")
            if parsed.path == "/api/download-aggregate":
                folder = self.folder(name)
                aggregate_batch(folder)
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    for file in sorted((folder / "aggregate").glob("*.json*")):
                        archive.write(file, file.name)
                return self.send_download(stream.getvalue(), f"aggregate-{name}.zip", "application/zip")
            if parsed.path == "/api/report":
                plan = query.get("plan", [""])[0] or None
                if plan is not None and not PLAN_RE.fullmatch(plan):
                    raise ValueError("无效的分配方案编号")
                return self.json_response(200, {"report": report_batch(self.folder(name), plan)})
            if parsed.path == "/api/download-slot":
                folder = self.folder(name)
                plan, slot = query.get("plan", [""])[0], query.get("slot", [""])[0]
                target = slot_package_path(folder, plan, slot)
                if not target.is_file():
                    raise ValueError("该编号的评测包尚未生成")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename=download; filename*=UTF-8''{quote(target.name)}")
                self.end_headers()
                with target.open("rb") as stream:
                    import shutil
                    shutil.copyfileobj(stream, self.wfile)
                return
            if parsed.path == "/api/download-kit":
                folder = self.folder(name)
                target = package_path(folder)
                if not target.is_file():
                    raise ValueError("请先点击‘生成 / 更新分发包’，完成后再下载")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="evaluation-{name}.zip"')
                self.end_headers()
                with target.open("rb") as stream:
                    import shutil
                    shutil.copyfileobj(stream, self.wfile)
                return
            return self.send_error(404)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return self.json_response(400, {"error": str(exc)})

    def do_POST(self):
        if not self.allowed(mutation=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 30 * 1024 * 1024:
                raise ValueError("单次请求应小于 30 MB；请分批导入")
            body = json.loads(self.rfile.read(length))
            state = self.server.state
            path = urlparse(self.path).path
            with state.mutex:
                if path == "/api/check":
                    state.source = source_preview()
                    return self.json_response(200, state.source)
                if path == "/api/prepare":
                    return self.json_response(202, state.prepare(body["batch"]))
                if path == "/api/package":
                    self.folder(body["batch"])
                    return self.json_response(202, state.prepare(body["batch"], package_only=True))
                if path == "/api/plan":
                    self.folder(body["batch"])
                    return self.json_response(202, state.package_plan(
                        body["batch"], body.get("slots"), body.get("redundancy"), body.get("seed"), body.get("plan_id")))
                if path == "/api/seed":
                    folder = self.folder(body["batch"])
                    with LOCK:
                        result = seed_local(folder, body["package"])
                    item = state.running.get(body["batch"])
                    if item:   # let the self-review reach every task
                        item[0].tasks_per_volunteer = max(item[0].tasks_per_volunteer, result["total_tasks"])
                    return self.json_response(200, result)
                if path == "/api/start":
                    info = state.start(body["batch"], body.get("port"), body.get("host", "127.0.0.1"),
                                       body.get("target", 3), body.get("limit", 30))
                    return self.json_response(200, info)
                if path == "/api/stop":
                    state.stop(body["batch"])
                    return self.json_response(200, {"ok": True})
                if path == "/api/import":
                    folder = self.folder(body["batch"])
                    file_name = body.get("file_name") if isinstance(body.get("file_name"), str) else None
                    with LOCK:
                        try:
                            result = import_results(folder, body["package"], file_name, log=True)
                        except ValueError as exc:
                            log_rejection(folder, body.get("package"), file_name, exc)
                            raise
                    return self.json_response(200, result)
                if path == "/api/aggregate":
                    return self.json_response(200, aggregate_batch(self.folder(body["batch"])))
            return self.send_error(404)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            return self.json_response(400, {"error": str(exc)})


def run_admin(port=8766, open_browser=False):
    server = ThreadingHTTPServer(("127.0.0.1", port), AdminHandler)
    server.state = AdminState()
    url = f"http://127.0.0.1:{port}/"
    print(f"Management: {url} (keep this window open)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for name in list(server.state.running):
            server.state.stop(name)
        server.server_close()
