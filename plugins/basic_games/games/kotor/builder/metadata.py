import configparser
import html
from pathlib import Path

from .full_build_parser import canonical_url


DownloadMetaEntry = tuple[str, Path, dict[str, str]]



def read_mod_meta(mod_path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    meta_path = mod_path / "meta.ini"
    try:
        parser.read(meta_path, encoding="utf-8")
    except Exception:
        return {}
    if not parser.has_section("General"):
        return {}
    return {key: value for key, value in parser["General"].items()}



def read_download_meta(download_meta_path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(download_meta_path, encoding="utf-8")
    except Exception:
        return {}
    if not parser.has_section("General"):
        return {}
    return {key: value for key, value in parser["General"].items()}



def download_meta_path(downloads_path: Path, installation_file: str) -> Path | None:
    candidates = [
        installation_file,
        html.unescape(installation_file),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip().strip('"').strip("'")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        meta_path = downloads_path / f"{cleaned}.meta"
        if meta_path.exists():
            return meta_path
    return None



def load_download_meta_entries(downloads_path: Path) -> list[DownloadMetaEntry]:
    entries: list[DownloadMetaEntry] = []
    if not downloads_path.exists():
        return entries
    for meta_path in downloads_path.glob("*.meta"):
        meta = read_download_meta(meta_path)
        installation_file = meta_path.name.removesuffix(".meta")
        entries.append((installation_file, meta_path, meta))
    return entries



def download_meta_info(mod_path: Path, downloads_path: Path) -> tuple[str, Path | None, dict[str, str]]:
    mod_meta = read_mod_meta(mod_path)
    installation_file = str(mod_meta.get("installationFile", "")).strip()
    if installation_file:
        meta_path = download_meta_path(downloads_path, installation_file)
        if meta_path is not None:
            return installation_file, meta_path, read_download_meta(meta_path)
    return "", None, {}



def installed_mod_urls(download_meta: dict[str, str], game_short_name: str) -> list[str]:
    if not download_meta:
        return []
    archive_game_name = download_meta.get("gameName", "").strip().lower()
    archive_mod_id = download_meta.get("modID", "").strip()
    nexus_url = ""
    if archive_mod_id.isdigit() and int(archive_mod_id) > 0:
        if archive_game_name in {"kotor", "kotor2"}:
            nexus_url = f"https://www.nexusmods.com/{archive_game_name}/mods/{archive_mod_id}"
        else:
            managed_game_name = game_short_name.lower()
            if managed_game_name in {"kotor", "kotor2"}:
                nexus_url = f"https://www.nexusmods.com/{managed_game_name}/mods/{archive_mod_id}"
    return unique_urls(
        [
            download_meta.get("manualURL", "").strip(),
            download_meta.get("manualUrl", "").strip(),
            download_meta.get("manualurl", "").strip(),
            download_meta.get("url", "").strip(),
            nexus_url,
        ]
    )



def write_mod_meta_for_build_match(
    mod_path: Path,
    build_name: str,
    build_url: str,
    archive_entry: DownloadMetaEntry | None,
) -> None:
    meta_path = mod_path / "meta.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(meta_path, encoding="utf-8")
    if not parser.has_section("General"):
        parser.add_section("General")
    general = parser["General"]
    general["buildName"] = build_name
    general["buildURL"] = build_url
    if archive_entry is not None:
        installation_file, _download_meta_path, download_meta = archive_entry
        general["installationFile"] = installation_file
        general["modName"] = str(download_meta.get("modName", "")).strip()
        general["manualURL"] = str(download_meta.get("manualURL", "")).strip()
        general["url"] = str(download_meta.get("url", "")).strip()
        author = str(download_meta.get("author", "")).strip()
        if author:
            general["author"] = author
        general["modID"] = str(download_meta.get("modID", "")).strip()
        general["fileID"] = str(download_meta.get("fileID", "")).strip()
    mod_path.mkdir(parents=True, exist_ok=True)
    with meta_path.open("w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)



def write_download_meta_field(meta_path: Path, field_name: str, value: str) -> bool:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(meta_path, encoding="utf-8")
        if not parser.has_section("General"):
            parser.add_section("General")
        parser["General"][field_name] = value
        with meta_path.open("w", encoding="utf-8") as handle:
            parser.write(handle, space_around_delimiters=False)
        return True
    except Exception:
        return False


def write_download_meta(downloads_path: Path, archive_name: str, fields: dict[str, str]) -> Path | None:
    cleaned = html.unescape(str(archive_name or "")).strip().strip('"').strip("'")
    if not cleaned:
        return None
    meta_path = downloads_path / f"{cleaned}.meta"
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        if meta_path.exists():
            parser.read(meta_path, encoding="utf-8")
        if not parser.has_section("General"):
            parser.add_section("General")
        for key, value in fields.items():
            text = str(value or "").strip()
            if text:
                parser["General"][key] = text
        with meta_path.open("w", encoding="utf-8") as handle:
            parser.write(handle, space_around_delimiters=False)
        return meta_path
    except Exception:
        return None



def unique_urls(urls) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        canonical = canonical_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(canonical)
    return ordered
