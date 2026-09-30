"""Media/file helpers shared by upload routes and recognition providers."""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

def safe_filename(name: str) -> str:
    name = Path(name or "video.mp4").name
    name = re.sub(r"[^\w\-.\u4e00-\u9fff ]+", "_", name).strip(" .")
    return name or "video.mp4"


def title_from_filename(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", safe_filename(name)).strip() or "未命名视频"


_EPISODE_PATTERNS = (
    re.compile(r"第\s*0*(\d{1,4})\s*(?:集|话|期)", re.IGNORECASE),
    re.compile(r"(?:^|[\s_\-.()（）\[\]【】])(?:ep|e|episode)\s*0*(\d{1,4})(?=$|[\s_\-.()（）\[\]【】])", re.IGNORECASE),
)


def episode_from_filename(name: str) -> int | None:
    """Extract an episode number only from an explicit filename marker.

    Pure numeric filenames (``1.mp4``) are common for episode batches.  Other
    numbers are accepted when they are separated from the title, while long
    hashes and dates remain untouched instead of being mistaken for episodes.
    """
    stem = re.sub(r"\.[^.]+$", "", safe_filename(name)).strip()
    if not stem:
        return None
    if re.match(r"^(?:19|20)\d{6}(?:$|[\s_\-.])", stem):
        return None
    if stem.isdigit():
        value = int(stem)
        return value if 0 < value <= 9999 else None
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(stem)
        if match:
            value = int(match.group(1))
            return value if 0 < value <= 9999 else None
    # A standalone leading number such as 01_开场.mp4 is an episode marker.
    match = re.match(r"^0*(\d{1,4})(?=$|[\s_\-.()（）\[\]【】])", stem)
    if match:
        value = int(match.group(1))
        return value if 0 < value <= 9999 else None
    return None


def title_with_episode(title: str, file_name: str) -> str:
    """Make the episode visible in the displayed and exported screenplay title."""
    base = str(title or "").strip() or title_from_filename(file_name)
    episode = episode_from_filename(file_name)
    stem = re.sub(r"\.[^.]+$", "", safe_filename(file_name)).strip()
    if episode is None:
        return base
    if re.search(r"第\s*\d+\s*(?:集|话|期)", base, re.IGNORECASE):
        return re.sub(r"第\s*\d+\s*(集|话|期)", f"第{episode}集", base, count=1, flags=re.IGNORECASE)
    if base == stem and stem.isdigit():
        return f"第{episode}集"
    return f"{base} 第{episode}集"


def episode_sort_key(file_name: str, fallback: int = 0) -> tuple[int, int, int]:
    """Sort numbered episodes before unnumbered videos, preserving input order."""
    episode = episode_from_filename(file_name)
    return (0, episode, fallback) if episode is not None else (1, 0, fallback)


def probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        value = float(result.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def sample_script(title: str, duration_sec: float, transcript: str = "") -> dict[str, Any]:
    """Return a stable script shape even when no AI provider is configured.

    This fallback keeps the upload -> processing -> export loop usable locally.
    The provider hook below can replace the scene content without changing the API.
    """
    action = "视频已完成音视频素材整理，建议根据成片快速核对人物名与专有名词。"
    if transcript:
        action = transcript[:500]
    return {
        "version": "1.0",
        "title": title,
        "characters": [],
        "scenes": [
            {
                "id": "scene_001",
                "heading": "1-1 不明 不明 未标注地点",
                "location": "待补充",
                "characters": [],
                # A duration note is metadata, not an environment description.
                # Keeping it out of the script avoids the unhelpful generic first
                # sentence that previously appeared before every scene.
                "environment": "",
                "summary": "开场信息不足，无法从当前素材确认人物关系和下一步行动。",
                "blocks": [{"type": "action", "text": action}],
            }
        ],
    }


def multipart_body(fields: dict[str, str], file_field: str, file_name: str, file_data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----JBB{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        file_data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary
