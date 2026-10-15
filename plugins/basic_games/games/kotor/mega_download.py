import json
from pathlib import Path
from urllib.parse import urlparse


def is_mega_url(url: str) -> bool:
    return (urlparse(url).hostname or "").casefold() in {"mega.nz", "www.mega.nz"}


def completed_mega_download(stdout: str, downloads_path: Path) -> Path | None:
    try:
        downloads_root = downloads_path.resolve()
    except OSError:
        return None
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("event") != "complete":
            continue
        path_text = str(payload.get("path") or "").strip()
        if not path_text:
            continue
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = downloads_path / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.parent == downloads_root and resolved.is_file():
            return resolved
    return None
