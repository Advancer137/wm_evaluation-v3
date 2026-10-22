"""Portable result snapshots with revision-aware, idempotent merging."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from assignment import PLAN_RE, SLOT_RE, load_plan, merged_slot_id, read_manifest, task_db_id
from data_import import sha256, HERE
from server import connect, init_db, load_manifest, now, validate_answers


RESULTS = HERE.parent / "evaluation_results"
RESULT_FORMAT = "pwb-results-v2"            # v2: base-task / consistency questions
ACCEPTED_FORMATS = ("pwb-results-v1", RESULT_FORMAT)

def result_directory(batch):
    return RESULTS / Path(batch).name

def slot_info(batch):
    """{"plan_id", "slot"} when this batch folder is a per-computer package, else None."""
    path = Path(batch) / "slot.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(doc.get("slot"), str) and isinstance(doc.get("plan_id"), str):
        return {"plan_id": doc["plan_id"], "slot": doc["slot"]}
    return None


def result_path(batch, device, person, name):
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "unnamed").strip(" .")[:40] or "unnamed"
    identity = hashlib.sha256(person.encode()).hexdigest()[:12]
    slot = slot_info(batch)
    prefix = f"{slot['slot']}-" if slot else ""
    return result_directory(batch) / f"results-{prefix}{label}-{device}-{identity}.json"

def fingerprint(batch):
    batch = Path(batch)
    snapshot = json.loads((batch / "snapshot.json").read_text(encoding="utf-8"))
    if sha256(batch / "tasks.jsonl") != snapshot["manifest_sha256"]:
        raise ValueError("题单已修改，不能导出或合并")
    identity = {"manifest": snapshot["manifest_sha256"], "assets": snapshot["assets"]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


CONTENT_FIELDS = ("source_case_id", "track", "world_model", "stage", "generation_seed",
                  "base_condition", "preference_a", "preference_b")


def task_identities(batch):
    """{task_id: {"fp", "frame", ...}} — a per-task content fingerprint that survives batch changes.

    The fingerprint covers what the answer is about: case / track / model / version / seed, the
    English condition texts and the SHA-256 of the *original* A/B videos (playback copies and
    reference translations are deliberately excluded). The initial frame is tracked separately.
    """
    batch = Path(batch)
    assets = json.loads((batch / "snapshot.json").read_text(encoding="utf-8"))["assets"]
    def digest(relative):
        return assets.get(relative, {}).get("sha256") if relative else None
    out = {}
    for item in read_manifest(batch):
        content = {k: item.get(k) for k in CONTENT_FIELDS}
        content["video_a"] = digest(item.get("original_video_a") or item.get("video_a"))
        content["video_b"] = digest(item.get("original_video_b") or item.get("video_b"))
        fp = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
        out[task_db_id(item)] = {"fp": fp, "frame": digest(item.get("initial_frame")),
                                 "source_case_id": item.get("source_case_id"), "track": item.get("track")}
    return out


def find_source_batch(batch, batch_fingerprint):
    """Locate the (old) batch folder a result file was exported from, among sibling batches."""
    batch = Path(batch)
    for folder in sorted(batch.parent.iterdir()) if batch.parent.is_dir() else []:
        if folder == batch or not (folder / "snapshot.json").is_file():
            continue
        try:
            if fingerprint(folder) == batch_fingerprint:
                return folder
        except (ValueError, OSError, KeyError):
            continue
    return None


def export_results(batch, db_path=None, annotator_id=None):
    batch = Path(batch)
    db_path = Path(db_path or batch / "annotations.sqlite3")
    if not db_path.is_file():
        raise ValueError("本机尚无作答数据库，请先开启此题库")
    init_db(db_path)
    with connect(db_path) as db:
        device = db.execute("SELECT value FROM instance_meta WHERE key='device_id'").fetchone()[0]
        records = []
        names = {r["annotator_id"]: r["name"] for r in db.execute("SELECT * FROM annotator_profiles")}
        for row in db.execute("""SELECT a.*, s.x_is_a, COALESCE(r.revision,1) AS revision
            FROM annotations a JOIN assignments s USING(annotator_id,task_id)
            LEFT JOIN annotation_revisions r USING(annotator_id,task_id)
            JOIN tasks t ON t.id=a.task_id WHERE t.demo=0 ORDER BY a.annotator_id,a.task_id"""):
            if annotator_id is not None and row["annotator_id"] != annotator_id:
                continue
            records.append({"annotator_id": row["annotator_id"], "annotator_name": names.get(row["annotator_id"], ""), "task_id": row["task_id"],
                            "x_is_a": bool(row["x_is_a"]), "answers": json.loads(row["answers_json"]),
                            "revision": row["revision"], "submitted_at": row["submitted_at"]})
    identities = task_identities(batch)
    for record in records:
        info = identities.get(record["task_id"])
        if info:
            record.update(task_fingerprint=info["fp"], frame_sha256=info["frame"])
    folder = result_directory(batch)
    folder.mkdir(parents=True, exist_ok=True)
    people = sorted({r["annotator_id"] for r in records})
    paths = []
    batch_fingerprint = fingerprint(batch)
    slot = slot_info(batch)
    for person in people:
        package = {"format": RESULT_FORMAT, "batch_fingerprint": batch_fingerprint,
                   "batch_name": batch.name, "device_id": device, "exported_at": now(),
                   "annotator_name": names.get(person, ""),
                   "records": [r for r in records if r["annotator_id"] == person]}
        if slot:
            package.update(slot=slot["slot"], plan_id=slot["plan_id"])
        target = result_path(batch, device, person, names.get(person, ""))
        temporary = folder / f".export-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        paths.append(target)
    return paths


def import_results(batch, package, file_name=None, log=False):
    """Validate a complete file before committing; never trust uploaded paths or task text.

    Returns counts plus slot / plan / warning details. With ``log=True`` the import is also
    written to the import history shown in the progress report.
    """
    batch = Path(batch)
    if not isinstance(package, dict) or package.get("format") not in ACCEPTED_FORMATS:
        raise ValueError("不是本系统导出的评测结果文件")
    if not isinstance(package.get("batch_fingerprint"), str):
        raise ValueError("结果文件缺少题库指纹")
    cross = package["batch_fingerprint"] != fingerprint(batch)
    source, here, by_fp, there = None, {}, {}, {}
    if cross:
        # Results of another (older / newer) batch: keep only tasks whose content is identical.
        here = task_identities(batch)
        by_fp = {info["fp"]: task for task, info in here.items()}
        source = find_source_batch(batch, package["batch_fingerprint"])
        there = task_identities(source) if source else {}
    device = package.get("device_id")
    if not isinstance(device, str) or len(device) != 32 or any(c not in "0123456789abcdef" for c in device):
        raise ValueError("无效的设备标识")
    records = package.get("records")
    if not isinstance(records, list) or len(records) > 100000:
        raise ValueError("结果记录格式或数量无效")
    slot, plan_id = package.get("slot"), package.get("plan_id")
    plan, allowed, warnings = None, None, []
    if slot is not None or plan_id is not None:
        if not (isinstance(slot, str) and SLOT_RE.fullmatch(slot) and isinstance(plan_id, str) and PLAN_RE.fullmatch(plan_id)):
            raise ValueError("结果文件中的评测编号无效")
        plan = load_plan(batch, plan_id) or (load_plan(source, plan_id) if source else None)
        if plan is None:
            warnings.append(f"本机没有分配方案 {plan_id}，无法核对编号 {slot} 的题单（已按文件内容导入）")
        elif slot not in plan["slots"]:
            raise ValueError(f"分配方案 {plan_id} 中没有编号 {slot}")
        else:
            allowed = {e["task_id"]: bool(e["x_is_a"]) for e in plan["slots"][slot]}
    db_path = batch / "merged.sqlite3"
    init_db(db_path)
    load_manifest(db_path, batch / "tasks.jsonl")
    count = {"added": 0, "updated": 0, "unchanged": 0, "older_ignored": 0}
    skipped = {"removed": [], "changed": []}
    frame_changed = 0
    seen = set()
    with connect(db_path) as db:
        valid_tasks = {r[0] for r in db.execute("SELECT id FROM tasks")}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("每条结果必须是对象")
            person, task = record.get("annotator_id"), record.get("task_id")
            if not isinstance(person, str) or not 1 <= len(person) <= 128 or not isinstance(task, str) or \
                    (not cross and task not in valid_tasks):
                raise ValueError("结果包含无效作答者或未知题目")
            if allowed is not None:
                if task not in allowed:
                    raise ValueError(f"题目 {task} 不在编号 {slot} 的分配清单中；整份文件未导入")
                if type(record.get("x_is_a")) is bool and allowed[task] != record["x_is_a"]:
                    raise ValueError(f"题目 {task} 的 X/Y 映射与分配方案不一致；整份文件未导入")
            if cross:
                fp = record.get("task_fingerprint")
                frame = record.get("frame_sha256")
                if not isinstance(fp, str):
                    if task not in there:
                        raise ValueError(f"这是旧格式的结果文件（题库 {package.get('batch_name') or '未知'}），"
                                         "需要它对应的旧题库来逐题核对：请把旧题库文件夹放回 batches/ 后再导入")
                    fp, frame = there[task]["fp"], there[task]["frame"]
                target = by_fp.get(fp)
                if target is None:
                    skipped["changed" if task in here else "removed"].append(task)
                    continue
                if frame != here[target]["frame"]:
                    frame_changed += 1
                task = target
            name = record.get("annotator_name", "")
            if not isinstance(name, str) or len(name) > 80:
                raise ValueError("无效的作答者姓名")
            key = (person, task)
            if key in seen:
                raise ValueError("同一文件中出现重复的作答者/题目")
            seen.add(key)
            revision = record.get("revision")
            if type(revision) is not int or not 1 <= revision <= 2147483647:
                raise ValueError("无效的答案版本")
            if type(record.get("x_is_a")) is not bool:
                raise ValueError("缺少有效的 X/Y 映射")
            submitted = record.get("submitted_at")
            try:
                timestamp = datetime.fromisoformat(submitted)
                if timestamp.tzinfo is None:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError("无效的提交时间")
            answers = validate_answers(record.get("answers"), allow_legacy=True)
            merged_person = (merged_slot_id(plan_id, slot) if slot else
                             hashlib.sha256(f"{device}:{person}".encode()).hexdigest())
            old = db.execute("""SELECT a.*, s.x_is_a, r.revision FROM annotations a
                JOIN assignments s USING(annotator_id,task_id)
                JOIN annotation_revisions r USING(annotator_id,task_id)
                WHERE a.annotator_id=? AND a.task_id=?""", (merged_person, task)).fetchone()
            if old:
                if bool(old["x_is_a"]) != record["x_is_a"]:
                    raise ValueError("同一作答的 X/Y 映射冲突；整份文件未导入")
                if old["revision"] > revision:
                    count["older_ignored"] += 1
                    continue
                if old["revision"] == revision:
                    if json.loads(old["answers_json"]) != answers or old["submitted_at"] != submitted:
                        raise ValueError("同一版本有不同答案；整份文件未导入")
                    count["unchanged"] += 1
                    continue
            db.execute("INSERT OR IGNORE INTO annotators VALUES (?,?)", (merged_person, submitted))
            if name:
                db.execute("INSERT INTO annotator_profiles VALUES (?,?) ON CONFLICT(annotator_id) DO UPDATE SET name=excluded.name", (merged_person, name))
            db.execute("INSERT OR IGNORE INTO result_sources VALUES (?,?,?)", (merged_person, device, person))
            db.execute("INSERT OR IGNORE INTO assignments VALUES (?,?,?,?)",
                       (merged_person, task, int(record["x_is_a"]), submitted))
            db.execute("INSERT INTO annotations VALUES (?,?,?,?) ON CONFLICT(annotator_id,task_id) DO UPDATE SET answers_json=excluded.answers_json,submitted_at=excluded.submitted_at",
                       (merged_person, task, json.dumps(answers), submitted))
            db.execute("INSERT INTO annotation_revisions VALUES (?,?,?) ON CONFLICT(annotator_id,task_id) DO UPDATE SET revision=excluded.revision",
                       (merged_person, task, revision))
            if slot:
                db.execute("INSERT OR REPLACE INTO result_slots VALUES (?,?,?)", (merged_person, plan_id, slot))
            count["updated" if old else "added"] += 1
        if cross:
            label = source.name if source else (package.get("batch_name") or "其他题库")
            warnings.append(f"来自其他题库 {label}：按题目内容核对，计入 {sum(count.values())} 条；"
                            f"新题库已删除的题 {len(skipped['removed'])} 条、内容已变的题 {len(skipped['changed'])} 条，未计入")
            if frame_changed:
                warnings.append(f"其中 {frame_changed} 条的初始帧与作答时不同（例如后来补充了初始帧），仍已计入")
        if log:
            db.execute("""INSERT INTO import_log (imported_at,file_name,plan_id,slot,annotator_name,device_id,status,
                          added,updated,unchanged,older_ignored,error,warnings_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (now(), (file_name or "")[:200], plan_id, slot, str(package.get("annotator_name") or "")[:80],
                        device, "ok", count["added"], count["updated"], count["unchanged"], count["older_ignored"],
                        None, json.dumps(warnings, ensure_ascii=False)))
    return {**count, "slot": slot, "plan_id": plan_id, "device_id": device,
            "annotator_name": str(package.get("annotator_name") or ""), "records": len(records),
            "planned": len(allowed) if allowed is not None else None, "warnings": warnings,
            "cross_batch": cross, "source_batch": source.name if source else (package.get("batch_name") if cross else None),
            "skipped_removed": len(skipped["removed"]), "skipped_changed": len(skipped["changed"]),
            "frame_changed": frame_changed}


