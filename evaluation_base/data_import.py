"""Validate data_source and create self-contained, frozen evaluation batches."""

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE.parent / "data_source"
BATCHES = HERE / "batches"
REPORTS = HERE / "reports"
VIDEO_NAME = re.compile(r"(?P<case>[^_]+(?:_[^_]+)*)__(?P<side>[AB])__(?P<variant>[^_]+(?:_[^_]+)*?)(?:__seed_(?P<seed>\d+))?\.mp4", re.IGNORECASE)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def inside(root, relative):
    if not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute():
        raise ValueError("必须使用非空相对路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("路径超出数据目录")
    return path


TRANSLATION_FILE = "translations_zh.tsv"
TRANSLATED_FIELDS = ("base_condition", "preference_a", "preference_b")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _norm_text(text):
    return " ".join(str(text).split())


def load_translations(base, needed):
    """Optional reference translations (question_base/translations_zh.tsv: English<TAB>Chinese).

    Returns (mapping or None, summary, problems). The feature is switched off (mapping None)
    when the file is absent, unreadable, mostly malformed, or covers none of the needed texts.
    Individual bad lines are dropped and reported; they never block a batch.
    """
    path = Path(base) / TRANSLATION_FILE
    summary = {"status": "missing", "file": TRANSLATION_FILE, "entries": 0,
               "needed_texts": len(needed), "covered_texts": 0}
    problems = []
    if not path.is_file():
        return None, summary, problems
    def problem(code, message, examples=None):
        problems.append({"code": code, "message": message, "examples": (examples or [])[:5],
                         "count": len(examples or [])})
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        summary["status"] = "disabled"
        problem("translation_unreadable", f"参考译文文件无法以 UTF-8 读取，已关闭参考译文：{exc}")
        return None, summary, problems
    entries, malformed, empty, no_cjk, digits, conflicts, total = {}, [], [], [], [], [], 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if number == 1 and parts[0].strip().lower() in ("source", "english", "en", "原文"):
            continue
        total += 1
        if len(parts) != 2 or not parts[0].strip():
            malformed.append(f"第 {number} 行")
            continue
        english, chinese = _norm_text(parts[0]), parts[1].strip()
        if not chinese:
            empty.append(english[:60])
            continue
        if not _CJK.search(chinese):
            no_cjk.append(english[:60])
            continue
        if set(re.findall(r"\d+", english)) - set(re.findall(r"\d+", chinese)):
            digits.append(english[:60])
        if english in entries and entries[english] != chinese:
            conflicts.append(english[:60])
            continue
        entries[english] = chinese
    if malformed:
        problem("translation_malformed", "参考译文中有格式不对的行（应为“英文原文<Tab>中文译文”两列），已忽略", malformed)
    if empty:
        problem("translation_empty", "参考译文为空，这些句子不显示译文", empty)
    if no_cjk:
        problem("translation_not_chinese", "参考译文不含中文，可能未翻译，这些句子不显示译文", no_cjk)
    if digits:
        problem("translation_number_mismatch", "原文中的数字未出现在译文里，请人工核对（仍会显示）", digits)
    if conflicts:
        problem("translation_conflict", "同一英文句子有不同译文，已采用第一条", conflicts)
    covered = {t for t in needed if t in entries}
    missing = sorted(set(needed) - covered)
    if missing:
        problem("translation_missing", f"{len(missing)} 条题目文本没有参考译文，这些句子不显示译文", missing)
    summary.update(entries=len(entries), covered_texts=len(covered))
    if total and len(malformed) > total / 2:
        summary["status"] = "disabled"
        problem("translation_disabled", "参考译文文件大部分行格式不正确，已关闭参考译文功能")
        return None, summary, problems
    if not covered:
        summary["status"] = "disabled"
        problem("translation_disabled", "参考译文与题目文本没有任何匹配，已关闭参考译文功能")
        return None, summary, problems
    summary["status"] = "enabled"
    return entries, summary, problems


def batch_path(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("题库名请使用 1–64 个英文字母、数字、下划线或短横线")
    return BATCHES / name


def scan_source(source=DEFAULT_SOURCE, metadata=None):
    """Return eligible normalized tasks and a detailed report; never mutate inputs."""
    source = Path(source).resolve()
    base = source / "question_base"
    video_root = source / "question_video"
    issues, tasks = [], []
    report = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "source_root": str(source), "issues": issues, "summary": {}, "issue_labels": ISSUE_LABELS}

    def issue(code, message, fatal=False, severity="error", **context):
        issues.append({"code": code, "message": message, "fatal": fatal, "severity": severity, **context})

    for folder in (base, video_root):
        if not folder.is_dir():
            issue("missing_directory", "缺少输入目录", fatal=True, path=str(folder))
    if not (base / "images").is_dir():
        issue("missing_images_directory", "未提供初始帧目录；题目仍可进入评测题库", severity="warning",
              path=str(base / "images"))
    # Optional explicit method mapping; policy suffixes alone do not prove AlignX provenance.
    methods = {}
    config_path = source / "input_config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            methods = config.get("variant_methods", {})
            if not isinstance(methods, dict) or any(
                    not isinstance(k, str) or v not in ("raw", "alignx", None) for k, v in methods.items()):
                raise ValueError("variant_methods 的值只允许 raw、alignx 或 null")
        except (ValueError, OSError, AttributeError) as exc:
            issue("invalid_config", str(exc), fatal=True)
            methods = {}
    metadata_path = None
    try:
        if metadata:
            metadata_path = inside(base, metadata)
        else:
            candidates = sorted(p for p in base.glob("*") if p.suffix.lower() in (".json", ".jsonl"))
            if len(candidates) != 1:
                raise ValueError("question_base 应有且仅有一个 JSON/JSONL 文件；多个文件时用 --metadata 指定文件名")
            metadata_path = candidates[0]
        contents = metadata_path.read_text(encoding="utf-8-sig")
        if metadata_path.suffix.lower() == ".jsonl":
            rows = []
            for line_number, line in enumerate(contents.splitlines(), 1):
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError as exc:
                        issue("invalid_json", str(exc), fatal=True, line=line_number)
        else:
            rows = json.loads(contents)
            if not isinstance(rows, list):
                raise ValueError("JSON 文件顶层必须是数组；JSONL 文件每行一个对象")
        report["metadata_file"] = metadata_path.relative_to(source).as_posix()
        report["metadata_sha256"] = sha256(metadata_path)
    except (ValueError, OSError) as exc:
        issue("metadata_error", str(exc), fatal=True)
        rows = []

    by_key, duplicates, frames, invalid_frames = {}, set(), {}, set()
    missing_frame_rows = 0
    for number, row in enumerate(rows, 1):
        key = None
        try:
            if not isinstance(row, dict):
                raise ValueError("元数据记录必须是对象")
            for field in ("source_case_id", "track"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise ValueError(f"缺少非空字符串字段 {field}")
            key = (row["source_case_id"], row["track"])
            if row.get("domain") is not None and not isinstance(row["domain"], str):
                raise ValueError("domain 必须为字符串或 null")
            if row.get("index") is not None and type(row["index"]) not in (str, int):
                raise ValueError("index 必须为字符串、整数或 null")
            if key in by_key:
                duplicates.add(key)
                issue("duplicate_metadata", "source_case_id + track 重复", fatal=True, key=list(key))
                continue
            normalized = {}
            for target, field in (("base_condition", "base_condition"),
                                  ("preference_a", "preference_condition_A"),
                                  ("preference_b", "preference_condition_B")):
                text = row.get(field, {}).get("text") if isinstance(row.get(field), dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"缺少非空文本 {field}.text")
                normalized[target] = text
            by_key[key] = {**normalized, "source_case_id": key[0], "track": key[1],
                           "domain": row.get("domain"), "index": row.get("index")}
            frame_value = row.get("first_frame")
            if frame_value is None or (isinstance(frame_value, str) and not frame_value.strip()):
                missing_frame_rows += 1
                issue("missing_frame", "未提供初始帧；题目仍可进入评测题库", severity="warning", key=list(key))
                continue
            frame = inside(base, frame_value)
            if not frame.is_relative_to((base / "images").resolve()):
                raise ValueError("first_frame 必须指向 question_base/images 下的图片")
            if not frame.is_file() or frame.stat().st_size == 0:
                missing_frame_rows += 1
                issue("missing_frame", "初始帧缺失或文件为空；题目仍可进入评测题库", severity="warning", key=list(key), path=str(frame))
            elif frame.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                issue("invalid_frame_format", "首帧应为 jpg/png/webp/gif 图片", key=list(key), path=str(frame))
                invalid_frames.add(key)
            else:
                frames[key] = frame.relative_to(source).as_posix()
        except (ValueError, OSError) as exc:
            issue("invalid_metadata", str(exc), fatal=True, record=number)
            if key is not None:
                invalid_frames.add(key)

    needed_texts = {_norm_text(r[f]) for r in by_key.values() for f in TRANSLATED_FIELDS}
    translations, translation_summary, translation_problems = load_translations(base, needed_texts)
    for item in translation_problems:
        issue(item["code"], item["message"], severity="warning", count=item["count"], examples=item["examples"])

    groups = defaultdict(lambda: defaultdict(list))
    variants, models, tracks = Counter(), Counter(), Counter()
    videos = sorted(video_root.rglob("*.mp4")) if video_root.is_dir() else []
    for video in videos:
        relative = video.relative_to(video_root)
        match = VIDEO_NAME.fullmatch(video.name)
        if len(relative.parts) != 3 or not match:
            issue("unrecognized_video", "预期 <模型>/<track>/<case>__A或B__<版本>[__seed_数字].mp4",
                  path=str(video))
            continue
        if not video.resolve().is_relative_to(source):
            issue("unsafe_video_path", "视频链接指向数据目录之外", fatal=True, path=str(video))
            continue
        model, track = relative.parts[:2]
        case, side, variant, seed = match.group("case", "side", "variant", "seed")
        side = side.upper()
        seed = int(seed) if seed is not None else None
        groups[(model, track, case, variant, seed)][side].append(video)
        variants[variant] += 1
        models[model] += 1
        tracks[track] += 1
    complete = 0
    for key, sides in sorted(groups.items(), key=lambda item: str(item[0])):
        model, track, case, variant, seed = key
        context = {"model": model, "track": track, "source_case_id": case,
                   "variant": variant, "seed": seed}
        usable = True
        if any(len(paths) != 1 for paths in sides.values()):
            issue("duplicate_video", "同组同一偏好有多个视频，不能自动选择", **context)
            usable = False
        if set(sides) != {"A", "B"}:
            issue("incomplete_pair", "缺少配对视频", missing_sides=sorted({"A", "B"} - set(sides)), **context)
            usable = False
        elif usable:
            complete += 1
        if (case, track) not in by_key or (case, track) in duplicates:
            issue("unmatched_metadata", "无法唯一匹配基础信息", **context)
            usable = False
        if (case, track) in invalid_frames:
            usable = False
        for paths in sides.values():
            for video in paths:
                if video.stat().st_size == 0:
                    issue("empty_video", "视频文件为空", path=str(video), **context)
                    usable = False
        if not usable:
            continue
        identity = json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))
        reference = {}
        if translations:
            for field in TRANSLATED_FIELDS:
                text = translations.get(_norm_text(by_key[(case, track)][field]))
                if text:
                    reference[f"{field}_zh"] = text
        tasks.append({**by_key[(case, track)], **reference,
                      "task_id": hashlib.sha256(identity.encode()).hexdigest(),
                      "initial_frame": frames.get((case, track)),
                      "video_a": sides["A"][0].relative_to(source).as_posix(),
                      "video_b": sides["B"][0].relative_to(source).as_posix(),
                      "world_model": model, "stage": variant, "generation_variant": variant,
                      "method": methods.get(variant), "generation_seed": seed})
    for variant in variants:
        if methods.get(variant) is None:
            issue("unclassified_method", "未声明 raw/AlignX 对应关系；保留原始版本名，method=null",
                  severity="warning", variant=variant)
    fatal = any(i["fatal"] for i in issues)
    covered_keys = {(key[2], key[1]) for key in groups}
    selected_assets = {t[field] for t in tasks for field in ("initial_frame", "video_a", "video_b") if t[field]}
    report["summary"] = {
        "metadata_rows": len(rows), "unique_cases": len({k[0] for k in by_key}),
        "metadata_without_videos": len(set(by_key) - covered_keys),
        "missing_frame_rows": missing_frame_rows, "video_files": len(videos),
        "video_groups": len(groups), "complete_pairs": complete,
        "eligible_tasks": len(tasks), "excluded_groups": len(groups) - len(tasks),
        "tasks_without_frame": sum(t["initial_frame"] is None for t in tasks),
        "fatal_errors": sum(i["fatal"] for i in issues), "can_prepare": bool(tasks) and not fatal,
        "snapshot_media_bytes": sum((source / p).stat().st_size for p in selected_assets),
        "models": dict(models), "tracks": dict(tracks), "variants": dict(variants),
        "issue_counts": dict(Counter(i["code"] for i in issues)),
        "translation": {**translation_summary,
                        "tasks_fully_translated": sum(all(f"{f}_zh" in t for f in TRANSLATED_FIELDS) for t in tasks)},
    }
    return tasks, report


