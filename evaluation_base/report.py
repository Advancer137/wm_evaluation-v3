"""Progress and quality report over the merged results of one batch (organizer side)."""
import json
from collections import Counter, defaultdict
from pathlib import Path

from assignment import list_plans, load_plan, merged_slot_id, normalize_name
from server import connect, init_db

UNDER_LIMIT = 200
SKIP_MIN_VIDEOS = 20      # fewer answered videos: too little to compare
SKIP_MARGIN = 0.25        # flagged when this far above the mean


def build_report(folder, plan_id=None, default_target=3):
    """Who has delivered what, which tasks still lack answers, and what looks suspicious."""
    folder = Path(folder)
    db_path = folder / "merged.sqlite3"
    init_db(db_path)
    plans = list_plans(folder)
    plan = None
    if plan_id:
        plan = load_plan(folder, plan_id)
        if plan is None:
            raise ValueError("找不到该分配方案")
    elif plans:
        plan = load_plan(folder, plans[0]["plan_id"])
    target = plan["redundancy"] if plan else default_target

    with connect(db_path) as db:
        tasks = {r["id"]: dict(r) for r in db.execute(
            "SELECT id, source_case_id, track, domain FROM tasks WHERE demo=0")}
        answers = defaultdict(dict)          # annotator -> task -> (submitted_at, revision)
        skips = defaultdict(lambda: [0, 0])  # annotator -> [videos answered, videos that skip the later questions]
        for row in db.execute("""SELECT a.annotator_id, a.task_id, a.submitted_at, a.answers_json, COALESCE(r.revision,1) AS revision
                                 FROM annotations a LEFT JOIN annotation_revisions r USING(annotator_id,task_id)
                                 JOIN tasks t ON t.id=a.task_id WHERE t.demo=0"""):
            answers[row["annotator_id"]][row["task_id"]] = (row["submitted_at"], row["revision"])
            sheet = json.loads(row["answers_json"])
            if "x_base_task" in sheet:       # sheets of the old question set cannot be compared
                for side in ("x", "y"):
                    skips[row["annotator_id"]][0] += 1
                    skips[row["annotator_id"]][1] += (sheet[f"{side}_validity"] != "valid" or sheet[f"{side}_base_task"] == "no")
        names = {r["annotator_id"]: r["name"] for r in db.execute("SELECT annotator_id, name FROM annotator_profiles")}
        sources = {r["annotator_id"]: (r["device_id"], r["original_id"]) for r in db.execute("SELECT * FROM result_sources")}
        slot_of = {r["annotator_id"]: (r["plan_id"], r["slot"]) for r in db.execute("SELECT * FROM result_slots")}
        history = [dict(r) for r in db.execute("SELECT * FROM import_log ORDER BY id DESC LIMIT 200")]

    for entry in history:
        entry["warnings"] = json.loads(entry.pop("warnings_json") or "[]")
    ok_logs = [h for h in history if h["status"] == "ok"]

    slots, warnings, plan_slot_ids = [], [], {}
    task_slots = defaultdict(list)           # task -> planned slot codes
    if plan:
        for code, entries in plan["slots"].items():
            for e in entries:
                task_slots[e["task_id"]].append(code)
        for code, entries in plan["slots"].items():
            mid = merged_slot_id(plan["plan_id"], code)
            plan_slot_ids[mid] = code
            planned = [e["task_id"] for e in entries]
            mine = answers.get(mid, {})
            done = sum(t in mine for t in planned)
            logs = [h for h in ok_logs if h["plan_id"] == plan["plan_id"] and h["slot"] == code]
            devices = sorted({h["device_id"] for h in logs if h["device_id"]})
            name = names.get(mid) or next((h["annotator_name"] for h in logs if h["annotator_name"]), "")
            received = mid in slot_of or bool(mine)
            status = ("未收到" if not received else "已完成" if done == len(planned) else "进行中")
            slots.append({"slot": code, "name": name, "planned": len(planned), "done": done, "status": status,
                          "last_submitted": max((v[0] for v in mine.values()), default=None),
                          "last_imported": logs[0]["imported_at"] if logs else None,
                          "devices": devices, "edits": sum(v[1] > 2 for v in mine.values())})
        missing = [s["slot"] for s in slots if s["status"] == "未收到"]
        partial = [f"{s['slot']}（{s['done']}/{s['planned']}）" for s in slots if s["status"] == "进行中"]
        if missing:
            warnings.append({"level": "warn", "text": "尚未收到结果：" + "、".join(missing)})
        if partial:
            warnings.append({"level": "warn", "text": "尚未做完（可能还在作答，或结果文件不是最新）：" + "、".join(partial)})
        by_name = defaultdict(list)
        for s in slots:
            if s["name"]:
                by_name[normalize_name(s["name"])].append(s)
        for group in by_name.values():
            if len(group) > 1:
                warnings.append({"level": "warn", "text": f"姓名「{group[0]['name']}」出现在多个编号：" +
                                 "、".join(s["slot"] for s in group) + "。请确认是否同一个人领取了多份评测包（会重复贡献同一批题目的票数）。"})
        for s in slots:
            if len(s["devices"]) > 1:
                warnings.append({"level": "info", "text": f"编号 {s['slot']} 的结果来自 {len(s['devices'])} 台不同电脑："
                                 "评测包可能被复制使用；同一编号的答案已按版本合并。"})
            if s["status"] != "未收到" and not s["name"]:
                warnings.append({"level": "info", "text": f"编号 {s['slot']} 没有姓名记录"})

    # Annotators that are not part of the active plan (direct answers on the organizer PC, other plans).
    others = []
    for ann, mine in answers.items():
        if ann in plan_slot_ids:
            continue
        info = slot_of.get(ann)
        others.append({"name": names.get(ann, ""), "answers": len(mine),
                       "slot": info[1] if info else None, "plan_id": info[0] if info else None})
    if plan and others:
        other_plans = sorted({o["plan_id"] for o in others if o["plan_id"]})
        direct = [o for o in others if not o["plan_id"]]
        if other_plans:
            warnings.append({"level": "info", "text": "还有来自其他分配方案的结果：" + "、".join(other_plans)})
        if direct:
            warnings.append({"level": "info", "text": f"有 {len(direct)} 位作答者不属于任何编号（例如直接在本机作答），"
                             "他们的答案计入题目覆盖，但没有对应的分配清单。"})

    # Someone who picks "invalid" / "base task not done" (which skip the later questions) far more
    # often than the others may be hurrying: worth a spot check.
    rates = {ann: n[1] / n[0] for ann, n in skips.items() if n[0] >= SKIP_MIN_VIDEOS}
    if len(rates) >= 3:
        mean = sum(rates.values()) / len(rates)
        for ann, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
            if rate >= mean + SKIP_MARGIN and rate >= 0.5:
                label = next((c for m, c in plan_slot_ids.items() if m == ann), None) or names.get(ann) or ann[:8]
                warnings.append({"level": "info", "text": f"{label}：{rate:.0%} 的视频选了“无效 / 基础任务未完成”"
                                 f"（所有人平均 {mean:.0%}），这两项会跳过后面的问题，建议抽查其答案。"})

    rejected = [h for h in history if h["status"] == "rejected"]
    latest_ok = {(h["file_name"], h["slot"]) for h in ok_logs}
    pending = [h for h in rejected if (h["file_name"], h["slot"]) not in latest_ok]
    if pending:
        warnings.append({"level": "error", "text": f"有 {len(pending)} 次导入被拒绝（详见导入记录），对应文件的答案没有计入。"})

    # Coverage: how many answers each task has versus the target.
    have = Counter()
    for mine in answers.values():
        for t in mine:
            if t in tasks:
                have[t] += 1
    by_count = Counter(have.get(t, 0) for t in tasks)
    under = []
    for t, info in sorted(tasks.items(), key=lambda kv: (have.get(kv[0], 0), kv[1]["source_case_id"], kv[1]["track"])):
        n = have.get(t, 0)
        if n >= target:
            continue
        missing_slots = [c for c in task_slots.get(t, [])
                         if t not in answers.get(merged_slot_id(plan["plan_id"], c), {})] if plan else []
        under.append({"task_id": t, "source_case_id": info["source_case_id"], "track": info["track"],
                      "domain": info["domain"], "have": n, "need": target, "missing_slots": missing_slots})
    meeting = sum(1 for t in tasks if have.get(t, 0) >= target)
    summary = {"tasks": len(tasks), "tasks_meeting_target": meeting, "answers": sum(have.values()),
               "slots": len(slots), "slots_received": sum(s["status"] != "未收到" for s in slots),
               "slots_complete": sum(s["status"] == "已完成" for s in slots), "target": target}
    return {"plan": ({"plan_id": plan["plan_id"], "slots": plan["slots_count"], "redundancy": plan["redundancy"],
                      "created_at": plan["created_at"]} if plan else None),
            "plans": plans, "summary": summary, "slots": slots, "others": others,
            "coverage": {"by_count": {str(k): by_count[k] for k in sorted(by_count)}, "under_total": len(under),
                         "under": under[:UNDER_LIMIT]},
            "warnings": warnings, "imports": history[:100]}
