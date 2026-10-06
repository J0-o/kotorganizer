import base64
import difflib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from hash_utils import file_hash as _file_hash
from hash_utils import file_hashes as _file_hashes


TEXT_EXTENSIONS = {".ini", ".cfg", ".txt"}
SUBPROCESS_STARTUPINFO = None
SUBPROCESS_CREATIONFLAGS = 0
if os.name == "nt":
    SUBPROCESS_STARTUPINFO = subprocess.STARTUPINFO()
    SUBPROCESS_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    SUBPROCESS_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW



@dataclass
class BuildInputMod:
    priority: int
    mod_name: str
    archive_name: str
    archive_path: Path | None
    version: str
    release_date: str
    url: str
    mod_path: Path
    repository: str = ""
    mod_id: str = ""
    file_id: str = ""
    enabled: bool = True



@dataclass
class BuildResult:
    output_path: Path
    mod_count: int
    skipped: list[str]
    warnings: list[str]


@dataclass
class InitialBuildMod:
    priority: int
    mod_name: str
    archive_name: str
    url: str
    version: str = ""
    release_date: str = ""
    repository: str = ""
    mod_id: str = ""
    file_id: str = ""
    enabled: bool = True



def build_instruction_set(
    game_name: str,
    output_path: Path,
    mods: list[BuildInputMod],
    tslpatch_order_path: Path | None = None,
    progress=None,
) -> BuildResult:
    warnings: list[str] = []
    skipped: list[str] = []
    ordered_mods = sorted(mods, key=lambda item: item.priority)
    payload = {
        "format": "kotor-builder-instructions",
        "format_version": 1,
        "game": game_name,
        "tslpatch_order": _read_tslpatch_order(tslpatch_order_path),
        "mods": [],
    }

    total = len(ordered_mods)
    if progress and ordered_mods:
        progress(0, total, "7-Zip", "Using bundled kotor/7z.exe")

    for index, mod in enumerate(ordered_mods, start=1):
        if progress:
            progress(index, total, mod.mod_name, "Reading archive and installed files")
        try:
            payload["mods"].append(_build_mod_payload(mod, warnings, progress, index, total))
        except Exception as exc:
            skipped.append(f"{mod.mod_name}: {exc}")

    if progress:
        progress(total, total, "Writing output", str(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return BuildResult(output_path=output_path, mod_count=len(payload["mods"]), skipped=skipped, warnings=warnings)


def build_initial_instruction_set(
    game_name: str,
    output_path: Path,
    mods: list[InitialBuildMod],
) -> BuildResult:
    payload = {
        "format": "kotor-builder-instructions",
        "format_version": 1,
        "game": game_name,
        "tslpatch_order": {},
        "mods": [],
    }
    ordered_mods = sorted(mods, key=lambda item: item.priority)
    for mod in ordered_mods:
        payload["mods"].append(
            {
                "priority": mod.priority,
                "mod_name": mod.mod_name,
                "enabled": mod.enabled,
                "archive_name": str(mod.archive_name or "").strip(),
                "archive_xxh3": "",
                "version": mod.version,
                "release_date": mod.release_date,
                "url": mod.url,
                "repository": mod.repository,
                "mod_id": mod.mod_id,
                "file_id": mod.file_id,
                "archive_files": [],
                "actions": [],
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return BuildResult(output_path=output_path, mod_count=len(payload["mods"]), skipped=[], warnings=[])



def _build_mod_payload(mod: BuildInputMod, warnings: list[str], progress, current: int, total: int) -> dict:
    if progress:
        progress(current, total, mod.mod_name, f"Reading archive: {mod.archive_name or '(none)'}")
    archive_files = _archive_files(mod.archive_path, warnings, progress, current, total, mod.mod_name) if mod.archive_path else []
    archive_by_hash = {}
    archive_by_name = {}
    for item in archive_files:
        archive_by_hash.setdefault(item["xxh3"], []).append(item)
        archive_by_name.setdefault(Path(item["path"]).name.casefold(), []).append(item)

    if progress:
        progress(current, total, mod.mod_name, "Hashing installed mod folder")
    installed_files = _installed_files(mod.mod_path, progress, current, total, mod.mod_name)
    used_archive_paths: set[str] = set()
    actions = []
    if progress:
        progress(current, total, mod.mod_name, f"Comparing {len(installed_files)} installed files to {len(archive_files)} archive files")
    for installed in installed_files:
        installed_name = Path(installed["path"]).name.casefold()
        same_hash = archive_by_hash.get(installed["xxh3"], [])
        same_name = archive_by_name.get(installed_name, [])
        source = same_hash[0] if same_hash else (same_name[0] if same_name else None)
        if source:
            used_archive_paths.add(source["path"])

        if source and source["xxh3"] == installed["xxh3"]:
            action = "rename" if Path(source["path"]).name.casefold() != Path(installed["path"]).name.casefold() else "move"
            actions.append(
                {
                    "action": action,
                    "source": source["path"],
                    "destination": installed["path"],
                    "xxh3": installed["xxh3"],
                }
            )
        elif source and _is_text_path(installed["path"]):
            patch = _binary_delta_patch(source.get("data", b""), installed.get("data", b""))
            actions.append(
                {
                    "action": "patched",
                    "source": source["path"],
                    "destination": installed["path"],
                    "base_xxh3": source["xxh3"],
                    "result_xxh3": installed["xxh3"],
                    "patch": patch,
                }
            )
        else:
            actions.append(
                {
                    "action": "move",
                    "source": source["path"] if source else None,
                    "destination": installed["path"],
                    "xxh3": installed["xxh3"],
                    "generated": source is None,
                }
            )

    for archive_file in archive_files:
        if archive_file["path"] not in used_archive_paths:
            actions.append(
                {
                    "action": "delete",
                    "source": archive_file["path"],
                    "xxh3": archive_file["xxh3"],
                }
            )

    return {
        "priority": mod.priority,
        "mod_name": mod.mod_name,
        "enabled": mod.enabled,
        "archive_name": str(mod.archive_name or "").strip(),
        "archive_xxh3": _file_hash(mod.archive_path) if mod.archive_path and mod.archive_path.exists() else "",
        "version": mod.version,
        "release_date": mod.release_date,
        "url": mod.url,
        "repository": mod.repository,
        "mod_id": mod.mod_id,
        "file_id": mod.file_id,
        "archive_files": [_without_data(item) for item in archive_files],
        "actions": actions,
    }



def _read_tslpatch_order(path: Path | None) -> dict | list:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}



def _archive_files(path: Path, warnings: list[str], progress=None, current: int = 0, total: int = 0, mod_name: str = "") -> list[dict]:
    if not path.exists():
        warnings.append(f"Archive not found: {path}")
        return []
    return _seven_zip_files(path, warnings, progress, current, total, mod_name)



def _seven_zip_files(path: Path, warnings: list[str], progress=None, current: int = 0, total: int = 0, mod_name: str = "") -> list[dict]:
    exe = _seven_zip_exe()
    if not exe:
        warnings.append(
            f"Bundled 7-Zip is missing. Expected kotor/7z.exe and kotor/7z.dll; cannot inspect archive: {path.name}"
        )
        return []

    with tempfile.TemporaryDirectory(prefix="kotorganizer_archive_") as temp_dir:
        temp_path = Path(temp_dir)
        if progress:
            progress(current, total, mod_name, f"Extracting archive once with 7-Zip: {path.name}")
        result = subprocess.run(
            [exe, "x", "-y", f"-o{temp_path}", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            startupinfo=SUBPROCESS_STARTUPINFO,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            warnings.append(f"Bundled 7-Zip failed to extract {path.name}: {detail}")
            return []

        return _folder_files(temp_path, temp_path, progress, current, total, mod_name, "Hashing extracted archive files")



@lru_cache(maxsize=1)
def _seven_zip_exe() -> str:
    plugin_dir = Path(__file__).resolve().parent.parent
    exe = plugin_dir / "7z.exe"
    dll = plugin_dir / "7z.dll"
    if exe.exists() and dll.exists():
        return str(exe)
    return ""



def _installed_files(mod_path: Path, progress=None, current: int = 0, total: int = 0, mod_name: str = "") -> list[dict]:
    return _folder_files(mod_path, mod_path, progress, current, total, mod_name, "Hashing installed files", skip_meta=True)



def _folder_files(
    root_path: Path,
    relative_root: Path,
    progress=None,
    current: int = 0,
    total: int = 0,
    mod_name: str = "",
    status_prefix: str = "Hashing files",
    skip_meta: bool = False,
) -> list[dict]:
    items = []
    file_paths = sorted(
        path for path in root_path.rglob("*")
        if path.is_file() and not (skip_meta and path.name.casefold() == "meta.ini")
    )
    file_hashes = _file_hashes(file_paths)
    for index, file_path in enumerate(file_paths, start=1):
        if progress and (index == 1 or index % 50 == 0 or index == len(file_paths)):
            progress(current, total, mod_name, f"{status_prefix} {index}/{len(file_paths)}")
        rel_path = file_path.relative_to(relative_root).as_posix()
        item = {
            "path": rel_path.replace("\\", "/"),
            "size": file_path.stat().st_size,
            "xxh3": file_hashes.get(file_path) or _file_hash(file_path),
        }
        if _is_text_path(rel_path):
            item["data"] = file_path.read_bytes()
        items.append(item)
    return items



def _without_data(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "data"}



def _is_text_path(path: str) -> bool:
    return Path(path).suffix.casefold() in TEXT_EXTENSIONS



def _binary_delta_patch(source: bytes, target: bytes) -> dict:
    operations = []
    matcher = difflib.SequenceMatcher(a=source, b=target, autojunk=False)
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        operations.append(
            {
                "offset": source_start,
                "delete": source_end - source_start,
                "insert_base64": base64.b64encode(target[target_start:target_end]).decode("ascii"),
            }
        )

    return {
        "format": "multi-span-binary-v1",
        "source_size": len(source),
        "target_size": len(target),
        "operations": operations,
    }
