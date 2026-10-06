import json
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import mobase
from PyQt6.QtCore import QPoint, QProcess, QThread, QTimer, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .exporter import BuildInputMod, InitialBuildMod, build_initial_instruction_set
from .full_build_parser import BuildModEntry as _BuildModEntry
from .full_build_parser import canonical_url as _canonical_url
from .kotor_repo import (
    KOTOR_REPO_DATA_URL,
    KotorRepoMetadata,
    fetch_kotor_repo_data,
    fetch_kotor_repo_metadata,
    parse_kotor_repo_build_entries,
    parse_kotor_repo_metadata,
)
from .metadata import (
    DownloadMetaEntry,
    download_meta_info,
    download_meta_path,
    installed_mod_urls,
    load_download_meta_entries,
    read_mod_meta,
    write_download_meta,
    write_download_meta_field,
    write_mod_meta_for_build_match,
)
from .models import InstalledModEntry as _InstalledModEntry
from .models import ValidationResult as _ValidationResult
from .nexus import (
    describe_callback_args,
    extract_nexus_file_info_payload,
    nexus_file_id,
    nexus_file_id_from_url,
    nexus_file_latest_version,
    nexus_file_name,
    nexus_file_summary,
    nexus_mod_id_from_url,
)
from .validation import versions_equal
from .workers import _BuildWorker, _NumericTreeWidgetItem
from ..ui_theme import configure_tree_widget, refresh_mo2, set_header_resize_mode