ISSUE_LABELS = {
    "missing_directory": "缺少输入目录", "invalid_config": "input_config.json 无效",
    "invalid_json": "元数据 JSON 解析失败", "metadata_error": "元数据文件错误",
    "duplicate_metadata": "元数据重复（source_case_id + track）", "invalid_metadata": "元数据记录无效",
    "unsafe_video_path": "视频路径超出数据目录", "missing_images_directory": "缺少初始帧目录",
    "missing_frame": "缺少初始帧", "invalid_frame_format": "初始帧格式不支持",
    "unrecognized_video": "视频路径或文件名不符合约定", "duplicate_video": "同组同侧有多个视频",
    "incomplete_pair": "缺少 A/B 配对视频", "unmatched_metadata": "视频找不到对应的基础信息",
    "empty_video": "视频文件为空", "unclassified_method": "未声明版本对应的方法（raw / AlignX）",
    "translation_unreadable": "参考译文文件无法读取", "translation_malformed": "参考译文格式不对的行",
    "translation_empty": "参考译文为空", "translation_not_chinese": "参考译文不含中文",
    "translation_number_mismatch": "参考译文数字与原文不一致", "translation_conflict": "同一句有多个参考译文",
    "translation_missing": "题目文本没有参考译文", "translation_disabled": "参考译文功能已关闭",
}
ISSUE_LEVELS = (("fatal", "致命错误（必须处理，否则无法创建题库）"),
                ("error", "错误（相关视频组不会进入题库）"),
                ("warning", "警告（不影响创建题库）"))


