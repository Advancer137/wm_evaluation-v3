"""Balanced, reproducible task plans for independent evaluation computers.

Every evaluation computer gets one *slot* (A01, A02, ...). The organizer creates a plan once
for a frozen batch: each task is given to ``redundancy`` different slots, load is balanced, and
the hidden X/Y order of every (slot, task) pair is fixed in advance. A student package then
contains only that slot's list and videos.
"""
import hashlib
import json
import os
import random
import re
import secrets
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLAN_FORMAT = "pwb-plan-v1"
SLOT_FORMAT = "pwb-slot-v1"
SLOT_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
PLAN_RE = re.compile(r"[0-9a-f]{10}")


def now():
    return datetime.now(timezone.utc).isoformat()


def task_db_id(item):
    """Database id of a manifest row (must stay identical to what load_manifest stores)."""
    stable_key = "|".join(str(item.get(key, "")) for key in
                          ("source_case_id", "track", "world_model", "stage", "generation_seed"))
    return hashlib.sha256(str(item.get("task_id") or stable_key).encode("utf-8")).hexdigest()[:24]


def normalize_name(name):
    return " ".join(unicodedata.normalize("NFKC", name or "").split()).casefold()


def slot_codes(count):
    width = max(2, len(str(count)))
    return [f"A{i:0{width}d}" for i in range(1, count + 1)]


def slot_annotator_id(plan_id, slot):
    """Local session identity of a slot; the same on any computer, so a cleared browser can resume."""
    return hashlib.sha256(f"slot:{plan_id}:{slot}".encode()).hexdigest()[:32]


def merged_slot_id(plan_id, slot):
    """Identity of a slot in the organizer's merged database (the same slot on two PCs merges)."""
    return hashlib.sha256(f"merged-slot:{plan_id}:{slot}".encode()).hexdigest()