class KotorBuilderWindow(QWidget):
    _CACHE_VERSION = 7
    _FETCH_TIMEOUT_SECONDS = 20
    _NEXUS_TIMEOUT_SECONDS = 20
    _ARCHIVE_RELEASE_DATE_FIELD = "ArchiveReleaseDate"


    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._build_entries: list[_BuildModEntry] = []
        self._validation_by_mod: dict[str, _ValidationResult] = {}
        self._validation_queue: list[tuple[_InstalledModEntry, _BuildModEntry]] = []
        self._validation_total = 0
        self._validation_done = 0
        self._validation_current_mod = ""
        self._nexus_bridge = None
        self._nexus_signal_count = 0
        self._nexus_validation_current: tuple[_InstalledModEntry, _BuildModEntry] | None = None
        self._nexus_validation_timer: QTimer | None = None
        self._nexus_last_request = ""
        self._nexus_last_payload = ""
        self._build_thread: QThread | None = None
        self._build_worker: _BuildWorker | None = None
        self._build_progress_lines: list[str] = []
        self._download_meta_entries_cache: list[DownloadMetaEntry] | None = None
        self._manual_build_match_by_mod: dict[str, str] = {}
        self._manual_archive_by_mod: dict[str, str] = {}
        self._editor_item: QTreeWidgetItem | None = None
        self._validation_force_release_update = False
        self._validation_stop_requested = False
        self._kotor_repo_by_url: dict[str, KotorRepoMetadata] = {}
        self._kotor_repo_error = ""
        self._download_queue: list[_BuildModEntry] = []
        self._download_process: QProcess | None = None
        self._browser_process = None
        self._browser_waiting: tuple[_BuildModEntry, Path, str, float, str, set[str], dict[str, str]] | None = None
        self._download_total = 0
        self._download_existing_names: set[str] = set()
        self._author_by_url: dict[str, str] = {}
        self._author_queue: list[tuple[str, str]] = []
        self._author_process: QProcess | None = None
        self._author_total = 0
        self._author_updated = 0
        self._author_failed = 0
        self._init_nexus_bridge()
        self._load_cache()

        self.setWindowTitle(f"KOTOR Builder - {game.gameName()}")
        self.resize(980, 700)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("0 installed mods")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        self._fetch_btn = QPushButton("Fetch")
        self._fetch_btn.clicked.connect(self._fetch_full_build)
        self._download_all_btn = QPushButton("Download All")
        self._download_all_btn.clicked.connect(self._download_all_build_urls)
        self._validate_btn = QPushButton("Validate")
        self._validate_btn.clicked.connect(self._validate_matches)
        self._force_release_btn = QPushButton("Force ReleaseDate Update")
        self._force_release_btn.clicked.connect(self._force_release_date_update)
        self._grab_authors_btn = QPushButton("Grab Authors")
        self._grab_authors_btn.clicked.connect(self._grab_authors)
        self._apply_all_meta_btn = QPushButton("Apply All Meta")
        self._apply_all_meta_btn.clicked.connect(self._apply_all_meta)
        self._stop_validate_btn = QPushButton("Stop")
        self._stop_validate_btn.setEnabled(False)
        self._stop_validate_btn.clicked.connect(self._stop_validation)
        self._build_btn = QPushButton("Build")
        self._build_btn.clicked.connect(self._build_instruction_set)
        header.addWidget(self._refresh_btn)
        header.addWidget(self._fetch_btn)
        header.addWidget(self._download_all_btn)
        header.addWidget(self._validate_btn)
        header.addWidget(self._force_release_btn)
        header.addWidget(self._grab_authors_btn)
        header.addWidget(self._apply_all_meta_btn)
        header.addWidget(self._stop_validate_btn)
        header.addWidget(self._summary_label)
        header.addStretch()
        header.addWidget(self._build_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Priority", "Installed MO2 Mod", "Build Match", "Archive File", "Validation"])
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
        )
        header_view = self._tree.header()
        set_header_resize_mode(header_view, header_view.ResizeMode.Interactive, 4)
        self._tree.setColumnWidth(0, 80)
        self._tree.setColumnWidth(1, 330)
        self._tree.setColumnWidth(2, 360)
        self._tree.setColumnWidth(3, 360)
        self._tree.setColumnWidth(4, 160)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.itemSelectionChanged.connect(self._update_details)
        layout.addWidget(self._tree, 3)

        self._details = QPlainTextEdit(self)
        self._details.setReadOnly(True)
        self._details.setPlaceholderText("Builder details will appear here.")
        layout.addWidget(self._details, 2)

        self.refresh()


    def refresh(self):
        current_item = self._tree.currentItem()
        current_mod_name = str(current_item.data(0, Qt.ItemDataRole.UserRole + 1) or "") if current_item else ""
        scroll_value = self._tree.verticalScrollBar().value()
        self._editor_item = None
        self._tree.clear()
        self._download_meta_entries_cache = None
        installed_mods = self._installed_mod_entries()
        selected_item = None
        matched_build_keys: set[str] = set()
        for installed in installed_mods:
            matches = self._matching_build_entries(installed)
            matched_build_keys.update(_build_entry_key(entry) for entry in matches)
            match_text = ", ".join(entry.name for entry in matches) or "No URL match"
            validation = self._validation_by_mod.get(installed.name)
            validation_text = validation.summary if validation is not None else ""
            archive_text = self._manual_archive_by_mod.get(installed.name, "") or installed.installation_file or "No archive"
            item = _NumericTreeWidgetItem([str(installed.priority), installed.name, match_text, archive_text, validation_text])
            item.setData(0, Qt.ItemDataRole.UserRole, self._details_text(installed, matches))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, installed.name)
            item.setData(0, Qt.ItemDataRole.UserRole + 2, installed)
            item.setData(0, Qt.ItemDataRole.UserRole + 3, matches)
            item.setData(0, Qt.ItemDataRole.UserRole + 10, installed.priority)
            self._tree.addTopLevelItem(item)
            if installed.name == current_mod_name:
                selected_item = item

        unmatched_entries = [
            entry
            for entry in self._build_entries
            if _build_entry_key(entry) not in matched_build_keys
        ]
        for entry in unmatched_entries:
            entry_key = _build_entry_key(entry)
            item = _NumericTreeWidgetItem(["", "(not installed)", entry.name, "", "No MO2 match"])
            item.setData(0, Qt.ItemDataRole.UserRole, self._unmatched_build_details_text(entry))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, f"build:{entry_key}")
            item.setData(0, Qt.ItemDataRole.UserRole + 4, entry)
            item.setData(0, Qt.ItemDataRole.UserRole + 10, 1_000_000)
            self._tree.addTopLevelItem(item)
            if f"build:{entry_key}" == current_mod_name:
                selected_item = item

        if selected_item is not None:
            self._tree.setCurrentItem(selected_item)
        elif self._tree.topLevelItemCount():
            self._tree.setCurrentItem(self._tree.topLevelItem(0))
        self._tree.verticalScrollBar().setValue(scroll_value)
        build_label = "build not fetched" if not self._build_entries else f"{len(self._build_entries)} build mods fetched"
        if self._validation_total and self._validation_done < self._validation_total:
            current = f": {self._validation_current_mod}" if self._validation_current_mod else ""
            build_label = f"validating {self._validation_done + 1}/{self._validation_total}{current}"
        unmatched_label = f" | {len(unmatched_entries)} unmatched build mods" if self._build_entries else ""
        self._summary_label.setText(f"{len(installed_mods)} installed mods | {build_label}{unmatched_label}")
        self._update_details()


    def _fetch_full_build(self):
        try:
            page_url = self._full_build_url()
            payload = fetch_kotor_repo_data(self._game.gameShortName(), self._FETCH_TIMEOUT_SECONDS)
            self._build_entries = parse_kotor_repo_build_entries(payload)
            for url, metadata in parse_kotor_repo_metadata(payload).items():
                if metadata.author:
                    self._author_by_url[url] = metadata.author
            self._save_cache()
            self.refresh()
            self._details.setPlainText(
                "\n".join(
                    [
                        f"Fetched {len(self._build_entries)} build mods.",
                        f"Source: {page_url}",
                    ]
                )
            )
        except Exception as exc:
            self._tree.clear()
            error_item = QTreeWidgetItem(["", "Fetch failed", str(exc), ""])
            error_item.setData(
                0,
                Qt.ItemDataRole.UserRole,
                "\n".join(
                    [
                        f"Unable to fetch the full build page for {self._game.gameName()}.",
                        "",
                        str(exc),
                    ]
                ),
            )
            self._tree.addTopLevelItem(error_item)
            self._tree.setCurrentItem(error_item)
            self._summary_label.setText("0 installed mods | fetch failed")
            self._update_details()


    def _update_details(self):
        item = self._tree.currentItem()
        self._details.setPlainText(str(item.data(0, Qt.ItemDataRole.UserRole) or "") if item else "")
        self._update_current_row_widgets(item)


    def _update_current_row_widgets(self, item: QTreeWidgetItem | None):
        if self._editor_item is not None and self._editor_item is not item:
            self._tree.removeItemWidget(self._editor_item, 2)
            self._tree.removeItemWidget(self._editor_item, 3)
            self._editor_item = None
        if item is None or self._tree.itemWidget(item, 2) is not None:
            return
        installed = item.data(0, Qt.ItemDataRole.UserRole + 2)
        matches = item.data(0, Qt.ItemDataRole.UserRole + 3)
        if not isinstance(installed, _InstalledModEntry) or not isinstance(matches, list):
            return
        self._tree.setItemWidget(item, 2, self._build_match_combo(installed, matches))
        self._tree.setItemWidget(item, 3, self._archive_combo(installed))
        self._editor_item = item


    def _build_match_combo(self, installed: _InstalledModEntry, matches: list[_BuildModEntry]) -> QWidget:
        widget = QWidget(self._tree)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo = QComboBox(widget)
        combo.setEditable(False)
        combo.setFixedHeight(22)
        combo.addItem("No manual match", "")
        sorted_entries = sorted(self._build_entries, key=lambda entry: entry.name.casefold())
        for entry in sorted_entries:
            combo.addItem(entry.name, _build_entry_key(entry))
        auto_key = _build_entry_key(matches[0]) if matches else ""
        selected_key = self._manual_build_match_by_mod.get(installed.name, "")
        if selected_key:
            index = combo.findData(selected_key)
            if index >= 0:
                combo.setCurrentIndex(index)
        elif auto_key:
            index = combo.findData(auto_key)
            if index >= 0:
                combo.setCurrentIndex(index)

        apply_btn = QPushButton("Apply", widget)
        apply_btn.setFixedHeight(22)
        apply_btn.setVisible(False)
        apply_btn.clicked.connect(
            lambda _checked=False, mod_name=installed.name, selector=combo, automatic_key=auto_key:
            self._apply_manual_match(mod_name, selector, automatic_key)
        )
        combo.currentIndexChanged.connect(
            lambda _index, mod_name=installed.name, selector=combo, button=apply_btn, automatic_key=auto_key:
            self._manual_match_changed(mod_name, selector, button, automatic_key)
        )
        layout.addWidget(combo, 1)
        layout.addWidget(apply_btn)
        return widget


    def _manual_match_changed(self, mod_name: str, combo: QComboBox, apply_btn: QPushButton, auto_key: str):
        key = str(combo.currentData() or "")
        effective_key = self._manual_build_match_by_mod.get(mod_name, "") or auto_key
        apply_btn.setVisible(key != effective_key)


    def _apply_manual_match(self, mod_name: str, combo: QComboBox, auto_key: str):
        key = str(combo.currentData() or "")
        if not key or key == auto_key:
            self._manual_build_match_by_mod.pop(mod_name, None)
        else:
            self._manual_build_match_by_mod[mod_name] = key
        entry = self._manual_build_entry_for_key(key) if key else None
        if entry is not None:
            self._write_mod_meta_for_build_match(mod_name, entry)
        self._save_cache()
        self.refresh()


    def _archive_combo(self, installed: _InstalledModEntry) -> QWidget:
        widget = QWidget(self._tree)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        combo = QComboBox(widget)
        combo.setEditable(False)
        combo.setFixedHeight(22)
        combo.addItem("No archive override", "")
        for installation_file, _meta_path, _meta in sorted(self._download_meta_entries(), key=lambda item: item[0].casefold()):
            combo.addItem(installation_file, installation_file)
        auto_key = installed.installation_file
        selected_key = self._manual_archive_by_mod.get(installed.name, "")
        if selected_key:
            index = combo.findData(selected_key)
            if index >= 0:
                combo.setCurrentIndex(index)
        elif auto_key:
            index = combo.findData(auto_key)
            if index >= 0:
                combo.setCurrentIndex(index)

        apply_btn = QPushButton("Apply", widget)
        apply_btn.setFixedHeight(22)
        apply_btn.setVisible(False)
        apply_btn.clicked.connect(
            lambda _checked=False, mod_name=installed.name, selector=combo, automatic_key=auto_key:
            self._apply_manual_archive(mod_name, selector, automatic_key)
        )
        combo.currentIndexChanged.connect(
            lambda _index, mod_name=installed.name, selector=combo, button=apply_btn, automatic_key=auto_key:
            self._manual_archive_changed(mod_name, selector, button, automatic_key)
        )
        layout.addWidget(combo, 1)
        layout.addWidget(apply_btn)
        return widget


    def _manual_archive_changed(self, mod_name: str, combo: QComboBox, apply_btn: QPushButton, auto_key: str):
        key = str(combo.currentData() or "")
        effective_key = self._manual_archive_by_mod.get(mod_name, "") or auto_key
        apply_btn.setVisible(key != effective_key)


    def _apply_manual_archive(self, mod_name: str, combo: QComboBox, auto_key: str):
        key = str(combo.currentData() or "")
        if not key or key == auto_key:
            self._manual_archive_by_mod.pop(mod_name, None)
        else:
            self._manual_archive_by_mod[mod_name] = key
        installed = next((item for item in self._installed_mod_entries() if item.name == mod_name), None)
        if installed is not None:
            matches = self._matching_build_entries(installed)
            if matches:
                self._write_mod_meta_for_build_match(mod_name, matches[0])
        self._save_cache()
        self.refresh()


    def _apply_all_meta(self):
        if not self._build_entries:
            QMessageBox.information(self, "Apply All Meta", "Fetch the build list before applying metadata.")
            return
        updated: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        for installed in self._installed_mod_entries():
            matches = self._matching_build_entries(installed)
            entry = matches[0] if matches else None
            if entry is None:
                missing.append(installed.name)
                continue
            if self._write_mod_meta_for_build_match(installed.name, entry):
                updated.append(installed.name)
            else:
                failed.append(installed.name)

        self._save_cache()
        self.refresh()
        detail_lines = [f"Updated {len(updated)} mod meta.ini file(s)."]
        if missing:
            detail_lines.extend(["", f"No build match for {len(missing)} mod(s):", *missing[:30]])
            if len(missing) > 30:
                detail_lines.append(f"... {len(missing) - 30} more")
        if failed:
            detail_lines.extend(["", f"Failed to update {len(failed)} mod(s):", *failed[:30]])
            if len(failed) > 30:
                detail_lines.append(f"... {len(failed) - 30} more")
        self._details.setPlainText("\n".join(detail_lines))


    def _installed_mod_entries(self) -> list[_InstalledModEntry]:
        self._ensure_download_meta_index()
        mods_root = Path(self._organizer.modsPath())
        ordered_names = list(self._organizer.modList().allModsByProfilePriority())
        installed: list[_InstalledModEntry] = []
        for priority, mod_name in enumerate(ordered_names):
            mod_path = mods_root / mod_name
            if mod_path.exists() and mod_path.is_dir():
                enabled = bool(self._organizer.modList().state(mod_name) & mobase.ModState.ACTIVE)
                installation_file, download_meta_path, download_meta = self._download_meta_info(mod_path)
                mo2_version, mo2_newest_version, mo2_nexus_id = self._mo2_mod_version_info(mod_name)
                installed.append(
                    _InstalledModEntry(
                        name=mod_name,
                        priority=priority,
                        enabled=enabled,
                        meta_candidates=(
                            [(installation_file, download_meta_path, download_meta)]
                            if installation_file and download_meta_path is not None and download_meta
                            else []
                        ),
                        urls=installed_mod_urls(download_meta, self._game.gameShortName()),
                        installation_file=installation_file,
                        download_meta_path=download_meta_path,
                        download_meta=download_meta,
                        mo2_version=mo2_version,
                        mo2_newest_version=mo2_newest_version,
                        mo2_nexus_id=mo2_nexus_id,
                    )
                )
        return installed


    def _mo2_mod_version_info(self, mod_name: str) -> tuple[str, str, str]:
        try:
            mod = self._organizer.modList().getMod(mod_name)
        except Exception:
            mod = None
        if mod is None:
            return "", "", ""

        version = self._safe_mod_value(mod, "version")
        newest_version = self._safe_mod_value(mod, "newestVersion")
        nexus_id = self._safe_mod_value(mod, "nexusId")
        return version, newest_version, nexus_id


    @staticmethod
    def _safe_mod_value(mod, name: str) -> str:
        try:
            value = getattr(mod, name)
            return str(value() if callable(value) else value).strip()
        except Exception:
            return ""


    def _on_tree_context_menu(self, pos: QPoint):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        mod_name = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
        if not mod_name:
            return

        menu = QMenu(self)
        download_action = menu.addAction("Download")
        validate_action = menu.addAction("Validate Mod")
        validate_action.setEnabled(self._nexus_validation_current is None)
        current_validation = self._validation_by_mod.get(mod_name)
        is_override = current_validation is not None and current_validation.summary == "OVERRIDE"
        override_action = menu.addAction("Clear Override" if is_override else "Override")
        download_action.setEnabled(self._download_url_for_item(item) != "")
        chosen_action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen_action is download_action:
            self._download_item(item)
        elif chosen_action is validate_action:
            self._validate_single_mod(mod_name)
        elif chosen_action is override_action:
            if is_override:
                self._validation_by_mod.pop(mod_name, None)
            else:
                self._validation_by_mod[mod_name] = _ValidationResult(
                    "OVERRIDE",
                    [
                        "Validation manually overridden by user.",
                        "This result is cached and will be kept until cleared or revalidated.",
                    ],
                )
            self._save_cache()
            self.refresh()


    def _download_item(self, item: QTreeWidgetItem):
        entry = self._download_entry_for_item(item)
        if entry is None or not entry.urls:
            return
        if self._download_process is not None or self._download_queue:
            return
        self._download_queue = [entry]
        self._download_total = 1
        self._set_download_running(True)
        self._details.setPlainText(f"Downloading 1 build URL.\n{entry.name}")
        self._process_next_build_download()


    def _download_url_for_item(self, item: QTreeWidgetItem) -> QUrl:
        entry = self._download_entry_for_item(item)
        if entry is not None and entry.urls:
            return QUrl(str(entry.urls[0]))
        return QUrl()


    def _download_entry_for_item(self, item: QTreeWidgetItem) -> _BuildModEntry | None:
        matches = item.data(0, Qt.ItemDataRole.UserRole + 3)
        if isinstance(matches, list) and matches:
            for entry in matches:
                urls = getattr(entry, "urls", [])
                if urls:
                    return entry
        build_entry = item.data(0, Qt.ItemDataRole.UserRole + 4)
        urls = getattr(build_entry, "urls", [])
        if urls:
            return build_entry
        return None


    def _selected_download_start_index(self) -> int:
        item = self._tree.currentItem()
        if item is None:
            return 0
        entry = self._download_entry_for_item(item)
        if entry is None:
            return 0
        entry_key = _build_entry_key(entry)
        for index, build_entry in enumerate(self._build_entries):
            if _build_entry_key(build_entry) == entry_key and build_entry.urls:
                return index
        return 0


    def _download_all_build_urls(self):
        if self._download_process is not None or self._download_queue:
            return
        start_index = self._selected_download_start_index()
        queue = [entry for entry in self._build_entries[start_index:] if entry.urls]
        if not queue:
            self._details.setPlainText("No fetched build URLs to download.")
            return
        self._download_queue = list(queue)
        self._download_total = len(queue)
        self._set_download_running(True)
        start_name = queue[0].name
        self._details.setPlainText(
            f"Downloading {self._download_total} fetched build URL(s) starting at:\n{start_name}"
        )
        self._process_next_build_download()


    def _set_download_running(self, running: bool):
        self._download_all_btn.setEnabled(not running)
        self._refresh_btn.setEnabled(not running)
        self._fetch_btn.setEnabled(not running)
        self._grab_authors_btn.setEnabled(not running)
        self._build_btn.setEnabled(not running)


    def _process_next_build_download(self):
        if self._download_process is not None or self._browser_waiting is not None:
            return
        if not self._download_queue:
            self._download_total = 0
            self._set_download_running(False)
            self._summary_label.setText(
                "0 installed mods"
                if self._tree.topLevelItemCount() == 0
                else self._summary_label.text()
            )
            self._details.appendPlainText("\nBuilder download queue complete.")
            return

        entry = self._download_queue.pop(0)
        current = self._download_total - len(self._download_queue)
        url = str(entry.urls[0]).strip()
        self._summary_label.setText(f"downloading {current}/{self._download_total}: {entry.name}")
        self._details.appendPlainText(f"\n[{current}/{self._download_total}] {entry.name}\n{url}")
        host = urlparse(url).netloc.lower()
        if "deadlystream.com" in host:
            if self._start_builder_deadlystream_download(entry, url):
                return
        self._start_builder_browser_download(entry, url, "Browser fallback", self._basic_download_meta_fields(entry))


    def _start_builder_deadlystream_download(self, entry: _BuildModEntry, url: str) -> bool:
        scraper = self._deadly_scraper_exe()
        if not scraper.exists():
            self._details.appendPlainText(f"DeadlyScraper.exe not found: {scraper}")
            return False
        self._download_existing_names = {
            path.name for path in Path(self._organizer.downloadsPath()).iterdir() if path.is_file()
        }
        current = self._download_total - len(self._download_queue)
        self._summary_label.setText(f"downloading {current}/{self._download_total}: {entry.name}")
        self._details.appendPlainText(f"Starting DeadlyScraper download to {self._organizer.downloadsPath()}")
        process = QProcess(self)
        self._download_process = process
        process.finished.connect(
            lambda code, _status, entry=entry, process=process: self._finish_builder_deadlystream_download(entry, process, code)
        )
        process.start(str(scraper), [url, "--download", str(Path(self._organizer.downloadsPath()))])
        return True


    def _finish_builder_deadlystream_download(self, entry: _BuildModEntry, process: QProcess, exit_code: int):
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        self._download_process = None
        downloaded_names = self._new_builder_download_names()
        self._rename_builder_deadlystream_download(entry, downloaded_names)
        success = exit_code == 0 and bool(downloaded_names)
        if stdout:
            self._details.appendPlainText(stdout)
        if stderr:
            self._details.appendPlainText(stderr)
        if success:
            self._write_builder_deadlystream_meta(entry, downloaded_names, stdout)
            self._download_meta_entries_cache = None
            self._summary_label.setText(f"downloaded {entry.name}")
            self._details.appendPlainText(f"Finished download: {entry.name}")
        else:
            url = str(entry.urls[0]).strip() if entry.urls else ""
            self._summary_label.setText(f"download failed: {entry.name}")
            self._details.appendPlainText(f"DeadlyScraper download failed for: {entry.name}")
            if url:
                metadata = self._deadlystream_meta_fields_from_stdout(entry, stdout)
                self._start_builder_browser_download(entry, url, "DeadlyScraper fallback", metadata)
                return
        QTimer.singleShot(0, self._process_next_build_download)

    def _write_builder_deadlystream_meta(self, entry: _BuildModEntry, downloaded_names: list[str], stdout: str):
        if not downloaded_names:
            return
        fields = self._deadlystream_meta_fields_from_stdout(entry, stdout)
        archive_name = downloaded_names[0]
        fields["name"] = Path(archive_name).stem
        write_download_meta(Path(self._organizer.downloadsPath()), archive_name, fields)

    def _deadlystream_meta_fields_from_stdout(self, entry: _BuildModEntry, stdout: str) -> dict[str, str]:
        payload = self._deadlystream_payload_from_stdout(stdout)
        url = str(entry.urls[0]).strip() if entry.urls else ""
        source_url = str(payload.get("SourceUrl", "")).strip() or url
        title = (
            str(payload.get("Title", "")).strip()
            or str(payload.get("Name", "")).strip()
            or str(payload.get("ModName", "")).strip()
        )
        author = str(payload.get("Author") or payload.get("author") or "").strip()
        version = str(
            payload.get("SelectedVersion")
            or payload.get("LatestVersion")
            or payload.get("CurrentVersion")
            or ""
        ).strip()
        newest_version = str(payload.get("LatestVersion") or payload.get("CurrentVersion") or version).strip()
        current_version_release_date = str(
            payload.get("SelectedVersionReleaseDate")
            or payload.get("LatestVersionReleaseDate")
            or payload.get("CurrentVersionReleaseDate")
            or ""
        ).strip()
        submitted_date = str(payload.get("OriginalUploadDate") or payload.get("SubmittedDate") or "").strip()
        published_date = str(payload.get("PublishedDate", "")).strip()
        effective_date = str(payload.get("EffectiveDate") or payload.get("UpdatedDate") or "").strip()
        release_date = current_version_release_date or published_date or submitted_date or effective_date
        return {
            "installed": "false",
            "uninstalled": "false",
            "gameName": self._game.gameShortName().lower(),
            "modName": entry.name,
            "version": version,
            "newestVersion": newest_version,
            "manualURL": source_url,
            "url": source_url,
            "repository": "DeadlyStream",
            self._ARCHIVE_RELEASE_DATE_FIELD: release_date,
            "author": author,
            "Title": title,
            "SourceUrl": source_url,
            "DownloadPageUrl": str(payload.get("DownloadPageUrl", "")).strip(),
        }

    @staticmethod
    def _deadlystream_payload_from_stdout(stdout: str) -> dict:
        try:
            payload = json.loads(stdout) if stdout else {}
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        if isinstance(payload.get("Metadata"), dict):
            metadata = dict(payload.get("Metadata") or {})
            downloads = payload.get("Downloads")
            download = downloads[0] if isinstance(downloads, list) and downloads else payload.get("Download")
            if isinstance(download, dict):
                for key in ("AvailableDownloads", "FileName", "FilePath", "FinalUrl"):
                    if key not in metadata and key in download:
                        metadata[key] = download[key]
            return metadata
        return payload

    def _basic_download_meta_fields(self, entry: _BuildModEntry) -> dict[str, str]:
        url = str(entry.urls[0]).strip() if entry.urls else ""
        host = urlparse(url).netloc.casefold()
        repository = ""
        if "nexusmods.com" in host:
            repository = "Nexus"
        elif "deadlystream.com" in host:
            repository = "DeadlyStream"
        return {
            "installed": "false",
            "uninstalled": "false",
            "gameName": self._game.gameShortName().lower(),
            "modName": entry.name,
            "manualURL": url,
            "url": url,
            "repository": repository,
            "author": self._author_by_url.get(_canonical_url(url), ""),
            "modID": nexus_mod_id_from_url(url) if repository == "Nexus" else "",
            "fileID": nexus_file_id_from_url(url) if repository == "Nexus" else "",
        }


    def _new_builder_download_names(self) -> list[str]:
        current_names = {
            path.name for path in Path(self._organizer.downloadsPath()).iterdir() if path.is_file()
        }
        return sorted(name for name in current_names if name not in self._download_existing_names)

    def _start_builder_browser_download(self, entry: _BuildModEntry, url: str, reason: str, metadata: dict[str, str]):
        downloads_path = Path(self._organizer.downloadsPath())
        edge = self._edge_path()
        if edge is None:
            self._details.appendPlainText(f"{reason}: Edge not found; opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_build_download)
            return
        profile_dir = Path(self._organizer.profilePath()) / "builder_edge_profile"
        self._write_edge_preferences(profile_dir, downloads_path)
        try:
            self._browser_process = subprocess.Popen(
                [
                    str(edge),
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--disable-sync",
                    "--new-window",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self._details.appendPlainText(f"{reason}: Edge launch failed ({exc}); opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_build_download)
            return

        existing_names = {path.name for path in downloads_path.iterdir() if path.is_file()}
        self._browser_waiting = (
            entry,
            downloads_path,
            reason,
            datetime.now().timestamp() + 900,
            url,
            existing_names,
            dict(metadata),
        )
        current = self._download_total - len(self._download_queue)
        self._summary_label.setText(f"waiting for browser download {current}/{self._download_total}: {entry.name}")
        self._details.appendPlainText(f"{reason}: opened sandboxed Edge profile. Waiting for archive download.")
        QTimer.singleShot(2000, self._poll_builder_browser_download)

    def _poll_builder_browser_download(self):
        if self._browser_waiting is None:
            return
        entry, downloads_path, reason, deadline, url, existing_names, metadata = self._browser_waiting
        detected_path = self._detect_builder_browser_download(downloads_path, existing_names)
        if detected_path is not None:
            self._browser_waiting = None
            self._finish_builder_browser_download(entry, detected_path, reason, metadata)
            QTimer.singleShot(2500, self._close_builder_browser_process)
            QTimer.singleShot(2600, self._process_next_build_download)
            return
        if datetime.now().timestamp() >= deadline:
            self._browser_waiting = None
            self._close_builder_browser_process()
            self._details.appendPlainText(f"{reason}: sandboxed Edge did not finish within 15 minutes; opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_build_download)
            return
        QTimer.singleShot(2000, self._poll_builder_browser_download)

    def _detect_builder_browser_download(self, downloads_path: Path, existing_names: set[str]) -> Path | None:
        new_files = [
            path
            for path in downloads_path.iterdir()
            if path.is_file()
            and path.name not in existing_names
            and not self._is_incomplete_download_name(path.name)
        ]
        if len(new_files) != 1:
            return None
        return new_files[0]

    @staticmethod
    def _is_incomplete_download_name(name: str) -> bool:
        lower_name = name.casefold()
        return (
            lower_name.endswith(".crdownload")
            or lower_name.endswith(".tmp")
            or lower_name.endswith(".meta")
            or lower_name.endswith(".part")
            or lower_name.endswith(".partial")
            or lower_name.endswith(".download")
            or lower_name.endswith(".opdownload")
            or lower_name.endswith(".unfinished")
        )

    def _finish_builder_browser_download(self, entry: _BuildModEntry, archive_path: Path, reason: str, metadata: dict[str, str]):
        archive_name = archive_path.name
        if metadata:
            metadata = dict(metadata)
            metadata["name"] = Path(archive_name).stem
            write_download_meta(Path(self._organizer.downloadsPath()), archive_name, metadata)
            self._download_meta_entries_cache = None
        self._summary_label.setText(f"downloaded {entry.name}")
        self._details.appendPlainText(f"{reason}: browser download detected for {entry.name} -> {archive_name}")

    def _close_builder_browser_process(self):
        process = self._browser_process
        self._browser_process = None
        if process is not None and process.poll() is None:
            process.terminate()

    @staticmethod
    def _edge_path() -> Path | None:
        for candidate in (
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ):
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _write_edge_preferences(profile_dir: Path, downloads_path: Path):
        default_dir = profile_dir / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        prefs_path = default_dir / "Preferences"
        prefs = {}
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            except Exception:
                prefs = {}
        prefs.setdefault("download", {})
        prefs["download"]["default_directory"] = str(downloads_path)
        prefs["download"]["prompt_for_download"] = False
        prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    def _rename_builder_deadlystream_download(self, entry: _BuildModEntry, downloaded_names: list[str]):
        expected_name = self._expected_builder_archive_name(entry)
        if not expected_name:
            return
        downloads_path = Path(self._organizer.downloadsPath())
        for name in list(downloaded_names):
            source_path = downloads_path / name
            if not source_path.exists() or name == expected_name:
                continue
            target_path = downloads_path / expected_name
            if target_path.exists():
                return
            try:
                source_path.rename(target_path)
                meta_path = downloads_path / f"{name}.meta"
                if meta_path.exists():
                    meta_path.rename(downloads_path / f"{expected_name}.meta")
                downloaded_names.remove(name)
                downloaded_names.append(expected_name)
                downloaded_names.sort()
                return
            except Exception:
                return

    def _expected_builder_archive_name(self, entry: _BuildModEntry) -> str:
        for item in self._installed_mod_entries():
            for match in self._matching_build_entries(item):
                if _build_entry_key(match) != _build_entry_key(entry):
                    continue
                if not item.installation_file:
                    return ""
                return str(item.installation_file or "").strip()
        return ""

    def _initial_build_mods_from_downloads(self) -> list[InitialBuildMod]:
        if not self._build_entries:
            return []
        mods: list[InitialBuildMod] = []
        used_archives: set[str] = set()
        for index, entry in enumerate(self._build_entries, start=1):
            archive_entry = self._best_download_meta_entry_for_build_entry(entry, used_archives)
            if archive_entry is None:
                continue
            archive_name, _meta_path, meta = archive_entry
            used_archives.add(archive_name)
            url = str(entry.urls[0]).strip() if entry.urls else ""
            repository = str(meta.get("repository", "")).strip()
            if not repository:
                host = urlparse(url).netloc.casefold()
                if "nexusmods.com" in host:
                    repository = "Nexus"
                elif "deadlystream.com" in host:
                    repository = "DeadlyStream"
            mods.append(
                InitialBuildMod(
                    priority=index,
                    mod_name=entry.name,
                    archive_name=archive_name,
                    url=url,
                    version=str(meta.get("version", "")).strip() or str(meta.get("newestVersion", "")).strip(),
                    release_date=str(meta.get(self._ARCHIVE_RELEASE_DATE_FIELD, "")).strip(),
                    author=(
                        self._author_by_url.get(_canonical_url(url), "")
                        or str(meta.get("author", "")).strip()
                    ),
                    repository=repository,
                    mod_id=str(meta.get("modID", "")).strip() or nexus_mod_id_from_url(url),
                    file_id=str(meta.get("fileID", "")).strip(),
                    enabled=True,
                )
            )
        return mods

    def _best_download_meta_entry_for_build_entry(
        self,
        entry: _BuildModEntry,
        used_archives: set[str],
    ) -> tuple[str, Path, dict[str, str]] | None:
        entry_urls = {_canonical_url(url) for url in entry.urls if _canonical_url(url)}
        entry_mod_ids = {mod_id for url in entry.urls if (mod_id := nexus_mod_id_from_url(url))}
        candidates: list[tuple[int, tuple[str, Path, dict[str, str]]]] = []
        for archive_name, meta_path, meta in self._download_meta_entries():
            if archive_name in used_archives:
                continue
            score = 0
            meta_urls = installed_mod_urls(meta, self._game.gameShortName())
            meta_canonicals = {_canonical_url(url) for url in meta_urls if _canonical_url(url)}
            if entry_urls and meta_canonicals & entry_urls:
                score += 10
            meta_mod_id = str(meta.get("modID", "")).strip()
            if meta_mod_id and meta_mod_id in entry_mod_ids:
                score += 5
            meta_name = str(meta.get("modName", "")).strip().casefold()
            if meta_name and meta_name == entry.name.casefold():
                score += 1
            if score > 0:
                candidates.append((score, (archive_name, meta_path, meta)))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1][0].casefold()))
        return candidates[0][1]


    def _download_meta_info(self, mod_path: Path) -> tuple[str, Path | None, dict[str, str]]:
        self._ensure_download_meta_index()
        return download_meta_info(mod_path, Path(self._organizer.downloadsPath()))


    def _ensure_download_meta_index(self):
        if self._download_meta_entries_cache is not None:
            return
        self._download_meta_entries_cache = load_download_meta_entries(Path(self._organizer.downloadsPath()))


    def _validate_matches(self):
        self._start_validation(force_release_update=False)


    def _force_release_date_update(self):
        self._start_validation(force_release_update=True)


    def _grab_authors(self):
        if (
            self._author_process is not None
            or self._author_queue
            or self._download_process is not None
            or self._download_queue
            or self._validation_queue
            or self._build_thread is not None
        ):
            return
        scraper = self._deadly_scraper_exe()
        if not scraper.exists():
            QMessageBox.warning(self, "Grab Authors", f"DeadlyScraper.exe not found:\n{scraper}")
            return

        queue: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in self._build_entries:
            for raw_url in entry.urls:
                url = str(raw_url).strip()
                canonical = _canonical_url(url)
                if "deadlystream.com" not in urlparse(url).netloc.casefold() or not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                queue.append((entry.name, url))
        for installed in self._installed_mod_entries():
            for url in installed.urls:
                canonical = _canonical_url(url)
                if "deadlystream.com" not in urlparse(url).netloc.casefold() or not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                queue.append((installed.name, url))

        if not queue:
            QMessageBox.information(self, "Grab Authors", "No DeadlyStream URLs are available.")
            return

        self._author_queue = queue
        self._author_total = len(queue)
        self._author_updated = 0
        self._author_failed = 0
        self._set_author_running(True)
        self._details.setPlainText(f"Grabbing authors for {self._author_total} DeadlyStream mod(s).")
        self._process_next_author()


    def _set_author_running(self, running: bool):
        self._refresh_btn.setEnabled(not running)
        self._fetch_btn.setEnabled(not running)
        self._download_all_btn.setEnabled(not running)
        self._validate_btn.setEnabled(not running)
        self._force_release_btn.setEnabled(not running)
        self._grab_authors_btn.setEnabled(not running)
        self._apply_all_meta_btn.setEnabled(not running)
        self._build_btn.setEnabled(not running)
        self._stop_validate_btn.setEnabled(False)


    def _process_next_author(self):
        if self._author_process is not None:
            return
        if not self._author_queue:
            self._save_cache()
            self._set_author_running(False)
            self.refresh()
            refresh_mo2(self._organizer, self)
            self._summary_label.setText(
                f"Authors updated: {self._author_updated}; failed: {self._author_failed}"
            )
            self._details.appendPlainText(
                f"\nAuthor lookup complete. Updated {self._author_updated}; failed {self._author_failed}."
            )
            self._author_total = 0
            return

        name, url = self._author_queue.pop(0)
        current = self._author_total - len(self._author_queue)
        self._summary_label.setText(f"grabbing author {current}/{self._author_total}: {name}")
        process = QProcess(self)
        self._author_process = process
        process.finished.connect(
            lambda code, _status, name=name, url=url, process=process:
            self._finish_author_lookup(name, url, process, code)
        )
        process.start(str(self._deadly_scraper_exe()), [url])


    def _finish_author_lookup(self, name: str, url: str, process: QProcess, exit_code: int):
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        self._author_process = None
        process.deleteLater()
        payload = self._deadlystream_payload_from_stdout(stdout)
        author = str(payload.get("Author") or payload.get("author") or "").strip()
        if exit_code == 0 and author:
            self._store_author(url, author)
            self._author_updated += 1
            self._details.appendPlainText(f"\n{name}: {author}")
        else:
            self._author_failed += 1
            detail = stderr or "DeadlyScraper returned no author."
            self._details.appendPlainText(f"\n{name}: author lookup failed. {detail}")
        QTimer.singleShot(0, self._process_next_author)


    def _store_author(self, url: str, author: str):
        canonical = _canonical_url(url)
        if not canonical or not author:
            return
        self._author_by_url[canonical] = author
        for _archive_name, meta_path, meta in self._download_meta_entries():
            meta_urls = installed_mod_urls(meta, self._game.gameShortName())
            if canonical not in {_canonical_url(candidate) for candidate in meta_urls}:
                continue
            if write_download_meta_field(meta_path, "author", author):
                meta["author"] = author
        mods_root = Path(self._organizer.modsPath())
        for installed in self._installed_mod_entries():
            installed_urls = {_canonical_url(candidate) for candidate in installed.urls}
            for entry in self._matching_build_entries(installed):
                installed_urls.update(_canonical_url(candidate) for candidate in entry.urls)
            if canonical not in installed_urls:
                continue
            write_download_meta_field(mods_root / installed.name / "meta.ini", "author", author)


    def _validate_single_mod(self, mod_name: str):
        self._start_validation(force_release_update=False, mod_names={mod_name}, clear_existing_results=False)


    def _start_validation(
        self,
        force_release_update: bool,
        mod_names: set[str] | None = None,
        clear_existing_results: bool = True,
    ):
        if self._nexus_validation_current is not None:
            return
        self._validation_force_release_update = force_release_update
        self._validation_stop_requested = False
        if clear_existing_results:
            self._validation_by_mod = {}
            self._save_cache()
        installed_mods = self._installed_mod_entries()
        self._validation_queue = []
        for installed in installed_mods:
            if mod_names is not None and installed.name not in mod_names:
                continue
            if not clear_existing_results:
                self._validation_by_mod.pop(installed.name, None)
            matches = self._matching_build_entries(installed)
            if not matches:
                if mod_names is not None:
                    self._append_validation(installed.name, _ValidationResult("NO MATCH", ["No full build URL match found for this installed mod."]))
                continue
            for entry in matches:
                url = entry.urls[0] if entry.urls else ""
                host = urlparse(url).netloc.lower()
                if force_release_update and "deadlystream.com" not in host:
                    continue
                self._validation_queue.append((installed, entry))
        self._kotor_repo_by_url = {}
        self._kotor_repo_error = ""
        if any(
            "deadlystream.com" in urlparse(entry.urls[0]).netloc.lower()
            for _installed, entry in self._validation_queue
            if entry.urls
        ):
            try:
                self._kotor_repo_by_url = fetch_kotor_repo_metadata(
                    self._game.gameShortName(),
                    self._FETCH_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                self._kotor_repo_error = str(exc)
        self._validation_total = len(self._validation_queue)
        self._validation_done = 0
        self._validation_current_mod = ""
        self._set_validation_running(True)
        self.refresh()
        self._process_next_validation()


    def _set_validation_running(self, running: bool):
        build_running = self._build_thread is not None
        self._refresh_btn.setEnabled(not running)
        self._fetch_btn.setEnabled(not running)
        self._validate_btn.setEnabled(not running)
        self._force_release_btn.setEnabled(not running)
        self._grab_authors_btn.setEnabled(not running)
        self._apply_all_meta_btn.setEnabled(not running)
        self._build_btn.setEnabled(not running and not build_running)
        self._stop_validate_btn.setEnabled(running)


    def _set_build_running(self, running: bool):
        self._refresh_btn.setEnabled(not running)
        self._fetch_btn.setEnabled(not running)
        self._validate_btn.setEnabled(not running)
        self._force_release_btn.setEnabled(not running)
        self._grab_authors_btn.setEnabled(not running)
        self._apply_all_meta_btn.setEnabled(not running)
        self._build_btn.setEnabled(not running)
        self._stop_validate_btn.setEnabled(False)
        if running:
            self._summary_label.setText("Building instruction set...")


    def _build_instruction_set(self):
        self._prepare_for_build()
        installed_mods = self._installed_mod_entries()
        valid_mods: list[BuildInputMod] = []
        invalid_mods: list[str] = []
        for installed in installed_mods:
            validation = self._validation_by_mod.get(installed.name)
            if not self._is_build_valid(validation):
                invalid_mods.append(installed.name)
                continue
            build_entry = self._manual_build_entry_for_mod(installed.name)
            build_url = build_entry.urls[0] if build_entry and build_entry.urls else (installed.urls[0] if installed.urls else "")
            build_mod_id = installed.download_meta.get("modID", "").strip() or nexus_mod_id_from_url(build_url)
            archive_path = self._download_meta_path(installed.installation_file) if installed.installation_file else None
            archive_file = archive_path.with_suffix("") if archive_path is not None else None
            valid_mods.append(
                BuildInputMod(
                    priority=installed.priority,
                    mod_name=installed.name,
                    archive_name=installed.installation_file,
                    archive_path=archive_file if archive_file and archive_file.exists() else None,
                    version=self._current_archive_version(installed),
                    release_date=installed.download_meta.get(self._ARCHIVE_RELEASE_DATE_FIELD, "").strip(),
                    author=(
                        self._author_by_url.get(_canonical_url(build_url), "")
                        or installed.download_meta.get("author", "").strip()
                    ),
                    url=build_url,
                    mod_path=Path(self._organizer.modsPath()) / installed.name,
                    repository=installed.download_meta.get("repository", "").strip(),
                    mod_id=build_mod_id,
                    file_id=installed.download_meta.get("fileID", "").strip(),
                    enabled=installed.enabled,
                )
            )

        if invalid_mods:
            response = QMessageBox.warning(
                self,
                "Build Skipping Invalid Mods",
                "\n".join(
                    [
                        f"{len(invalid_mods)} mod(s) are not valid and will be skipped.",
                        "",
                        *invalid_mods[:30],
                        *(["..."] if len(invalid_mods) > 30 else []),
                        "",
                        "Continue building with valid mods only?",
                    ]
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return

        if not valid_mods:
            initial_mods = self._initial_build_mods_from_downloads()
            if not initial_mods:
                QMessageBox.information(self, "Build", "No valid installed mods or matched downloaded archives are available to build.")
                return
            output_path = self._default_build_output_path()
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "Save Initial KSON Instruction Set",
                str(output_path),
                "KSON Files (*.kson);;JSON Files (*.json);;All Files (*)",
            )
            if not selected:
                return
            try:
                result = build_initial_instruction_set(self._game.gameShortName(), Path(selected), initial_mods)
            except Exception as exc:
                self._details.setPlainText(f"Initial KSON build failed:\n{exc}")
                QMessageBox.critical(self, "Initial KSON Failed", str(exc))
                return
            self._details.setPlainText(
                "\n".join(
                    [
                        f"Wrote {result.mod_count} initial mod(s) to:",
                        str(result.output_path),
                        "",
                        "This KSON was generated from downloaded archive metadata and contains no installed-file actions yet.",
                    ]
                )
            )
            return

        output_path = self._default_build_output_path()
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save KSON Instruction Set",
            str(output_path),
            "KSON Files (*.kson);;JSON Files (*.json);;All Files (*)",
        )
        if not selected:
            return

        self._start_build_worker(Path(selected), valid_mods)


    def _prepare_for_build(self):
        patcher_tab = getattr(self._game, "_patcher_tab", None)
        clear_generated_patcher_mod = getattr(patcher_tab, "clear_generated_patcher_mod", None)
        if callable(clear_generated_patcher_mod):
            clear_generated_patcher_mod()

        texture_tab = getattr(self._game, "_texture_tab", None)
        run_unhide_all_for_build = getattr(texture_tab, "run_unhide_all_for_build", None)
        if callable(run_unhide_all_for_build):
            run_unhide_all_for_build()


    def _start_build_worker(self, output_path: Path, valid_mods: list[BuildInputMod]):
        if self._build_thread is not None:
            return
        thread = QThread(self)
        worker = _BuildWorker(
            self._game.gameShortName(),
            output_path,
            valid_mods,
            Path(self._organizer.profilePath()) / "tslpatch_order.json",
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._update_build_progress)
        worker.finished.connect(self._finish_build)
        worker.failed.connect(self._fail_build)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_build_worker)
        self._build_thread = thread
        self._build_worker = worker
        self._build_progress_lines = []
        self._set_build_running(True)
        thread.start()


    def _update_build_progress(self, current: int, total: int, name: str, status: str):
        self._summary_label.setText(f"Building {current}/{total}: {name}")
        line = f"[{current}/{total}] {name}: {status}"
        self._build_progress_lines.append(line)
        self._build_progress_lines = self._build_progress_lines[-80:]
        self._details.setPlainText("\n".join(self._build_progress_lines))
        self._details.verticalScrollBar().setValue(self._details.verticalScrollBar().maximum())


    def _finish_build(self, result):
        details = [
            f"Wrote {result.mod_count} mod(s) to:",
            str(result.output_path),
        ]
        if result.skipped:
            details.extend(["", "Skipped during export:", *result.skipped[:20]])
        if result.warnings:
            details.extend(["", "Warnings:", *result.warnings[:20]])
        self._details.setPlainText("\n".join(details))
        QMessageBox.information(self, "Build Complete", "\n".join(details[:8]))


    def _fail_build(self, message: str):
        self._details.setPlainText(f"Build failed:\n{message}")
        QMessageBox.critical(self, "Build Failed", message)


    def _clear_build_worker(self):
        self._build_thread = None
        self._build_worker = None
        self._set_build_running(False)
        self.refresh()


    @staticmethod
    def _is_build_valid(validation: _ValidationResult | None) -> bool:
        if validation is None:
            return False
        return any(part in {"VALID", "VALID URL", "OVERRIDE", "DATE FORCED", "DATE OK"} for part in validation.summary.split(" | "))


    @staticmethod
    def _current_archive_version(installed: _InstalledModEntry) -> str:
        version = installed.download_meta.get("version", "").strip()
        if version and version != "0.0.0.0":
            return version
        newest = installed.download_meta.get("newestVersion", "").strip()
        return newest if newest != "0.0.0.0" else ""


    def _default_build_output_path(self) -> Path:
        filename = f"{self._game.gameShortName().lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.kson"
        try:
            profile_path = Path(self._organizer.profilePath())
            if profile_path.exists():
                return profile_path / filename
        except Exception:
            pass
        return Path(__file__).resolve().parent / filename


    @staticmethod
    def _deadly_scraper_exe() -> Path:
        return Path(__file__).resolve().parent.parent / "DeadlyScraper.exe"


    def _stop_validation(self):
        self._validation_stop_requested = True
        self._validation_queue = []
        self._nexus_validation_current = None
        self._stop_nexus_timer()
        self._validation_total = 0
        self._validation_done = 0
        self._validation_current_mod = ""
        self._validation_force_release_update = False
        self._kotor_repo_by_url = {}
        self._kotor_repo_error = ""
        self._set_validation_running(False)
        self.refresh()


    def _process_next_validation(self):
        if self._validation_stop_requested or not self._validation_queue:
            self._validation_total = 0
            self._validation_done = 0
            self._validation_current_mod = ""
            self._validation_force_release_update = False
            self._kotor_repo_by_url = {}
            self._kotor_repo_error = ""
            self._validation_stop_requested = False
            self._set_validation_running(False)
            self.refresh()
            return

        installed, entry = self._validation_queue.pop(0)
        self._validation_current_mod = installed.name
        self._update_validation_status_label()
        url = entry.urls[0] if entry.urls else ""
        host = urlparse(url).netloc.lower()
        if "deadlystream.com" in host:
            metadata = self._kotor_repo_by_url.get(_canonical_url(url))
            if metadata is None:
                game = "k2" if self._game.gameShortName().lower() == "kotor2" else "k1"
                if self._kotor_repo_error:
                    summary = "KOTOR REPO ERROR"
                    details = [self._kotor_repo_error]
                else:
                    summary = "KOTOR REPO MISS"
                    details = [f"No KotorRepo metadata found for {url}"]
                details.append(f"Source: {KOTOR_REPO_DATA_URL.format(game=game)}")
                self._append_validation(installed.name, _ValidationResult(summary, details))
            else:
                self._append_validation(installed.name, self._validate_deadlystream_match(installed, entry, metadata))

        elif "nexusmods.com" in host:
            self._start_nexus_validation(installed, entry)
            return
        else:
            self._append_validation(installed.name, _ValidationResult("VALID URL", [f"Matched non-validated URL: {url}"]))
        self._validation_done += 1
        self._update_validation_status_label()
        QTimer.singleShot(0, self._process_next_validation)


    def _update_validation_status_label(self):
        if self._validation_total and self._validation_done < self._validation_total:
            current = f": {self._validation_current_mod}" if self._validation_current_mod else ""
            self._summary_label.setText(f"validating {self._validation_done + 1}/{self._validation_total}{current}")


    def _append_validation(self, mod_name: str, result: _ValidationResult):
        current = self._validation_by_mod.get(mod_name)
        if current is None:
            self._validation_by_mod[mod_name] = result
            self._save_cache()
            return
        summary = f"{current.summary} | {result.summary}"
        details = [*current.details, "", *result.details]
        self._validation_by_mod[mod_name] = _ValidationResult(summary, details)
        self._save_cache()


    def _init_nexus_bridge(self):
        try:
            self._nexus_bridge = self._organizer.createNexusBridge()
        except Exception:
            self._nexus_bridge = None
            self._nexus_signal_count = 0
            return

        self._connect_nexus_signal("fileInfoAvailable", self._on_nexus_file_info_available)
        self._connect_nexus_signal("requestFailed", self._on_nexus_request_failed)


    def _connect_nexus_signal(self, signal_name: str, handler):
        for source in self._nexus_signal_sources():
            try:
                getattr(source, signal_name).connect(handler)
                self._nexus_signal_count += 1
                return
            except Exception:
                pass


    def _nexus_signal_sources(self) -> list:
        sources = []
        if self._nexus_bridge is not None:
            sources.append(self._nexus_bridge)
            try:
                sources.append(self._nexus_bridge._object())
            except Exception:
                pass
        return sources


    def _start_nexus_validation(self, installed: _InstalledModEntry, entry: _BuildModEntry):
        nexus_meta = self._nexus_meta_for_entry(installed, entry)
        mod_id = nexus_meta.get("modID", "").strip()
        file_id = nexus_meta.get("fileID", "").strip()
        game_name = nexus_meta.get("gameName", "").strip().lower() or self._nexus_game_name()
        if (
            not self._nexus_bridge
            or self._nexus_signal_count <= 0
            or not mod_id.isdigit()
            or int(mod_id) <= 0
            or not file_id.isdigit()
            or int(file_id) <= 0
        ):
            self._append_validation(
                installed.name,
                self._validate_nexus_match(
                    installed,
                    entry,
                    None,
                    "MO2 Nexus file-info request unavailable or archive meta has fileID=0; URL match accepted.",
                ),
            )
            self._validation_done += 1
            self.refresh()
            QTimer.singleShot(0, self._process_next_validation)
            return

        self._nexus_validation_current = (installed, entry)
        self._nexus_last_request = f"game={game_name}, modID={mod_id}, fileID={file_id}, signals={self._nexus_signal_count}"
        self._start_nexus_timer()
        try:
            self._nexus_bridge.requestFileInfo(game_name, int(mod_id), int(file_id), installed.name)
        except Exception as exc:
            self._nexus_validation_current = None
            self._stop_nexus_timer()
            self._append_validation(
                installed.name,
                _ValidationResult(
                    "NEXUS ERROR",
                    [
                        f"Failed to request Nexus file info for modID {mod_id}, fileID {file_id}.",
                        f"Game name: {game_name}",
                        str(exc),
                    ],
                ),
            )
            self._validation_done += 1
            self.refresh()
            QTimer.singleShot(0, self._process_next_validation)


    def _nexus_game_name(self) -> str:
        try:
            value = self._game.gameNexusName()
            if value:
                return str(value).strip().lower()
        except Exception:
            pass
        return self._game.gameShortName().lower()


    def _nexus_meta_for_entry(self, installed: _InstalledModEntry, entry: _BuildModEntry) -> dict[str, str]:
        entry_mod_ids = [mod_id for url in entry.urls if (mod_id := nexus_mod_id_from_url(url))]
        candidates = list(installed.meta_candidates)
        for _archive, _path, meta in candidates:
            mod_id = str(meta.get("modID", "")).strip()
            file_id = str(meta.get("fileID", "")).strip()
            if mod_id in entry_mod_ids and file_id.isdigit() and int(file_id) > 0:
                return meta
        for _archive, _path, meta in candidates:
            if str(meta.get("modID", "")).strip() in entry_mod_ids:
                return meta
        return installed.download_meta


    def _on_nexus_file_info_available(self, *args):
        self._nexus_last_payload = describe_callback_args(args)
        self._finish_nexus_validation(extract_nexus_file_info_payload(args))


    def _on_nexus_request_failed(self, *args):
        current = self._nexus_validation_current
        if current is None:
            return
        installed, _entry = current
        self._nexus_validation_current = None
        self._stop_nexus_timer()
        if self._validation_stop_requested:
            return
        self._append_validation(
            installed.name,
            _ValidationResult(
                "NEXUS ERROR",
                [
                    f"Nexus request failed: {args}",
                    f"Request: {self._nexus_last_request or '(unknown)'}",
                    f"Callback argument types: {describe_callback_args(args)}",
                ],
            ),
        )
        self._validation_done += 1
        self.refresh()
        self._continue_validation_queue()


    def _start_nexus_timer(self):
        self._stop_nexus_timer()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_nexus_timeout)
        self._nexus_validation_timer = timer
        timer.start(self._NEXUS_TIMEOUT_SECONDS * 1000)


    def _stop_nexus_timer(self):
        if self._nexus_validation_timer is not None:
            self._nexus_validation_timer.stop()
            self._nexus_validation_timer.deleteLater()
            self._nexus_validation_timer = None


    def _finish_nexus_validation(self, files: list):
        current = self._nexus_validation_current
        if current is None:
            return
        installed, entry = current
        self._nexus_validation_current = None
        self._stop_nexus_timer()
        if self._validation_stop_requested:
            return
        self._append_validation(
            installed.name,
            self._validate_nexus_match(
                installed,
                entry,
                files,
                f"MO2 Nexus requestFileInfo result. Payload: {self._nexus_last_payload or '(unknown)'}",
            ),
        )
        self._validation_done += 1
        self.refresh()
        self._continue_validation_queue()


    def _on_nexus_timeout(self):
        current = self._nexus_validation_current
        if current is None:
            return
        installed, entry = current
        self._nexus_validation_current = None
        self._stop_nexus_timer()
        if self._validation_stop_requested:
            return
        self._append_validation(
            installed.name,
            self._validate_nexus_match(
                installed,
                entry,
                None,
                f"MO2 Nexus requestFileInfo timed out; URL match accepted. Request: {self._nexus_last_request or '(unknown)'}",
            ),
        )
        self._validation_done += 1
        self.refresh()
        self._continue_validation_queue()


    def _continue_validation_queue(self):
        QTimer.singleShot(0, self._process_next_validation)


    def _validate_deadlystream_match(
        self,
        installed: _InstalledModEntry,
        entry: _BuildModEntry,
        metadata: KotorRepoMetadata,
    ) -> _ValidationResult:
        url = entry.urls[0] if entry.urls else ""
        available_files = metadata.available_files
        current_version = metadata.latest_version
        current_version_release_date = metadata.latest_version_release_date
        submitted_date = metadata.original_upload_date
        archive_release_date = current_version_release_date or submitted_date
        archive_name_valid = installed.installation_file in available_files if installed.installation_file else False

        local_newest = installed.download_meta.get("newestVersion", "").strip()
        version_valid = bool(current_version) and bool(local_newest) and local_newest not in {"", "0.0.0.0"} and local_newest == current_version
        local_release_date = installed.download_meta.get(self._ARCHIVE_RELEASE_DATE_FIELD, "").strip()
        release_date_valid = bool(archive_release_date) and bool(local_release_date) and local_release_date == archive_release_date
        release_date_updated = False
        if (
            installed.download_meta_path is not None
            and archive_release_date
            and self._validation_force_release_update
        ):
            release_date_updated = write_download_meta_field(
                installed.download_meta_path,
                self._ARCHIVE_RELEASE_DATE_FIELD,
                archive_release_date,
            )
            if release_date_updated:
                installed.download_meta[self._ARCHIVE_RELEASE_DATE_FIELD] = archive_release_date
                local_release_date = archive_release_date
                release_date_valid = True

        if archive_name_valid and release_date_valid:
            summary = "VALID"
        elif archive_name_valid:
            if release_date_updated and self._validation_force_release_update:
                summary = "DATE FORCED"
            else:
                summary = "DATE MISS"
        elif release_date_valid:
            summary = "DATE OK"
        else:
            summary = "MISMATCH"

        return _ValidationResult(
            summary,
            [
                f"{entry.name}",
                f"URL: {url}",
                f"Installed archive: {installed.installation_file or '(unknown)'}",
                f"KotorRepo latest version: {current_version or '(unknown)'}",
                f"MO2 newestVersion: {local_newest or '(unknown)'}",
                f"KotorRepo original upload date: {submitted_date or '(unknown)'}",
                f"KotorRepo latest version release date: {current_version_release_date or '(unknown)'}",
                f"Stored archive release date: {local_release_date or '(unknown)'}",
                f"Archive release date compared: {archive_release_date or '(unknown)'}",
                f"Archive name match: {archive_name_valid}",
                f"Version match (informational): {version_valid}",
                f"Archive release date match: {release_date_valid}",
                f"Archive release date field updated: {release_date_updated}",
                f"User-visible result: {summary}",
                "KotorRepo available downloads:",
                *(available_files or ["(none reported)"]),
            ],
        )


    def _validate_nexus_match(
        self,
        installed: _InstalledModEntry,
        entry: _BuildModEntry,
        nexus_files: list | None,
        nexus_source: str,
    ) -> _ValidationResult:
        nexus_meta = self._nexus_meta_for_entry(installed, entry)
        local_newest = nexus_meta.get("newestVersion", "").strip()
        local_version = nexus_meta.get("version", "").strip()
        archive_mod_id = nexus_meta.get("modID", "").strip()
        archive_file_id = nexus_meta.get("fileID", "").strip()
        mo2_version = installed.mo2_version
        mo2_newest = installed.mo2_newest_version
        archive_current = local_version if local_version and local_version != "0.0.0.0" else local_newest
        mo2_current = mo2_version if mo2_version and mo2_version != "0.0.0.0" else archive_current
        matched_file = self._matching_nexus_file(installed, entry, nexus_files or [])
        file_id_valid = bool(
            matched_file is not None
            and archive_file_id.isdigit()
            and nexus_file_id(matched_file) == int(archive_file_id)
        )
        nexus_latest = nexus_file_latest_version(matched_file) if matched_file is not None else ""
        version_valid = bool(nexus_latest) and bool(mo2_current) and versions_equal(mo2_current, nexus_latest)
        if nexus_files is None:
            summary = "VALID URL"
        elif matched_file is None:
            summary = "NEXUS FILE MISS"
        elif file_id_valid:
            summary = "VALID"
        elif version_valid:
            summary = "VALID"
        else:
            summary = "VERSION MISS"
        return _ValidationResult(
            summary,
            [
                f"{entry.name}",
                f"URL: {entry.urls[0] if entry.urls else '(missing)'}",
                f"Installed archive: {installed.installation_file or '(unknown)'}",
                f"MO2 Nexus modID: {archive_mod_id or '(unknown)'}",
                f"MO2 Nexus fileID: {archive_file_id or '(unknown)'}",
                f"MO2 API nexusId: {installed.mo2_nexus_id or '(unknown)'}",
                f"MO2 API version: {mo2_version or '(unknown)'}",
                f"MO2 API newestVersion (ignored): {mo2_newest or '(unknown)'}",
                f"Archive meta version: {local_version or '(unknown)'}",
                f"Archive meta newestVersion: {local_newest or '(unknown)'}",
                f"Nexus version compared: {mo2_current or '(unknown)'}",
                f"Nexus latest compared: {nexus_latest or '(unknown)'}",
                f"Nexus validation source: {nexus_source}",
                f"Nexus returned files: {len(nexus_files or [])}",
                f"Nexus file info summary: {nexus_file_summary(matched_file) if matched_file is not None else '(none)'}",
                f"Nexus fileID match: {file_id_valid}",
                f"Nexus matched file: {nexus_file_name(matched_file) if matched_file is not None else '(none)'}",
                f"Nexus latest version: {nexus_latest or '(unknown)'}",
                f"Version compared: {mo2_current or '(unknown)'}",
                f"Version match: {version_valid}",
                f"User-visible result: {summary}",
            ],
        )


    def _matching_nexus_file(self, installed: _InstalledModEntry, entry: _BuildModEntry, nexus_files: list):
        installed_file_id = self._nexus_meta_for_entry(installed, entry).get("fileID", "").strip()
        if installed_file_id.isdigit() and int(installed_file_id) > 0:
            for file_info in nexus_files:
                if nexus_file_id(file_info) == int(installed_file_id):
                    return file_info
        if len(nexus_files) == 1:
            return nexus_files[0]
        installed_name = installed.installation_file.strip().strip('"').strip("'").lower()
        if not installed_name:
            return None
        for file_info in nexus_files:
            if nexus_file_name(file_info).lower() == installed_name:
                return file_info
        return None


    def _download_meta_path(self, installation_file: str) -> Path | None:
        return download_meta_path(Path(self._organizer.downloadsPath()), installation_file)


    def _cache_path(self) -> Path:
        game_name = self._game.gameShortName().lower()
        try:
            profile_path = Path(self._organizer.profilePath())
            if profile_path.exists():
                return profile_path / f"kotor_builder_{game_name}.json"
        except Exception:
            pass
        return Path(__file__).resolve().parent / f".kotor_builder_{game_name}.json"


    def _load_cache(self):
        cache_path = self._cache_path()
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cache_version = int(payload.get("cache_version", 0) or 0)

        if str(payload.get("build_source", "")).strip() == self._full_build_url():
            self._build_entries = [
                _BuildModEntry(
                    name=str(item.get("name", "")).strip(),
                    urls=[str(url).strip() for url in item.get("urls", []) if str(url).strip()],
                )
                for item in payload.get("build_entries", [])
                if str(item.get("name", "")).strip()
            ]
        if cache_version != self._CACHE_VERSION:
            self._validation_by_mod = {}
            return
        self._validation_by_mod = {
            str(mod_name): _ValidationResult(
                summary=str(result.get("summary", "")).strip(),
                details=[str(line) for line in result.get("details", [])],
            )
            for mod_name, result in payload.get("validation_by_mod", {}).items()
            if str(mod_name).strip() and isinstance(result, dict)
        }
        self._manual_build_match_by_mod = {
            str(mod_name): str(key)
            for mod_name, key in payload.get("manual_build_match_by_mod", {}).items()
            if str(mod_name).strip() and str(key).strip()
        }
        self._manual_archive_by_mod = {
            str(mod_name): str(archive)
            for mod_name, archive in payload.get("manual_archive_by_mod", {}).items()
            if str(mod_name).strip() and str(archive).strip()
        }
        self._author_by_url = {
            str(url): str(author).strip()
            for url, author in payload.get("author_by_url", {}).items()
            if str(url).strip() and str(author).strip()
        }


    def _save_cache(self):
        cache_path = self._cache_path()
        payload = {
            "cache_version": self._CACHE_VERSION,
            "game": self._game.gameShortName().lower(),
            "build_source": self._full_build_url(),
            "build_entries": [
                {"name": entry.name, "urls": entry.urls}
                for entry in self._build_entries
            ],
            "validation_by_mod": {
                mod_name: {"summary": result.summary, "details": result.details}
                for mod_name, result in self._validation_by_mod.items()
            },
            "manual_build_match_by_mod": self._manual_build_match_by_mod,
            "manual_archive_by_mod": self._manual_archive_by_mod,
            "author_by_url": self._author_by_url,
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass


    def _full_build_url(self) -> str:
        game = "k2" if self._game.gameShortName().lower() == "kotor2" else "k1"
        return KOTOR_REPO_DATA_URL.format(game=game)


    def _matching_build_entries(self, installed: _InstalledModEntry) -> list[_BuildModEntry]:
        if not self._build_entries:
            return []
        manual_entry = self._manual_build_entry_for_mod(installed.name)
        if manual_entry is not None:
            return [manual_entry]
        installed_urls = {
            canonical
            for url in installed.urls
            if (canonical := _canonical_url(url))
        }
        candidate_mod_ids = {
            str(meta.get("modID", "")).strip()
            for _archive, _path, meta in installed.meta_candidates
            if str(meta.get("modID", "")).strip().isdigit()
        }
        matches: list[_BuildModEntry] = []
        for entry in self._build_entries:
            entry_urls = {
                canonical
                for url in entry.urls
                if (canonical := _canonical_url(url))
            }
            if installed_urls & entry_urls:
                matches.append(entry)
                continue
            entry_mod_ids = {mod_id for url in entry.urls if (mod_id := nexus_mod_id_from_url(url))}
            if candidate_mod_ids & entry_mod_ids:
                matches.append(entry)
        return matches


    def _manual_build_entry_for_mod(self, mod_name: str) -> _BuildModEntry | None:
        selected_key = self._manual_build_match_by_mod.get(mod_name, "")
        if not selected_key:
            return None
        return self._manual_build_entry_for_key(selected_key)


    def _manual_build_entry_for_key(self, selected_key: str) -> _BuildModEntry | None:
        for entry in self._build_entries:
            if _build_entry_key(entry) == selected_key:
                return entry
        return None


    def _write_mod_meta_for_build_match(self, mod_name: str, entry: _BuildModEntry) -> bool:
        mod_path = Path(self._organizer.modsPath()) / mod_name
        archive_entry = self._manual_archive_entry_for_mod(mod_name)
        if archive_entry is None:
            current_installation_file = str(read_mod_meta(mod_path).get("installationFile", "")).strip()
            archive_entry = self._download_meta_entry_for_archive_name(current_installation_file) if current_installation_file else None
        try:
            write_mod_meta_for_build_match(
                mod_path,
                entry.name,
                entry.urls[0] if entry.urls else "",
                archive_entry,
            )
            return True
        except Exception as exc:
            meta_path = mod_path / "meta.ini"
            QMessageBox.warning(self, "Metadata Update Failed", f"Could not update {meta_path}.\n\n{exc}")
            return False


    def _manual_archive_entry_for_mod(self, mod_name: str) -> tuple[str, Path, dict[str, str]] | None:
        archive_name = self._manual_archive_by_mod.get(mod_name, "")
        if not archive_name:
            return None
        return self._download_meta_entry_for_archive_name(archive_name)


    def _download_meta_entry_for_archive_name(self, archive_name: str) -> tuple[str, Path, dict[str, str]] | None:
        for entry in self._download_meta_entries():
            if entry[0] == archive_name:
                return entry
        return None


    def _download_meta_entries(self) -> list[DownloadMetaEntry]:
        self._ensure_download_meta_index()
        return list(self._download_meta_entries_cache or [])


    def _details_text(self, installed: _InstalledModEntry, matches: list[_BuildModEntry]) -> str:
        lines = [f"Installed MO2 mod: {installed.name}", f"MO2 priority: {installed.priority}", ""]
        validation = self._validation_by_mod.get(installed.name)
        if validation is not None:
            lines.append(f"Validation: {validation.summary}")
            lines.extend(validation.details)
            lines.append("")
        lines.append("MO archive URL(s):")
        lines.extend(installed.urls or ["(no source URLs found in MO2 metadata)"])
        lines.append("")
        lines.append("Archive meta candidate(s):")
        if installed.meta_candidates:
            for archive_name, meta_path, meta in installed.meta_candidates[:12]:
                mod_id = str(meta.get("modID", "")).strip()
                file_id = str(meta.get("fileID", "")).strip()
                repository = str(meta.get("repository", "")).strip()
                lines.append(
                    f"{archive_name} | repository={repository or '(unknown)'} | "
                    f"modID={mod_id or '(none)'} | fileID={file_id or '(none)'} | {meta_path}"
                )
            if len(installed.meta_candidates) > 12:
                lines.append(f"... {len(installed.meta_candidates) - 12} more")
        else:
            lines.append("(no archive .meta candidates found by modName)")
        lines.append("")

        if not matches:
            lines.extend(
                [
                    "Mod-build URL(s):",
                    "(no full build URL match found)",
                    "",
                    f"Build source: {self._full_build_url()}",
                ]
            )
            return "\n".join(lines)

        manual_entry = self._manual_build_entry_for_mod(installed.name)
        if manual_entry is not None:
            lines.append(f"Manual build match: {manual_entry.name}")
            lines.append("")
        lines.append("Matching mod-build entry URL(s):")
        for entry in matches:
            lines.append("")
            lines.append(entry.name)
            lines.extend(entry.urls or ["(no URLs found)"])
        return "\n".join(lines).rstrip()



    def _unmatched_build_details_text(self, entry: _BuildModEntry) -> str:
        lines = [
            "Full build mod: " + entry.name,
            "Installed MO2 mod: (no match)",
            "",
            "Mod-build URL(s):",
            *(entry.urls or ["(no URLs found)"]),
            "",
            "To include this in a KSON build, install the mod in MO2 or choose this build entry as a manual match on an installed mod row.",
            "",
            f"Build source: {self._full_build_url()}",
        ]
        return "\n".join(lines).rstrip()




def _build_entry_key(entry: _BuildModEntry) -> str:
    first_url = entry.urls[0] if entry.urls else ""
    return f"{entry.name}\n{first_url}"
