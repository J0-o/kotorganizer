import re
from urllib.parse import urlparse



def nexus_mod_id_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "nexusmods.com" not in host:
        return ""
    match = re.search(r"/mods/(\d+)", url)
    return match.group(1) if match else ""



def nexus_file_id_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "nexusmods.com" not in host:
        return ""
    match = re.search(r"/files/(\d+)", url)
    return match.group(1) if match else ""



def extract_nexus_files(args) -> list:
    files: list = []
    for arg in args:
        if looks_like_nexus_file(arg):
            files.append(arg)
        elif isinstance(arg, (list, tuple)):
            files.extend(item for item in arg if looks_like_nexus_file(item))
    return files



def extract_nexus_file_info_payload(args) -> list:
    files = extract_nexus_files(args)
    if files:
        return files
    candidates = [arg for arg in args if not isinstance(arg, (str, int, bool, type(None)))]
    if candidates:
        return [candidates[0]]
    return []



def looks_like_nexus_file(value) -> bool:
    if isinstance(value, (str, int, bool, type(None))):
        return False
    return any(hasattr(value, attr) for attr in ("fileName", "name", "version", "fileID", "id"))



def describe_callback_args(args) -> str:
    descriptions = []
    for arg in args:
        if isinstance(arg, (list, tuple)):
            descriptions.append(f"{type(arg).__name__}[{', '.join(type(item).__name__ for item in arg)}]")
        else:
            descriptions.append(type(arg).__name__)
    return ", ".join(descriptions) or "(none)"



def nexus_file_id(file_info) -> int:
    values = nexus_file_values(file_info, ("fileID", "fileId", "file_id", "id"))
    for value in values:
        try:
            return int(value)
        except Exception:
            continue
    return 0



def nexus_file_name(file_info) -> str:
    values = nexus_file_values(file_info, ("fileName", "filename", "name", "displayName"))
    return str(values[0]).strip() if values else ""



def nexus_file_latest_version(file_info) -> str:
    values = nexus_file_values(file_info, ("newestVersion", "newest_version", "latestVersion", "latest_version", "version"))
    for value in values:
        text = str(value).strip()
        if text and text != "0.0.0.0":
            return text
    return ""



def nexus_file_values(file_info, names) -> list:
    if file_info is None:
        return []
    values: list = []
    if isinstance(file_info, dict):
        for name in names:
            if name in file_info:
                values.append(file_info[name])
        return values
    for name in names:
        attr = getattr(file_info, name, None)
        if attr is None:
            continue
        try:
            values.append(attr() if callable(attr) else attr)
        except Exception:
            continue
    return values



def nexus_file_summary(file_info) -> str:
    if file_info is None:
        return "(none)"
    if isinstance(file_info, dict):
        keys = [
            "fileID",
            "fileId",
            "file_id",
            "id",
            "fileName",
            "filename",
            "name",
            "version",
            "newestVersion",
            "latestVersion",
        ]
        parts = ["dict"]
        for key in keys:
            if key in file_info:
                parts.append(f"{key}={file_info.get(key)}")
        if len(parts) == 1:
            parts.append(f"keys={', '.join(str(key) for key in file_info.keys())}")
        return "; ".join(parts)
    parts = [type(file_info).__name__]
    for attr in ("fileID", "id", "fileName", "filename", "name", "version", "newestVersion"):
        try:
            value = getattr(file_info, attr)
            raw = value() if callable(value) else value
            parts.append(f"{attr}={raw}")
        except Exception:
            pass
    return "; ".join(parts)