def read_manifest(folder):
    folder = Path(folder)
    return [json.loads(line) for line in (folder / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def make_assignments(tasks, slots, redundancy, seed):
    """tasks: [{task_id, source_case_id}] -> {slot: [{task_id, x_is_a}]}, balanced and deterministic."""
    if not isinstance(slots, int) or isinstance(slots, bool) or not 1 <= slots <= 200:
        raise ValueError("标注人数应为 1–200")
    if not isinstance(redundancy, int) or isinstance(redundancy, bool) or not 1 <= redundancy <= slots:
        raise ValueError("每题份数应为 1 到标注人数之间的整数（每份必须由不同的人完成）")
    if not tasks:
        raise ValueError("题库没有题目")
    rng = random.Random(seed)
    codes = slot_codes(slots)
    ordered = sorted(tasks, key=lambda t: t["task_id"])
    rng.shuffle(ordered)
    load = {c: 0 for c in codes}
    seen_case = {c: {} for c in codes}
    owned = {c: [] for c in codes}
    for task in ordered:
        # Fewest tasks first (keeps load within 1); among ties avoid giving one person many
        # variants of the same source case; the random key breaks the remaining ties.
        keyed = sorted(codes, key=lambda c: (load[c], seen_case[c].get(task["source_case_id"], 0), rng.random()))
        chosen = keyed[:redundancy]
        start = rng.randrange(2)
        rng.shuffle(chosen)
        for position, code in enumerate(chosen):
            owned[code].append({"task_id": task["task_id"], "x_is_a": bool((start + position) % 2),
                                "source_case_id": task["source_case_id"]})
            load[code] += 1
            seen_case[code][task["source_case_id"]] = seen_case[code].get(task["source_case_id"], 0) + 1
    plan = {}
    for code in codes:
        pool = owned[code]
        rng.shuffle(pool)
        ordering, previous = [], None
        while pool:
            options = [t for t in pool if t["source_case_id"] != previous] or pool
            pick = rng.choice(options)
            pool.remove(pick)
            ordering.append({"task_id": pick["task_id"], "x_is_a": pick["x_is_a"]})
            previous = pick["source_case_id"]
        plan[code] = ordering
    return plan


def plans_dir(folder):
    return Path(folder) / "plans"


def create_plan(folder, slots, redundancy, seed=None):
    """Create (or return the identical existing) plan for a frozen batch folder."""
    from data_import import sha256
    folder = Path(folder)
    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    if sha256(folder / "tasks.jsonl") != snapshot["manifest_sha256"]:
        raise ValueError("题库题单已被修改，不能生成分配方案")
    if seed is None:
        seed = secrets.randbelow(2 ** 31)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("随机种子应为整数")
    tasks = [{"task_id": task_db_id(item), "source_case_id": item["source_case_id"]}
             for item in read_manifest(folder)]
    ids = [t["task_id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("题单中存在重复题目")
    assignments = make_assignments(tasks, slots, redundancy, seed)
    canonical = json.dumps({"manifest": snapshot["manifest_sha256"], "slots": assignments},
                           sort_keys=True, separators=(",", ":"))
    plan_id = hashlib.sha256(canonical.encode()).hexdigest()[:10]
    target = plans_dir(folder) / f"{plan_id}.json"
    if target.is_file():
        return load_plan(folder, plan_id)
    plan = {"format": PLAN_FORMAT, "plan_id": plan_id, "batch_name": folder.name,
            "manifest_sha256": snapshot["manifest_sha256"], "created_at": now(),
            "slots_count": slots, "redundancy": redundancy, "seed": seed, "slots": assignments}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".plan-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return plan


def load_plan(folder, plan_id):
    """Return the plan dict, or None when no such plan file exists."""
    if not isinstance(plan_id, str) or not PLAN_RE.fullmatch(plan_id):
        raise ValueError("无效的分配方案编号")
    path = plans_dir(folder) / f"{plan_id}.json"
    if not path.is_file():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("format") != PLAN_FORMAT or plan.get("plan_id") != plan_id:
        raise ValueError("分配方案文件无效")
    return plan


def plan_summary(plan):
    sizes = [len(v) for v in plan["slots"].values()]
    return {"plan_id": plan["plan_id"], "created_at": plan["created_at"], "slots": plan["slots_count"],
            "redundancy": plan["redundancy"], "seed": plan["seed"],
            "tasks": len({e["task_id"] for v in plan["slots"].values() for e in v}),
            "per_slot_min": min(sizes), "per_slot_max": max(sizes)}


def list_plans(folder):
    out = []
    for path in plans_dir(folder).glob("*.json") if plans_dir(folder).is_dir() else []:
        try:
            plan = load_plan(folder, path.stem)
        except (ValueError, OSError):
            continue
        if plan:
            out.append(plan_summary(plan))
    return sorted(out, key=lambda p: p["created_at"], reverse=True)


def slot_document(plan, slot):
    if slot not in plan["slots"]:
        raise ValueError("分配方案中没有此编号")
    return {"format": SLOT_FORMAT, "plan_id": plan["plan_id"], "slot": slot,
            "batch_name": plan["batch_name"], "manifest_sha256": plan["manifest_sha256"],
            "slots_count": plan["slots_count"], "redundancy": plan["redundancy"],
            "tasks": plan["slots"][slot]}


def load_slot(folder):
    """The slot a student package belongs to, or None for an ordinary (unassigned) batch."""
    path = Path(folder) / "slot.json"
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if (doc.get("format") != SLOT_FORMAT or not isinstance(doc.get("slot"), str) or not SLOT_RE.fullmatch(doc["slot"])
            or not isinstance(doc.get("plan_id"), str) or not PLAN_RE.fullmatch(doc["plan_id"])):
        raise ValueError("slot.json 无效，请向组织者重新索取评测包")
    tasks = doc.get("tasks")
    if not isinstance(tasks, list) or not tasks or any(
            not isinstance(t, dict) or not isinstance(t.get("task_id"), str) or type(t.get("x_is_a")) is not bool
            for t in tasks):
        raise ValueError("slot.json 的题单无效")
    if len({t["task_id"] for t in tasks}) != len(tasks):
        raise ValueError("slot.json 的题单有重复")
    doc["annotator_id"] = slot_annotator_id(doc["plan_id"], doc["slot"])
    return doc
