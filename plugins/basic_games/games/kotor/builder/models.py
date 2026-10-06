from dataclasses import dataclass
from pathlib import Path



@dataclass
class InstalledModEntry:
    name: str
    priority: int
    enabled: bool
    meta_candidates: list[tuple[str, Path, dict[str, str]]]
    urls: list[str]
    installation_file: str
    download_meta_path: Path | None
    download_meta: dict[str, str]
    mo2_version: str
    mo2_newest_version: str
    mo2_nexus_id: str



@dataclass
class ValidationResult:
    summary: str
    details: list[str]
