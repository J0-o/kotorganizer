import json
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

from .full_build_parser import canonical_url


KOTOR_REPO_DATA_URL = "https://raw.githubusercontent.com/J0-o/KotorRepo/main/data/{game}.json"


@dataclass(frozen=True)
class KotorRepoMetadata:
    latest_version: str
    latest_version_release_date: str
    original_upload_date: str
    available_files: list[str]


def fetch_kotor_repo_metadata(game_short_name: str, timeout: int) -> dict[str, KotorRepoMetadata]:
    game = "k2" if game_short_name.lower() == "kotor2" else "k1"
    url = KOTOR_REPO_DATA_URL.format(game=game)
    request = Request(url, headers={"User-Agent": "KOTORganizer-MO2-Builder/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to fetch KotorRepo metadata from {url}: {exc}") from exc
    return parse_kotor_repo_metadata(payload)


def parse_kotor_repo_metadata(payload: dict) -> dict[str, KotorRepoMetadata]:
    if not isinstance(payload, dict) or not isinstance(payload.get("mods"), list):
        raise ValueError("KotorRepo response does not contain a mods list")
    records: dict[str, KotorRepoMetadata] = {}
    for item in payload.get("mods", []):
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            continue
        url = canonical_url(str(item.get("url", "")))
        if not url:
            continue
        metadata = item["metadata"]
        records[url] = KotorRepoMetadata(
            latest_version=str(metadata.get("latestVersion", "")).strip(),
            latest_version_release_date=str(metadata.get("latestVersionReleaseDate", "")).strip(),
            original_upload_date=str(metadata.get("originalUploadDate", "")).strip(),
            available_files=[
                str(download.get("fileName", "")).strip()
                for download in metadata.get("availableDownloads", [])
                if isinstance(download, dict) and str(download.get("fileName", "")).strip()
            ],
        )
    return records