def issue_level(item):
    return "fatal" if item.get("fatal") else ("warning" if item.get("severity") == "warning" else "error")


def group_issues(issues):
    """[{level, level_label, groups: [{code, label, count, items}]}] — same-type problems folded together."""
    out = []
    for level, level_label in ISSUE_LEVELS:
        groups = {}
        for item in issues:
            if issue_level(item) == level:
                groups.setdefault(item["code"], []).append(item)
        if groups:
            out.append({"level": level, "level_label": level_label, "count": sum(len(v) for v in groups.values()),
                        "groups": [{"code": code, "label": ISSUE_LABELS.get(code, items[0]["message"]),
                                    "count": len(items), "items": items}
                                   for code, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))]})
    return out


def translation_line(info):
    if not info or info["status"] == "missing":
        return f"参考译文：未找到 question_base/{TRANSLATION_FILE}，评测界面不显示参考译文。"
    if info["status"] == "disabled":
        return "参考译文：文件存在但无法使用，已关闭（原因见问题明细）。"
    return (f"参考译文：已启用。可用译文 {info['entries']} 条，覆盖题目文本 {info['covered_texts']} / {info['needed_texts']} 条；"
            f"{info.get('tasks_fully_translated', 0)} 道可用题目三段文字均有译文。")


def save_report(report, out=REPORTS):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "input_check.json", report)
    s = report["summary"]
    lines = ["# 数据输入检查", "", f"输入目录：`{report['source_root']}`", "",
             f"基础信息 {s['metadata_rows']} 条；视频 {s['video_files']} 个；完整配对 {s['complete_pairs']} 组。",
             f"可用题目 {s['eligible_tasks']} 道；排除分组 {s['excluded_groups']} 组；致命错误 {s['fatal_errors']} 个。", "",
             f"其中 {s['tasks_without_frame']} 道可用题目未提供初始帧（警告，不阻止入库）。完整配对仍需要有效元数据。检查不验证视频解码或内容质量。", "",
             translation_line(s.get("translation")), "",
             "## 问题明细（按类别汇总）", ""]
    grouped = group_issues(report["issues"])
    for level in grouped:
        lines.append(f"### {level['level_label']} · 共 {level['count']} 条")
        lines.append("")
        for group in level["groups"]:
            lines.append(f"- **{group['label']}**（`{group['code']}`）：{group['count']} 条")
        lines.append("")
    for level in grouped:
        for group in level["groups"]:
            lines.append(f"<details><summary>{level['level_label'][:2]} · {group['label']} · {group['count']} 条</summary>")
            lines.append("")
            for item in group["items"]:
                context = {k: v for k, v in item.items() if k not in {"message", "fatal", "code", "severity"}}
                lines.append(f"- {item['message']} — `{json.dumps(context, ensure_ascii=False)}`")
            lines += ["", "</details>", ""]
    if not report["issues"]:
        lines.append("无问题。")
    (out / "input_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "input_check.md"


def prepare_batch(source, destination, metadata=None, progress=None):
    """Copy selected media into a new batch; publishing never overwrites a batch."""
    progress = progress or (lambda percent, message: None)
    progress(0, "正在检查输入数据")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("题库已存在，不能覆盖；请换一个题库名")
    source = Path(source).resolve()
    tasks, report = scan_source(source, metadata)
    save_report(report, REPORTS)
    if not report["summary"]["can_prepare"]:
        raise ValueError("无法创建评测题库：没有可用题目或存在致命错误。请查看 reports/input_check.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preparing-", dir=destination.parent) as temp:
        staging = Path(temp) / "batch"
        staging.mkdir()
        assets = {}
        total_assets = len({t[f] for t in tasks for f in ("initial_frame", "video_a", "video_b") if t[f]})
        for task in tasks:
            for field in ("initial_frame", "video_a", "video_b"):
                relative = task[field]
                if not relative or relative in assets:
                    continue
                original = inside(source, relative)
                copied = staging / "media" / relative
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, copied)
                assets[relative] = {"sha256": sha256(copied), "size": copied.stat().st_size}
                progress(5 + 30 * len(assets) / total_assets, f"复制素材 {len(assets)} / {total_assets}")
        from video_playback import browser_asset
        converted = 0
        for task in tasks:
            for field in ("video_a", "video_b"):
                progress(35 + 60 * converted / (2 * len(tasks)), f"检查 / 转换视频 {converted + 1} / {2 * len(tasks)}（当前视频处理期间进度保持不变）")
                original = staging / "media" / task[field]
                playback = browser_asset(original, staging / "media" / "_browser")
                converted += 1
                if playback != original:
                    task[f"original_{field}"] = task[field]
                    task[field] = playback.relative_to(staging / "media").as_posix()
                    assets[task[field]] = {"sha256": sha256(playback), "size": playback.stat().st_size}
        progress(96, "写入并固定题单")
        manifest = staging / "tasks.jsonl"
        manifest.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
        save_report(report, staging)
        write_json(staging / "snapshot.json", {
            "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "source_root": str(source), "metadata_file": report.get("metadata_file"),
            "metadata_sha256": report.get("metadata_sha256"),
            "task_count": len(tasks), "manifest_sha256": sha256(manifest), "assets": assets,
            "translation": report["summary"].get("translation"),
        })
        staging.rename(destination)
    progress(100, "题库创建完成")
    return report


