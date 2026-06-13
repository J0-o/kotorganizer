import html
import json
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PyQt6.QtCore import QObject, pyqtSignal

from archive_service import ArchiveService
from sync_installer import install_kson_build


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)



def _kson_version_text_from_name(name: str) -> str:
    match = re.search(r"(\d{8})[_-]?(\d{6})", Path(name).stem)
    if not match:
        return "unknown"
    try:
        parsed = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "unknown"


class _FetchWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str, object)


    def __init__(self, cache_path: Path, build_key: str, game_name: str, repo: str, timeout: int):
        super().__init__()
        self._cache_path = cache_path
        self._build_key = build_key
        self._game_name = game_name
        self._repo = repo
        self._timeout = timeout


    def run(self):
        errors: list[str] = []
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            remote_kson, _source_url, remote_name = self._download_kson()
            remote_path = self._cache_path.parent / remote_name
            remote_path.write_text(json.dumps(remote_kson, indent=2), encoding="utf-8")
        except Exception as exc:
            errors.append(str(exc))

        try:
            selected_path, kson = self._latest_local_kson()
            kson["_selected_kson_name"] = selected_path.name
            self._cache_path.write_text(json.dumps(kson, indent=2), encoding="utf-8")
            mod_count = len([mod for mod in kson.get("mods", []) if ArchiveService.kson_mod_name(mod)])
            details = [
                f"Loaded {mod_count} mods for {kson.get('game') or self._build_key}.",
                f"KSON version: {_kson_version_text_from_name(selected_path.name)}",
                f"Selected KSON: {selected_path}",
                f"Source URL: {kson.get('_source_url') or '(local file)'}",
                f"Cache file: {self._cache_path}",
            ]
            if errors:
                details.extend(["", "Fetch warnings:", *errors])
            self.finished.emit(
                {
                    "selected_path": str(selected_path),
                    "kson": kson,
                    "mod_count": mod_count,
                    "details": "\n".join(details),
                    "warnings": errors,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc), errors)


    def _download_kson(self) -> tuple[dict, str, str]:
        errors: list[str] = []
        for branch in ("main", "master"):
            try:
                source_url, file_name = self._latest_kson_raw_url(branch)
                text = self._download_text(source_url)
                kson = json.loads(text)
                mods = kson.get("mods", [])
                if isinstance(mods, list) and any(ArchiveService.kson_mod_name(mod) for mod in mods):
                    kson["_source_url"] = source_url
                    kson["_fetched_at"] = datetime.now(timezone.utc).isoformat()
                    return kson, source_url, file_name
                errors.append(f"{source_url} -> no mod entries found")
            except Exception as exc:
                errors.append(f"{self._repo}/{branch} -> {exc}")
        raise RuntimeError("Unable to fetch a usable KSON manifest.\n\n" + "\n".join(errors))


    def _latest_kson_raw_url(self, branch: str) -> tuple[str, str]:
        tree_url = f"https://api.github.com/repos/{self._repo}/git/trees/{branch}?recursive=1"
        payload = json.loads(self._download_text(tree_url))
        files = [
            str(item.get("path", ""))
            for item in payload.get("tree", [])
            if item.get("type") == "blob" and self._is_game_kson_path(str(item.get("path", "")))
        ]
        if not files:
            raise RuntimeError(f"No {self._build_key} .kson files found on {branch}.")
        latest = max(files, key=self._kson_sort_key)
        return f"https://raw.githubusercontent.com/{self._repo}/{branch}/{quote(latest)}", Path(latest).name


    def _latest_local_kson(self) -> tuple[Path, dict]:
        candidates = []
        for path in self._cache_path.parent.glob("*.kson"):
            if path.name == self._cache_path.name:
                continue
            if self._is_game_kson_path(path.name):
                candidates.append(path)
        if not candidates and self._cache_path.exists():
            candidates.append(self._cache_path)
        if not candidates:
            raise RuntimeError(f"No local {self._build_key} KSON files are available.")

        errors: list[str] = []
        for path in sorted(candidates, key=lambda item: self._kson_sort_key(item.name), reverse=True):
            try:
                kson = json.loads(path.read_text(encoding="utf-8"))
                mods = kson.get("mods", [])
                if isinstance(mods, list) and any(ArchiveService.kson_mod_name(mod) for mod in mods):
                    return path, kson
                errors.append(f"{path.name}: no mod entries found")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        raise RuntimeError("No usable local KSON files are available.\n\n" + "\n".join(errors))


    def _download_text(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "KOTORganizer-MO2-SyncTab/1.0"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except URLError as exc:
            raise RuntimeError(str(exc)) from exc


    def _is_game_kson_path(self, path: str) -> bool:
        name = Path(path).name.lower()
        if not name.endswith(".kson"):
            return False
        if self._build_key == "kotor2":
            return name.startswith("kotor2")
        return name.startswith("kotor") and not name.startswith("kotor2")


    @staticmethod
    def _kson_sort_key(path: str) -> tuple[str, str]:
        name = Path(path).stem.lower()
        match = re.search(r"(\d{8}[_-]?\d{6})", name)
        timestamp = match.group(1).replace("_", "").replace("-", "") if match else ""
        return timestamp, name

class _SyncWorker(QObject):
    progress = pyqtSignal(int, int, str, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


    def __init__(self, kson_path: Path, downloads_path: Path, mods_path: Path, profile_path: Path):
        super().__init__()
        self._kson_path = kson_path
        self._downloads_path = downloads_path
        self._mods_path = mods_path
        self._profile_path = profile_path


    def run(self):
        try:
            result = install_kson_build(
                self._kson_path,
                self._downloads_path,
                self._mods_path,
                self._profile_path,
                progress=self.progress.emit,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class _ValidationWorker(QObject):
    progress = pyqtSignal(int, int, int, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


    def __init__(self, cache_path: Path, downloads_path: Path, kson: dict, row_specs: list[dict]):
        super().__init__()
        self._cache_path = cache_path
        self._downloads_path = downloads_path
        self._kson = kson
        self._row_specs = row_specs


    def run(self):
        try:
            runner = ArchiveService(self._downloads_path, self._cache_path)
            runner.prepare_tslrcm_archives_for_validation(self._kson)

            mods_by_name: dict[str, list[dict]] = {}
            for mod in self._kson.get("mods", []):
                if not isinstance(mod, dict):
                    continue
                mod_name = runner.kson_mod_name(mod)
                if mod_name:
                    mods_by_name.setdefault(mod_name, []).append(mod)

            hash_cache: dict[Path, str] = {}
            counts = {"ok": 0, "empty": 0, "missing": 0, "mismatch": 0, "skipped": 0}
            total = len(self._row_specs)

            for current, spec in enumerate(self._row_specs, start=1):
                mod = spec.get("mod")
                if not isinstance(mod, dict):
                    matches = mods_by_name.get(str(spec.get("mod_name") or ""), [])
                    mod = matches.pop(0) if matches else None
                if not isinstance(mod, dict):
                    result = runner._result(
                        "skipped",
                        "Skipped",
                        str(spec.get("mod_name") or ""),
                        "",
                        "",
                        None,
                        "",
                        "No matching KSON mod entry was found for this row.",
                    )
                else:
                    result = runner.validate_mod(mod, hash_cache=hash_cache)
                counts[str(result.get("bucket") or "skipped")] += 1
                self.progress.emit(current, total, int(spec.get("row_index", current - 1)), result)

            self.finished.emit(counts)
        except Exception as exc:
            self.failed.emit(str(exc))


class _DownloadedValidationWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, downloads_path: Path, cache_path: Path, mod: dict, archive_path: Path):
        super().__init__()
        self._downloads_path = downloads_path
        self._cache_path = cache_path
        self._mod = mod
        self._archive_path = archive_path

    def run(self):
        try:
            service = ArchiveService(self._downloads_path, self._cache_path)
            archive_path, wrap_result = self._wrap_loose_download(service, self._archive_path, service.expected_archive_name(self._mod))
            result = service.validate_archive_path(
                self._mod,
                archive_path,
                allow_content_hash=True,
            )
            self.finished.emit({"archive_path": str(archive_path), "wrap_result": wrap_result, "validation": result})
        except Exception as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def _wrap_loose_download(service: ArchiveService, archive_path: Path, archive_name: str) -> tuple[Path, str]:
        converted_path, converted_result = service.convert_tslrcm_installer_if_needed(archive_path, archive_name)
        if converted_path != archive_path or converted_result:
            return converted_path, converted_result
        if service.is_known_archive(archive_path):
            return archive_path, ""
        expected_name = html.unescape(archive_name).strip()
        if expected_name:
            wrapped_path = archive_path.with_name(expected_name)
        else:
            wrapped_path = archive_path.with_name(f"{archive_path.name}.zip")
        if wrapped_path.suffix.lower() != ".zip":
            wrapped_path = wrapped_path.with_name(f"{wrapped_path.name}.zip")
        temp_path = wrapped_path.with_name(f"{wrapped_path.name}.tmp")
        seven_zip = service.seven_zip_exe()
        if seven_zip:
            result = subprocess.run(
                [seven_zip, "a", "-tzip", "-mx=0", str(temp_path), archive_path.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                startupinfo=service.subprocess_startupinfo(),
                creationflags=service.subprocess_creationflags(),
                cwd=str(archive_path.parent),
            )
            if result.returncode == 0 and temp_path.exists():
                temp_path.replace(wrapped_path)
                if archive_path != wrapped_path and archive_path.exists():
                    archive_path.unlink()
                return wrapped_path, f"Wrapped loose file as uncompressed ZIP with 7-Zip: {wrapped_path.name}"

        original_bytes = archive_path.read_bytes()
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo(archive_path.name, _FIXED_ZIP_TIMESTAMP)
            archive.writestr(info, original_bytes, compress_type=zipfile.ZIP_STORED)
        temp_path.replace(wrapped_path)
        if archive_path != wrapped_path and archive_path.exists():
            archive_path.unlink()
        return wrapped_path, f"Wrapped loose file as uncompressed ZIP: {wrapped_path.name}"