def seed_local(batch, package):
    """Start a new local annotator from an existing result file (to review / extend it).

    Records are matched to this batch by task content (same rules as cross-batch import);
    the matched answers become this annotator's saved answers, and everything else
    (tasks not in the file) is answered normally afterwards. Returns the session token.
    """
    batch = Path(batch)
    if not isinstance(package, dict) or package.get("format") not in ACCEPTED_FORMATS:
        raise ValueError("不是本系统导出的评测结果文件")
    records = package.get("records")
    if not isinstance(records, list) or not records or len(records) > 100000:
        raise ValueError("结果文件中没有作答记录")
    same = package.get("batch_fingerprint") == fingerprint(batch)
    here = task_identities(batch)
    by_fp = {info["fp"]: task for task, info in here.items()}
    source = None if same else find_source_batch(batch, package.get("batch_fingerprint"))
    there = task_identities(source) if source else {}
    chosen, skipped = {}, {"removed": 0, "changed": 0}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str):
            raise ValueError("结果记录格式无效")
        if type(record.get("x_is_a")) is not bool:
            raise ValueError("缺少有效的 X/Y 映射")
        answers = validate_answers(record.get("answers"), allow_legacy=True)
        revision = record.get("revision") if type(record.get("revision")) is int and record["revision"] >= 1 else 2
        task = record["task_id"]
        if not same:
            fp = record.get("task_fingerprint")
            if not isinstance(fp, str):
                if task not in there:
                    raise ValueError("这是旧格式的结果文件，需要它对应的旧题库文件夹仍在 batches/ 下")
                fp = there[task]["fp"]
            target = by_fp.get(fp)
            if target is None:
                skipped["changed" if task in here else "removed"] += 1
                continue
            task = target
        elif task not in here:
            raise ValueError("结果包含未知题目")
        if task not in chosen or chosen[task][2] < revision:
            chosen[task] = (bool(record["x_is_a"]), answers, revision, record.get("submitted_at"))
    if not chosen:
        raise ValueError("结果文件中没有与本题库相同的题目")
    name = str(package.get("annotator_name") or "").strip()[:80] or "导入的作答"
    db_path = batch / "annotations.sqlite3"
    init_db(db_path)
    load_manifest(db_path, batch / "tasks.jsonl")
    token = uuid.uuid4().hex
    with connect(db_path) as db:
        db.execute("INSERT INTO annotators VALUES (?,?)", (token, now()))
        db.execute("INSERT INTO annotator_profiles VALUES (?,?)", (token, name))
        for order, (task, (x_is_a, answers, revision, submitted)) in enumerate(chosen.items()):
            stamp = now()
            db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (token, task, int(x_is_a), stamp))
            db.execute("INSERT INTO annotations VALUES (?,?,?,?)",
                       (token, task, json.dumps(answers, ensure_ascii=False),
                        submitted if isinstance(submitted, str) else stamp))
            db.execute("INSERT INTO annotation_revisions VALUES (?,?,?)", (token, task, revision))
    return {"token": token, "name": name, "seeded": len(chosen), "total_tasks": len(here),
            "skipped_removed": skipped["removed"], "skipped_changed": skipped["changed"]}