def verify_batch(directory):
    """Verify snapshot content on startup; return stat signatures for runtime guards."""
    directory = Path(directory).resolve()
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != 1:
        raise ValueError("不支持的题库版本")
    manifest = directory / "tasks.jsonl"
    if sha256(manifest) != snapshot["manifest_sha256"]:
        raise ValueError("题库题单已被修改，请恢复原文件或创建新题库")
    tasks = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not tasks or len(tasks) != snapshot["task_count"]:
        raise ValueError("题库题目或媒体清单不一致")
    from assignment import load_slot, task_db_id
    slot = load_slot(directory)
    if slot:
        # A per-computer package: only this slot's playable media are shipped (never the originals).
        by_id = {task_db_id(t): t for t in tasks}
        if any(item["task_id"] not in by_id for item in slot["tasks"]):
            raise ValueError("评测包的题单与题库不一致")
        required = {by_id[item["task_id"]][field] for item in slot["tasks"]
                    for field in ("initial_frame", "video_a", "video_b") if by_id[item["task_id"]][field]}
        if not required <= set(snapshot["assets"]):
            raise ValueError("题库题目或媒体清单不一致")
        to_check = {relative: snapshot["assets"][relative] for relative in required}
    else:
        required = {t[field] for t in tasks for field in ("initial_frame", "video_a", "video_b") if t[field]}
        required.update(t[field] for t in tasks for field in ("original_video_a", "original_video_b") if t.get(field))
        if required != set(snapshot["assets"]):
            raise ValueError("题库题目或媒体清单不一致")
        to_check = snapshot["assets"]
    root = directory / "media"
    signatures = {}
    for relative, expected in to_check.items():
        path = inside(root, relative)
        if not path.is_file() or path.stat().st_size != expected["size"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"题库媒体缺失或已修改：{relative}")
        stat = path.stat()
        signatures[path] = (stat.st_size, stat.st_mtime_ns)
    return signatures
