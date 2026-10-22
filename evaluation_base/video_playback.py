"""Browser playback copies for videos Chrome/Edge cannot play; originals stay intact.

A video is left alone when it is already H.264 (Baseline/Main/High profile) + yuv420p, which every
browser plays. Anything else (MPEG-4 Part 2 "mp4v", HEVC, 10-bit, the lossless "High 4:4:4
Predictive" profile, ...) is re-encoded to a high-quality H.264 MP4
(CRF 18, visually transparent, roughly 10x smaller than lossless). If the FFmpeg build lacks
libx264 a VP9 WebM copy is made instead.
"""
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from data_import import HERE, sha256

_LOCK = threading.Lock()
_ENCODERS = {}
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_VIDEO_LINE = re.compile(r"Stream #\S+.*?Video:\s*([A-Za-z0-9_]+)(.*)")


BROWSER_H264_PROFILES = {None, "baseline", "constrained baseline", "main", "high"}


def ffmpeg_executable():
    bundled = sorted((HERE / "vendor/imageio_ffmpeg/binaries").glob("ffmpeg-*.exe"))
    if bundled and os.name == "nt":
        return str(bundled[0])
    return shutil.which("ffmpeg")


def probe_video(path, executable=None):
    """Return {"codec": str, "yuv420p": bool} from `ffmpeg -i`, or None when it cannot be read."""
    executable = executable or ffmpeg_executable()
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "-nostdin", "-hide_banner", "-i", str(path)],
                                capture_output=True, timeout=60, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stderr.decode("utf-8", errors="replace").splitlines():
        match = _VIDEO_LINE.search(line)
        if match and "attached pic" not in line:
            profile = re.match(r"\s*\(([^)]*)\)", match.group(2))
            return {"codec": match.group(1).lower(),
                    "profile": profile.group(1).strip().lower() if profile else None,
                    "yuv420p": bool(re.search(r"\byuvj?420p\b", match.group(2)))}
    return None


def _marker_scan(path):
    """Fallback when FFmpeg is unavailable: look for MPEG-4 Part 2 / HEVC sample entries."""
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(2 * 1024 * 1024)
        stream.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
        data = header + stream.read()
    return any(tag in data for tag in (b"mp4v", b"hvc1", b"hev1"))


def needs_copy(path, executable=None):
    path = Path(path)
    if path.suffix.lower() != ".mp4":
        return False
    info = probe_video(path, executable)
    if info is not None:
        # Browsers decode only 8-bit 4:2:0 H.264 in the Baseline / Main / High profiles. Lossless
        # encoders (e.g. Cosmos outputs) use "High 4:4:4 Predictive" even for yuv420p: not playable.
        return not (info["codec"] == "h264" and info["yuv420p"] and info["profile"] in BROWSER_H264_PROFILES)
    return _marker_scan(path)


def _has_x264(executable):
    if executable not in _ENCODERS:
        try:
            out = subprocess.run([executable, "-nostdin", "-hide_banner", "-encoders"],
                                 capture_output=True, timeout=60, creationflags=_NO_WINDOW).stdout
            _ENCODERS[executable] = b"libx264" in out
        except (OSError, subprocess.TimeoutExpired):
            _ENCODERS[executable] = False
    return _ENCODERS[executable]


def browser_asset(path, cache):
    path, cache = Path(path), Path(cache)
    executable = ffmpeg_executable()
    if not needs_copy(path, executable):
        return path
    if not executable:
        raise ValueError("此视频需要播放兼容处理，请安装 FFmpeg 或使用包含 vendor 的完整程序目录")
    digest = sha256(path)
    x264 = _has_x264(executable)
    destination = cache / (f"{digest}-h264-crf18-v1.mp4" if x264 else f"{digest}-vp9-crf20-v1.webm")
    with _LOCK:
        if destination.is_file() and destination.stat().st_size:
            return destination
        cache.mkdir(parents=True, exist_ok=True)
        temp = cache / f".converting-{uuid.uuid4().hex}{destination.suffix}"
        command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
                   "-map", "0:v:0", "-map", "0:a?",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p"]
        if x264:
            command += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart",
                        "-c:a", "aac", "-b:a", "128k"]
        else:
            command += ["-c:v", "libvpx-vp9", "-crf", "20", "-b:v", "0", "-row-mt", "1",
                        "-deadline", "good", "-cpu-used", "4", "-c:a", "libopus"]
        try:
            result = subprocess.run(command + [str(temp)], capture_output=True, timeout=1800,
                                    creationflags=_NO_WINDOW)
            if result.returncode or not temp.is_file() or not temp.stat().st_size:
                raise ValueError("无法生成浏览器播放副本：" + result.stderr.decode("utf-8", errors="replace")[-500:])
            os.replace(temp, destination)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("播放副本处理超时，请检查视频文件或稍后重试") from exc
        finally:
            temp.unlink(missing_ok=True)
    return destination