def log_rejection(batch, package, file_name, error):
    """Remember a refused file so the progress report can list it (never raises)."""
    try:
        batch = Path(batch)
        db_path = batch / "merged.sqlite3"
        init_db(db_path)
        pkg = package if isinstance(package, dict) else {}
        text = lambda value, limit: value[:limit] if isinstance(value, str) else None
        with connect(db_path) as db:
            db.execute("""INSERT INTO import_log (imported_at,file_name,plan_id,slot,annotator_name,device_id,status,
                          added,updated,unchanged,older_ignored,error,warnings_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (now(), (file_name or "")[:200], text(pkg.get("plan_id"), 20), text(pkg.get("slot"), 40),
                        text(pkg.get("annotator_name"), 80), text(pkg.get("device_id"), 40), "rejected",
                        0, 0, 0, 0, str(error)[:500], "[]"))
    except Exception:
        pass


def sync_local(batch):
    batch = Path(batch)
    if (batch / "annotations.sqlite3").is_file():
        paths = export_results(batch)
        for path in paths:
            import_results(batch, json.loads(path.read_text(encoding="utf-8")))
        if not paths:
            db = batch / "merged.sqlite3"
            init_db(db)
            load_manifest(db, batch / "tasks.jsonl")
    else:
        db = batch / "merged.sqlite3"
        init_db(db)
        load_manifest(db, batch / "tasks.jsonl")
