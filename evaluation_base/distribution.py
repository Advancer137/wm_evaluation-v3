"""Create portable evaluation packages outside the application directory."""
import json
import os
import uuid
import zipfile
from pathlib import Path

from assignment import PLAN_RE, SLOT_RE, load_plan, read_manifest, slot_document, task_db_id
from data_import import HERE, verify_batch

PACKAGES = HERE.parent / "evaluation_packages"


def package_path(folder):
    return PACKAGES / f"evaluation-{folder.name}.zip"


def build_package(folder, progress=None):
    progress = progress or (lambda percent, message: None)
    progress(0, "校验题库素材")
    verify_batch(folder)
    files = []
    for name in ("server.py", "data_import.py", "result_io.py", "video_playback.py", "distribution.py",
                 "assignment.py", "report.py", "admin.py", "app.js", "index.html", "style.css", "admin.html",
                 "admin.js", "admin.css", "启动管理界面.cmd", "启动评测.cmd", "README.md"):
        files.append((HERE / name, f"evaluation_base/{name}"))
    for file in sorted((HERE / "vendor").rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts:
            files.append((file, f"evaluation_base/vendor/{file.relative_to(HERE / 'vendor').as_posix()}"))
    for name in ("tasks.jsonl", "snapshot.json", "input_check.json", "input_check.md"):
        files.append((folder / name, f"evaluation_base/batches/{folder.name}/{name}"))
    files.extend((p, f"evaluation_base/batches/{folder.name}/media/{p.relative_to(folder / 'media').as_posix()}")
                 for p in sorted((folder / "media").rglob("*")) if p.is_file())
    PACKAGES.mkdir(parents=True, exist_ok=True)
    target = package_path(folder)
    temporary = target.with_name(f".packaging-{uuid.uuid4().hex}.tmp")
    total = sum(p.stat().st_size for p, _ in files)
    done = 0
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
            for path, name in files:
                with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
                    while block := source.read(4 * 1024 * 1024):
                        dest.write(block)
                        done += len(block)
                        progress(100 * done / max(total, 1), f"写入评测包 {done // (1024*1024)} / {total // (1024*1024)} MB")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    progress(100, "评测包已保存")
    return target


# ---------------------------------------------------------------- per-computer (slot) packages
STUDENT_CODE = ("server.py", "data_import.py", "result_io.py", "assignment.py",
                "app.js", "index.html", "style.css")

STUDENT_README = """评测编号：{slot}（共 {count} 题）

使用方法
1. 先把整个文件夹解压到本机硬盘（不要直接在压缩包里运行）。
2. 双击「启动评测.cmd」。浏览器会自动打开评测页面；没有打开时，请手动访问窗口里显示的网址。
   （电脑需已安装 Python 3.10 或更新版本。）
3. 输入你的姓名，勾选规则后开始作答。每提交一道题，答案就自动保存。
4. 中途可以随时关闭窗口或浏览器；再次双击「启动评测.cmd」，输入同样的姓名，就能从上次的位置继续。
5. 全部做完后，把「evaluation_results」文件夹里的 results-*.json 文件发给组织者。
   做完以后如果修改了答案，请重新发送最新的文件。

注意
- 请只在这台电脑、这个解压后的文件夹里作答，不要复制给别人。
- 作答期间请保持黑色的启动窗口打开。
- 请只依据视频中看到的内容作答；看不出来时选择“无法判断”。
"""


def slot_package_path(folder, plan_id, slot):
    if not PLAN_RE.fullmatch(plan_id) or not SLOT_RE.fullmatch(slot):
        raise ValueError("无效的方案或编号")
    return PACKAGES / Path(folder).name / plan_id / f"evaluation-{Path(folder).name}-{slot}.zip"


def build_slot_packages(folder, plan_id, progress=None, verify=True):
    """One small zip per slot: only that slot's playable videos, no admin tools, no other tasks' media."""
    progress = progress or (lambda percent, message: None)
    folder = Path(folder)
    plan = load_plan(folder, plan_id)
    if plan is None:
        raise ValueError("找不到分配方案")
    if verify:
        progress(0, "校验题库素材")
        verify_batch(folder)
    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    by_id = {task_db_id(t): t for t in read_manifest(folder)}
    launcher = (HERE / "启动评测.cmd").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    codes = list(plan["slots"])
    results = []
    for index, slot in enumerate(codes):
        document = slot_document(plan, slot)
        assets = sorted({by_id[e["task_id"]][field] for e in document["tasks"]
                         for field in ("initial_frame", "video_a", "video_b") if by_id[e["task_id"]][field]})
        top = f"评测-{slot}"
        files = [(HERE / name, f"{top}/evaluation_base/{name}") for name in STUDENT_CODE]
        batch_root = f"{top}/evaluation_base/batches/{folder.name}"
        files += [(folder / "tasks.jsonl", f"{batch_root}/tasks.jsonl"),
                  (folder / "snapshot.json", f"{batch_root}/snapshot.json")]
        files += [(folder / "media" / a, f"{batch_root}/media/{a}") for a in assets]
        total = sum(p.stat().st_size for p, _ in files) + 1
        target = slot_package_path(folder, plan_id, slot)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".packaging-{uuid.uuid4().hex}.tmp")
        done = 0
        base = 100 * index / len(codes)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr(f"{top}/启动评测.cmd", launcher)
                archive.writestr(f"{top}/学生说明.txt", STUDENT_README.format(slot=slot, count=len(document["tasks"])).replace("\n", "\r\n"))
                archive.writestr(f"{batch_root}/slot.json", json.dumps(document, ensure_ascii=False, indent=1))
                for path, name in files:
                    with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
                        while block := source.read(4 * 1024 * 1024):
                            dest.write(block)
                            done += len(block)
                            progress(base + 100 / len(codes) * done / total,
                                     f"编号 {slot}（{index + 1}/{len(codes)}）：写入 {done // (1024 * 1024)} / {total // (1024 * 1024)} MB")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        results.append({"slot": slot, "tasks": len(document["tasks"]), "size": target.stat().st_size,
                        "path": str(target)})
    progress(100, "全部评测包已保存")
    return results


def slot_packages_status(folder, plan):
    """Existing package files for a plan (used by the management page)."""
    out = []
    for slot, entries in plan["slots"].items():
        path = slot_package_path(folder, plan["plan_id"], slot)
        out.append({"slot": slot, "tasks": len(entries), "exists": path.is_file(),
                    "size": path.stat().st_size if path.is_file() else 0, "path": str(path)})
    return out
