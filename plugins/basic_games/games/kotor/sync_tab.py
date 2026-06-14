import configparser
import ctypes
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import mobase
from PyQt6.QtCore import QPoint, QProcess, Qt, QThread, QTimer, QUrl
from PyQt6.QtGui import QBrush, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
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

from archive_service import ArchiveService
from hash_utils import file_hash
from sync_installer import SyncInstallResult
from sync_workers import (
    _DownloadedValidationWorker,
    _FetchWorker,
    _SyncWorker,
    _ValidationWorker,
    _kson_version_text_from_name,
)
from ui_theme import (
    configure_download_button,
    configure_refresh_button,
    configure_tree_widget,
    mo2_archive_conflict_purple,
    refresh_mo2,
    set_header_resize_mode,
    tree_conflict_row_color,
    tree_row_padding_stylesheet,
)

logger = logging.getLogger("mobase")


_WM_CLOSE = 0x0010


def _log_info(message: str):
    logger.info(f"[KOTOR2 Sync] {message}")


def _log_warning(message: str):
    cleaned = " ".join(str(message).split())
    if len(cleaned) > 500:
        cleaned = f"{cleaned[:497]}..."
    logger.warning(f"[KOTOR2 Sync] {cleaned}")



class _NumericTreeWidgetItem(QTreeWidgetItem):

    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        left = self.data(column, Qt.ItemDataRole.UserRole + 10)
        right = other.data(column, Qt.ItemDataRole.UserRole + 10)
        if isinstance(left, int) and isinstance(right, int):
            return left < right
        return super().__lt__(other)


class Kotor2SyncTab(QWidget):
    _FETCH_TIMEOUT_SECONDS = 20
    _DOWNLOAD_QUEUE_DELAY_MS = 3000
    _KSON_REPO = "J0-o/kson_modlist"


    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._download_queue: list[tuple[QTreeWidgetItem, dict]] = []
        self._download_process: QProcess | None = None
        self._download_process_context: tuple[QTreeWidgetItem, dict, set[str]] | None = None
        self._download_validation_thread: QThread | None = None
        self._download_validation_worker: _DownloadedValidationWorker | None = None
        self._download_validation_context: tuple[QTreeWidgetItem, dict, Path, str, bool] | None = None
        self._download_validation_continue_pending = False
        self._download_cancel_requested = False
        self._browser_process: subprocess.Popen | None = None
        self._browser_profile_dir: Path | None = None
        self._browser_waiting: tuple[QTreeWidgetItem, dict, Path, str, float, str, set[str]] | None = None
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _FetchWorker | None = None
        self._validation_thread: QThread | None = None
        self._validation_worker: _ValidationWorker | None = None
        self._validation_sorting_enabled: bool | None = None
        self._sync_thread: QThread | None = None
        self._sync_worker: _SyncWorker | None = None
        self._sync_temp_kson_path: Path | None = None
        self._sync_progress_lines: list[str] = []
        self._sync_busy = False
        self._validated_for_sync = False
        self._download_missing_available = False

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("0 mods")
        self._kson_version_label = QLabel("KSON: unknown")
        self._refresh_btn = QPushButton("Refresh")
        configure_refresh_button(self._refresh_btn)
        self._refresh_btn.clicked.connect(self._refresh_fetch_validate)
        self._download_btn = QPushButton("Download Missing")
        configure_download_button(self._download_btn)
        self._set_download_missing_available(False)
        self._download_btn.clicked.connect(self._download_missing_archives)
        self._stop_download_btn = QPushButton("Stop")
        self._stop_download_btn.setEnabled(False)
        self._stop_download_btn.clicked.connect(self._stop_downloads)
        self._sync_btn = QPushButton("Sync")
        self._sync_btn.setEnabled(False)
        self._sync_btn.clicked.connect(self._sync_validated_build)
        header.addWidget(self._refresh_btn)
        header.addWidget(self._download_btn)
        header.addWidget(self._stop_download_btn)
        header.addWidget(self._summary_label)
        header.addWidget(self._kson_version_label)
        header.addStretch()
        header.addWidget(self._sync_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(10)
        self._tree.setHeaderLabels(
            ["State", "Priority", "Mod", "Enabled", "Archive", "Version", "Release Date", "Source", "Files", "Actions"]
        )
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
        )
        self._tree.setStyleSheet(tree_row_padding_stylesheet())
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        header_view = self._tree.header()
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, 10)
        self._tree.setColumnWidth(0, 70)
        self._tree.setColumnWidth(1, 70)
        self._tree.setColumnWidth(2, 300)
        self._tree.setColumnWidth(3, 80)
        self._tree.setColumnWidth(4, 260)
        self._tree.setColumnWidth(5, 100)
        self._tree.setColumnWidth(6, 150)
        self._tree.setColumnWidth(7, 120)
        self._tree.setColumnWidth(8, 70)
        self._tree.setColumnWidth(9, 70)
        layout.addWidget(self._tree, 3)

        self._details = QPlainTextEdit(self)
        self._details.setReadOnly(True)
        self._details.setVisible(False)

        self.refresh()


    def _refresh_fetch_validate(self):
        if self._fetch_thread is not None or self._validation_thread is not None:
            return
        self._set_download_missing_available(False)
        self.refresh()
        QTimer.singleShot(0, self._refresh_sync_skip_row_styles)
        self._start_fetch_latest_manifest()


    def refresh(self):
        self._tree.clear()
        self._validated_for_sync = False
        self._update_sync_button_state()
        kson = self._read_cached_kson()
        version_text = self._cached_kson_version_text()
        self._kson_version_label.setText(f"KSON: {version_text}")
        if kson is None:
            row = QTreeWidgetItem(["Info", "", "No cached KSON", "", "", "", "", "Press Fetch", "", ""])
            row.setData(
                0,
                Qt.ItemDataRole.UserRole,
                "\n".join(
                    [
                        "No cached sync KSON exists yet.",
                        "",
                        f"Game: {self._game.gameName()}",
                        f"Cache path: {self._cache_path()}",
                        f"KSON version: {version_text}",
                        "",
                        "Press Fetch to download the latest KSON build for this game.",
                    ]
                ),
            )
            self._tree.addTopLevelItem(row)
            self._tree.setCurrentItem(row)
            self._summary_label.setText("0 mods")
            self._update_details()
            return

        mods = kson.get("mods", [])
        build_name = str(kson.get("game") or self._build_key())
        source_url = str(kson.get("_source_url") or "")
        fetched_at = str(kson.get("_fetched_at") or "")
        patch_order_count = self._patch_order_count(kson.get("tslpatch_order"))
        for mod in mods:
            mod_name = self._kson_mod_name(mod)
            if not mod_name:
                continue
            skipped = self._kson_mod_skipped(mod) if isinstance(mod, dict) else False
            enabled_label = "Enabled" if self._kson_mod_enabled(mod) else "Disabled"
            priority = mod.get("priority") if isinstance(mod, dict) else None
            mod_url = str(mod.get("url") or "").strip() if isinstance(mod, dict) else ""
            archive_files = mod.get("archive_files", []) if isinstance(mod, dict) else []
            actions = mod.get("actions", []) if isinstance(mod, dict) else []
            row = _NumericTreeWidgetItem(
                [
                    "Skip" if skipped else "Ready",
                    str(priority if priority is not None else ""),
                    mod_name,
                    enabled_label,
                    self._display_archive_name(mod) if isinstance(mod, dict) else "",
                    str(mod.get("version") or "").strip() if isinstance(mod, dict) else "",
                    str(mod.get("release_date") or "").strip() if isinstance(mod, dict) else "",
                    self._source_label(mod_url),
                    str(len(archive_files)) if isinstance(archive_files, list) else "",
                    str(len(actions)) if isinstance(actions, list) else "",
                ]
            )
            row.setToolTip(7, mod_url)
            row.setData(0, Qt.ItemDataRole.UserRole + 1, mod)
            row.setData(0, Qt.ItemDataRole.UserRole + 2, skipped)
            row.setData(0, Qt.ItemDataRole.UserRole + 3, "")
            row.setData(1, Qt.ItemDataRole.UserRole + 10, int(priority) if str(priority).lstrip("-").isdigit() else -1)
            row.setData(8, Qt.ItemDataRole.UserRole + 10, len(archive_files) if isinstance(archive_files, list) else -1)
            row.setData(9, Qt.ItemDataRole.UserRole + 10, len(actions) if isinstance(actions, list) else -1)
            self._apply_sync_skip_row_style(row, skipped)
            row.setData(
                0,
                Qt.ItemDataRole.UserRole,
                "\n".join(
                    [
                        f"Mod: {mod_name}",
                        f"Build: {build_name}",
                        f"Enabled: {enabled_label}",
                        f"Sync: {'Skipped' if skipped else 'Included'}",
                        f"Fetched: {fetched_at or '(unknown)'}",
                        f"KSON version: {version_text}",
                        f"Source URL: {source_url or '(unknown)'}",
                        f"TSLPatch order entries: {patch_order_count}",
                        f"Cache file: {self._cache_path()}",
                    ]
                ),
            )
            self._tree.addTopLevelItem(row)

        if self._tree.topLevelItemCount():
            self._tree.setCurrentItem(self._tree.topLevelItem(0))
        self._summary_label.setText(f"{self._tree.topLevelItemCount()} mods")
        QTimer.singleShot(0, self._refresh_sync_skip_row_styles)
        self._update_details()


    def _download_missing_archives(self):
        if (
            self._download_process is not None
            or self._browser_waiting is not None
            or self._download_validation_thread is not None
            or self._validation_thread is not None
        ):
            return
        self._download_queue = []
        for index in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(index)
            mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
            if not isinstance(mod, dict):
                continue
            if self._row_is_sync_skipped(row):
                continue
            archive_name = self._expected_archive_name(mod)
            url = str(mod.get("url") or "").strip()
            if not self._row_needs_missing_download(row):
                continue
            if archive_name:
                self._download_queue.append((row, mod))
                continue
            if url:
                self._download_queue.append((row, mod))

        if not self._download_queue:
            self._details.setPlainText("No missing archives to download.")
            return

        self._download_btn.setEnabled(False)
        self._stop_download_btn.setEnabled(True)
        self._details.setPlainText(f"Downloading {len(self._download_queue)} missing archive(s) one at a time.")
        _log_info(f"Download queue started: {len(self._download_queue)} missing archive(s).")
        self._process_next_download()


    def _process_next_download(self):
        if not self._download_queue:
            self._set_download_missing_available(self._has_missing_download_rows())
            self._stop_download_btn.setEnabled(False)
            self._summary_label.setText("Download queue complete")
            self._details.appendPlainText("\nDownload queue complete.")
            _log_info("Download queue complete.")
            return

        row, mod = self._download_queue.pop(0)
        if self._row_is_sync_skipped(row):
            QTimer.singleShot(0, self._process_next_download)
            return
        mod_name = self._kson_mod_name(mod)
        archive_name = self._expected_archive_name(mod)
        if archive_name:
            existing_archive_path = self._row_archive_path(row)
            if existing_archive_path is not None:
                self._mark_downloaded(row, mod, existing_archive_path, "Archive already exists in downloads.", continue_queue=True)
                return
            if row.text(0) in {"Hash OK", "Empty OK"}:
                QTimer.singleShot(0, self._process_next_download)
                return
        url = str(mod.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        row.setText(0, "Downloading")
        self._summary_label.setText(f"Downloading {mod_name}")
        self._details.setPlainText(
            "\n".join(
                [
                    f"Mod: {mod_name}",
                    f"Archive: {archive_name}",
                    f"URL: {url or '(none)'}",
                    "",
                    "Downloading one missing archive.",
                ]
            )
        )
        QApplication.processEvents()

        if not url:
            self._mark_download_failed(row, mod, "No URL in KSON.")
            QTimer.singleShot(0, self._process_next_download)
            return
        if "deadlystream.com" in host and self._start_deadlystream_download(row, mod, url, archive_name):
            return
        if "nexusmods.com" in host and self._start_nexus_download(row, mod, url):
            return
        self._start_browser_download(row, mod, url, "Browser fallback")


    def _start_deadlystream_download(self, row: QTreeWidgetItem, mod: dict, url: str, archive_name: str) -> bool:
        scraper = Path(__file__).resolve().parent / "DeadlyScraper.exe"
        if not scraper.exists():
            self._append_download_detail("DeadlyScraper.exe is missing; using browser fallback.", warning=True)
            return False
        download_url = url
        selected_url, selection_note = self._resolve_deadlystream_download_url(mod, url)
        if not selected_url:
            self._append_download_detail(selection_note or "DeadlyScraper could not resolve a matching DeadlyStream version.", warning=True)
            return False
        if selection_note:
            self._append_download_detail(selection_note)
        download_url = selected_url
        process = QProcess(self)
        self._download_process = process
        self._download_process_context = (
            row,
            mod,
            {path.name for path in self._downloads_path().iterdir() if path.is_file()},
        )
        self._stop_download_btn.setEnabled(True)
        process.finished.connect(
            lambda _code, _status, row=row, mod=mod, process=process:
            self._finish_deadlystream_download(row, mod, process)
        )
        args = [download_url, "--download", str(self._downloads_path())]
        if archive_name and not self._is_tslrcm_expected_archive_name(archive_name):
            args.extend(["--select", archive_name])
        process.start(str(scraper), args)
        return True


    def _finish_deadlystream_download(self, row: QTreeWidgetItem, mod: dict, process: QProcess):
        stdout = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        stderr = bytes(process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        self._download_process = None
        context = self._download_process_context
        self._download_process_context = None
        if self._download_cancel_requested:
            self._download_cancel_requested = False
            if context is not None:
                cancel_row, cancel_mod, existing_names = context
                self._cleanup_download_artifacts(existing_names)
                self._mark_download_stopped(cancel_row, cancel_mod, "DeadlyScraper download stopped and cleaned up.")
            self._download_btn.setEnabled(self._download_missing_available)
            self._stop_download_btn.setEnabled(False)
            return
        existing_names = context[2] if context is not None else set()
        downloaded_path = self._detect_new_download(existing_names)
        if downloaded_path is not None:
            self._mark_downloaded(row, mod, downloaded_path, "Downloaded with DeadlyScraper.", continue_queue=True)
            return
        self._append_download_detail(
            "\n".join(
                [
                    "DeadlyScraper did not produce the expected archive; using browser fallback.",
                    stdout,
                    stderr,
                ]
            ).strip()
            ,
            warning=True,
        )
        QTimer.singleShot(
            0,
            lambda row=row, mod=mod: self._start_browser_download(
                row,
                mod,
                str(mod.get("url") or ""),
                "DeadlyScraper fallback",
            ),
        )


    def _start_nexus_download(self, row: QTreeWidgetItem, mod: dict, url: str) -> bool:
        if self._start_nxm_download(row, mod, url):
            return True
        popup_url = self._nexus_download_popup_url(mod, url)
        if popup_url:
            self._start_browser_download(row, mod, popup_url, "Nexus DownloadPopUp fallback")
            return True
        self._append_download_detail(
            "Nexus file_id is missing from the KSON. Rebuild the KSON so Nexus archive.meta modID/fileID fields are included.",
            warning=True,
        )
        return False


    def _start_nxm_download(self, row: QTreeWidgetItem, mod: dict, url: str) -> bool:
        mod_id = str(mod.get("mod_id") or mod.get("modID") or "").strip() or self._nexus_mod_id(url)
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if not mod_id or not file_id or not file_id.isdigit() or int(file_id) <= 0:
            self._append_download_detail(
                "Nexus KSON entry has no usable file_id from archive.meta; cannot create nxm:// manager link.",
                warning=True,
            )
            return False

        nxm_url = f"nxm://{self._nexus_game_name()}/mods/{mod_id}/files/{file_id}"
        existing_names = {path.name for path in self._downloads_path().iterdir() if path.is_file()}
        QDesktopServices.openUrl(QUrl(nxm_url))
        self._mark_download_pending(row, mod, f"Opened MO2/Nexus manager link: {nxm_url}")
        self._browser_waiting = (
            row,
            mod,
            self._downloads_path() / html.unescape(self._expected_archive_name(mod)),
            "MO2 nxm download",
            time.monotonic() + 900,
            url,
            existing_names,
        )
        self._stop_download_btn.setEnabled(True)
        QTimer.singleShot(2000, self._poll_browser_download)
        return True


    def _nexus_download_popup_url(self, mod: dict, url: str) -> str:
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if not file_id or not file_id.isdigit() or int(file_id) <= 0:
            return ""
        return (
            "https://www.nexusmods.com/Core/Libs/Common/Widgets/DownloadPopUp"
            f"?id={file_id}&game_id={self._nexus_game_id()}&nmm=1"
        )

    def _manual_download_url(self, mod: dict, url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "nexusmods.com" not in host:
            return url
        mod_id = str(mod.get("mod_id") or mod.get("modID") or "").strip() or self._nexus_mod_id(url)
        if not mod_id:
            return url
        return f"https://www.nexusmods.com/{self._nexus_game_name()}/mods/{mod_id}?tab=files"


    def _start_browser_download(self, row: QTreeWidgetItem, mod: dict, url: str, reason: str):
        archive_name = self._expected_archive_name(mod)
        downloads_path = self._downloads_path()
        edge = self._edge_path()
        if not edge:
            self._mark_download_pending(row, mod, f"{reason}: Edge not found; opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_download)
            return

        profile_dir = Path(self._organizer.profilePath()) / "edge_profile"
        self._prepare_edge_profile(profile_dir, downloads_path)
        try:
            self._browser_profile_dir = profile_dir
            self._browser_process = subprocess.Popen(
                [
                    str(edge),
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble",
                    "--disable-sync",
                    "--new-window",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self._browser_profile_dir = None
            self._mark_download_pending(row, mod, f"{reason}: Edge launch failed ({exc}); opened default browser.")
            QDesktopServices.openUrl(QUrl(url))
            QTimer.singleShot(0, self._process_next_download)
            return

        existing_names = {path.name for path in downloads_path.iterdir() if path.is_file()}
        self._browser_waiting = (
            row,
            mod,
            downloads_path / html.unescape(archive_name),
            reason,
            time.monotonic() + 900,
            url,
            existing_names,
        )
        self._stop_download_btn.setEnabled(True)
        self._append_download_detail(f"{reason}: opened Edge profile. Waiting for archive download.")
        QTimer.singleShot(2000, self._poll_browser_download)


    def _poll_browser_download(self):
        if self._browser_waiting is None:
            return
        row, mod, expected_path, reason, deadline, url, existing_names = self._browser_waiting
        detected_path = self._detect_browser_download(expected_path, existing_names)
        if detected_path is not None:
            self._browser_waiting = None
            self._mark_downloaded(row, mod, detected_path, f"{reason}: browser download detected.", continue_queue=True)
            QTimer.singleShot(2500, self._close_browser_process)
            return
        if self._browser_process is not None and self._browser_process.poll() is not None:
            self._browser_waiting = None
            self._close_browser_process()
            self._set_validation_row(
                row,
                "Failed",
                self._kson_mod_name(mod),
                self._expected_archive_name(mod),
                str(mod.get("archive_xxh3") or "").strip().lower(),
                None,
                "",
                f"{reason}: browser window was closed before the download completed.",
            )
            QTimer.singleShot(0, self._process_next_download)
            return
        if time.monotonic() >= deadline:
            self._browser_waiting = None
            self._close_browser_process()
            fallback_url = self._nexus_download_popup_url(mod, url) if "nxm" in reason.casefold() else ""
            if fallback_url:
                self._start_browser_download(row, mod, fallback_url, "Nexus DownloadPopUp fallback")
            else:
                self._mark_download_pending(row, mod, f"{reason}: Edge did not finish within 15 minutes; opened default browser.")
                QDesktopServices.openUrl(QUrl(url))
                QTimer.singleShot(0, self._process_next_download)
            return
        QTimer.singleShot(2000, self._poll_browser_download)


    def _detect_browser_download(self, expected_path: Path, existing_names: set[str]) -> Path | None:
        return self._archive_service().detect_browser_download(expected_path, existing_names)

    def _detect_new_download(self, existing_names: set[str]) -> Path | None:
        return self._archive_service().detect_new_download(existing_names)

    @staticmethod
    def _is_incomplete_download_name(name: str) -> bool:
        return ArchiveService.is_incomplete_download_name(name)


    def _close_browser_process(self):
        process = self._browser_process
        self._browser_process = None
        profile_dir = self._browser_profile_dir
        self._browser_profile_dir = None
        if process is not None and process.poll() is None:
            self._request_browser_close(process.pid)
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass
        if profile_dir is not None:
            self._sanitize_edge_profile(profile_dir)

    def _stop_downloads(self):
        self._download_queue = []
        self._download_validation_continue_pending = False
        if self._validation_worker is not None:
            self._validation_worker.cancel()
            self._stop_download_btn.setEnabled(False)
            self._summary_label.setText("Stopping validation...")
            self._details.appendPlainText("\nStopping validation after the current archive...")
            return
        if self._download_validation_worker is not None:
            self._download_validation_worker.cancel()
            self._stop_download_btn.setEnabled(False)
            self._summary_label.setText("Stopping hash...")
            self._details.appendPlainText("\nStopping hash after the current archive...")
            return
        if self._sync_worker is not None:
            self._sync_worker.cancel()
            self._stop_download_btn.setEnabled(False)
            self._summary_label.setText("Stopping sync...")
            self._details.appendPlainText("\nStopping sync after the current mod...")
            return
        if self._download_process is not None:
            self._download_cancel_requested = True
            self._stop_download_btn.setEnabled(False)
            process = self._download_process
            process.kill()
            process.waitForFinished(5000)
            return
        if self._browser_waiting is not None:
            row, mod, _expected_path, _reason, _deadline, _url, existing_names = self._browser_waiting
            self._browser_waiting = None
            self._close_browser_process()
            self._cleanup_download_artifacts(existing_names)
            self._mark_download_stopped(row, mod, "Browser download stopped and cleaned up.")
        self._download_btn.setEnabled(self._download_missing_available)
        self._stop_download_btn.setEnabled(False)

    def _cleanup_download_artifacts(self, existing_names: set[str]):
        try:
            candidates = [path for path in self._downloads_path().iterdir() if path.is_file()]
        except Exception:
            return
        for path in candidates:
            if path.name in existing_names:
                continue
            try:
                path.unlink()
            except Exception:
                continue
            meta_path = path.with_name(f"{path.name}.meta")
            if meta_path.exists():
                try:
                    meta_path.unlink()
                except Exception:
                    pass


    def _mark_downloaded(
        self,
        row: QTreeWidgetItem,
        mod: dict,
        archive_path: Path,
        result: str,
        continue_queue: bool = False,
    ):
        self._set_validation_row(
            row,
            "Hashing",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            archive_path,
            "",
            result,
        )
        self._start_download_validation(row, mod, archive_path, result, continue_queue)

    def _start_download_validation(
        self,
        row: QTreeWidgetItem,
        mod: dict,
        archive_path: Path,
        result: str,
        continue_queue: bool,
    ):
        if self._download_validation_thread is not None:
            _log_warning("Download validation skipped: another downloaded archive is already being validated.")
            if continue_queue:
                QTimer.singleShot(0, self._process_next_download)
            return
        thread = QThread(self)
        worker = _DownloadedValidationWorker(self._downloads_path(), self._cache_path(), mod, archive_path)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._finish_download_validation)
        worker.failed.connect(self._fail_download_validation)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_download_validation_worker)
        self._download_validation_thread = thread
        self._download_validation_worker = worker
        self._download_validation_context = (row, mod, archive_path, result, continue_queue)
        thread.start()

    def _finish_download_validation(self, payload: dict):
        context = self._download_validation_context
        if context is None:
            return
        row, mod, archive_path, result, continue_queue = context
        archive_path_text = str(payload.get("archive_path") or "")
        if archive_path_text:
            archive_path = Path(archive_path_text)
        wrap_result = str(payload.get("wrap_result") or "")
        if wrap_result:
            result = f"{result}\n{wrap_result}"
        validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}
        self._apply_validation_result(row, validation)
        validation_result = str(validation.get("bucket") or "skipped")
        if validation_result == "ok":
            self._capture_downloaded_archive_metadata(mod, archive_path)
            row.setText(4, self._display_archive_name(mod))
            self._write_archive_meta(mod, archive_path)
        details = str(row.data(0, Qt.ItemDataRole.UserRole) or "")
        if details:
            details = f"{details}\n\nDownload: {result}"
            for column in range(row.columnCount()):
                row.setData(column, Qt.ItemDataRole.UserRole, details)
        if self._tree.currentItem() is row:
            self._details.setPlainText(str(row.data(0, Qt.ItemDataRole.UserRole) or ""))
        if validation_result == "ok":
            row.setText(0, "Hash OK")
        if continue_queue:
            self._download_validation_continue_pending = True

    def _fail_download_validation(self, message: str):
        context = self._download_validation_context
        if context is None:
            return
        row, mod, archive_path, _result, continue_queue = context
        if "stopped" in message.lower():
            self._mark_download_stopped(row, mod, message)
            self._download_validation_continue_pending = False
            self._download_queue = []
            return
        self._set_validation_row(
            row,
            "Hash Fail",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            archive_path,
            "",
            f"Downloaded archive validation failed: {message}",
        )
        _log_warning(f"Downloaded archive validation failed: {message}")
        if continue_queue:
            self._download_validation_continue_pending = True

    def _clear_download_validation_worker(self):
        self._download_validation_thread = None
        self._download_validation_worker = None
        self._download_validation_context = None
        continue_pending = self._download_validation_continue_pending
        self._download_validation_continue_pending = False
        if not continue_pending and self._download_process is None and self._browser_waiting is None:
            self._stop_download_btn.setEnabled(False)
        if continue_pending:
            QTimer.singleShot(self._DOWNLOAD_QUEUE_DELAY_MS, self._process_next_download)


    def _mark_download_pending(self, row: QTreeWidgetItem, mod: dict, result: str):
        self._set_validation_row(
            row,
            "Pending",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            None,
            "",
            result,
        )


    def _mark_download_failed(self, row: QTreeWidgetItem, mod: dict, result: str):
        self._set_validation_row(
            row,
            "Download Fail",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            None,
            "",
            result,
        )

    def _mark_download_stopped(self, row: QTreeWidgetItem, mod: dict, result: str):
        self._set_validation_row(
            row,
            "Stopped",
            self._kson_mod_name(mod),
            self._expected_archive_name(mod),
            str(mod.get("archive_xxh3") or "").strip().lower(),
            None,
            "",
            result,
        )


    def _append_download_detail(self, text: str, warning: bool = False):
        if text:
            self._details.appendPlainText(f"\n{text}")
            if warning:
                _log_warning(text)


    def _start_fetch_latest_manifest(self):
        thread = QThread(self)
        worker = _FetchWorker(
            self._cache_path(),
            self._build_key(),
            self._game.gameName(),
            self._KSON_REPO,
            self._FETCH_TIMEOUT_SECONDS,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._finish_fetch_latest_manifest)
        worker.failed.connect(self._fail_fetch_latest_manifest)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_fetch_worker)
        self._fetch_thread = thread
        self._fetch_worker = worker
        self._refresh_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._stop_download_btn.setEnabled(True)
        self._sync_btn.setEnabled(False)
        self._summary_label.setText("Fetching KSON...")
        self._details.setPlainText("Fetching latest KSON manifest...")
        thread.start()


    def _finish_fetch_latest_manifest(self, result: dict):
        self.refresh()
        details = str(result.get("details") or "")
        current = self._tree.currentItem()
        if current is not None:
            current.setData(0, Qt.ItemDataRole.UserRole, details)
        self._details.setPlainText(details)
        _log_info(f"KSON loaded: {result.get('mod_count')} mod(s).")
        for warning in result.get("warnings", []):
            _log_warning(f"Fetch warning: {warning}")
        self._validate_archives()


    def _fail_fetch_latest_manifest(self, message: str, errors: list[str]):
        _log_warning(f"Failed to load KSON: {message}")
        self._tree.clear()
        row = QTreeWidgetItem(["Error", "", "Fetch failed", "", "", "", "", "See details", "", ""])
        row.setData(
            0,
            Qt.ItemDataRole.UserRole,
            "\n".join(
                [
                    f"Failed to fetch the latest KSON for {self._game.gameName()}.",
                    "",
                    message,
                    *(["", "Fetch warnings:", *errors] if errors else []),
                ]
            ),
        )
        self._tree.addTopLevelItem(row)
        self._tree.setCurrentItem(row)
        self._summary_label.setText("0 mods")
        self._set_download_missing_available(False)
        self._update_details()


    def _clear_fetch_worker(self):
        self._fetch_thread = None
        self._fetch_worker = None
        if self._validation_thread is None:
            self._refresh_btn.setEnabled(True)
            self._download_btn.setEnabled(self._download_missing_available)
        self._update_sync_button_state()


    def _update_details(self):
        item = self._tree.currentItem()
        self._details.setPlainText(str(item.data(0, Qt.ItemDataRole.UserRole) or "") if item else "")


    def _show_context_menu(self, pos: QPoint):
        row = self._tree.itemAt(pos)
        if row is None:
            return
        mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(mod, dict):
            return

        archive_name = self._display_archive_name(mod)
        archive_path = self._row_archive_path(row)
        url = str(mod.get("url") or "").strip()

        menu = QMenu(self)
        skip_action = menu.addAction("Unskip" if self._row_is_sync_skipped(row) else "Skip")
        download_action = menu.addAction("Download")
        manual_download_action = menu.addAction("Manual Download")
        webpage_action = menu.addAction("View Web Page")
        hash_action = menu.addAction("Hash Check")
        explorer_action = menu.addAction("Open in Explorer")

        if (
            self._validation_thread is not None
            or self._sync_busy
            or self._download_process is not None
            or self._browser_waiting is not None
            or self._download_validation_thread is not None
        ):
            skip_action.setEnabled(False)
        if (
            self._download_process is not None
            or self._browser_waiting is not None
            or self._download_validation_thread is not None
        ):
            download_action.setEnabled(False)
            manual_download_action.setEnabled(False)
        if self._validation_thread is not None:
            download_action.setEnabled(False)
            manual_download_action.setEnabled(False)
            hash_action.setEnabled(False)
        if self._row_is_sync_skipped(row):
            download_action.setEnabled(False)
            manual_download_action.setEnabled(False)
            hash_action.setEnabled(False)
        if not url:
            webpage_action.setEnabled(False)
            manual_download_action.setEnabled(False)
        if not archive_name:
            download_action.setEnabled(False)
            hash_action.setEnabled(False)
        if archive_path is None:
            explorer_action.setEnabled(False)

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen is skip_action:
            self._set_row_sync_skipped(row, mod, not self._row_is_sync_skipped(row))
        elif chosen is download_action:
            self._download_selected_row(row, mod)
        elif chosen is manual_download_action:
            self._manual_download_selected_row(row, mod)
        elif chosen is webpage_action and url:
            QDesktopServices.openUrl(QUrl(url))
        elif chosen is hash_action:
            self._validate_archive_row(row)
        elif chosen is explorer_action and archive_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(archive_path.parent)))


    def _download_selected_row(self, row: QTreeWidgetItem, mod: dict):
        if (
            self._download_process is not None
            or self._browser_waiting is not None
            or self._download_validation_thread is not None
            or self._validation_thread is not None
        ):
            return
        if self._row_is_sync_skipped(row):
            return
        self._download_queue = [(row, mod)]
        self._download_btn.setEnabled(False)
        self._process_next_download()

    def _manual_download_selected_row(self, row: QTreeWidgetItem, mod: dict):
        if (
            self._download_process is not None
            or self._browser_waiting is not None
            or self._download_validation_thread is not None
            or self._validation_thread is not None
        ):
            return
        if self._row_is_sync_skipped(row):
            return
        url = str(mod.get("url") or "").strip()
        if not url:
            return
        manual_url = self._manual_download_url(mod, url)
        self._download_queue = []
        self._download_btn.setEnabled(False)
        self._start_browser_download(row, mod, manual_url, "Manual download")


    def _validate_archive_row(self, row: QTreeWidgetItem):
        if self._validation_thread is not None:
            return
        mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(mod, dict):
            return
        if self._row_is_sync_skipped(row):
            return
        self._validate_archive_row_from_mod(row, mod)
        self._update_details()


    def _validate_archive_row_from_mod(
        self,
        row: QTreeWidgetItem,
        mod: dict,
        hash_cache: dict[Path, str] | None = None,
    ) -> str:
        result = self._archive_service().validate_mod(mod, hash_cache=hash_cache)
        self._apply_validation_result(row, result)
        return str(result.get("bucket") or "skipped")


    def _validate_archives(self):
        kson = self._read_cached_kson()
        if not kson:
            self._details.setPlainText("No cached KSON is loaded. Fetch or place a local KSON first.")
            _log_warning("Archive validation skipped: no cached KSON.")
            return
        if self._validation_thread is not None:
            return

        rows = [self._tree.topLevelItem(index) for index in range(self._tree.topLevelItemCount())]
        validation_rows = [row for row in rows if not self._row_is_sync_skipped(row)]
        row_specs = [
            {
                "row_index": rows.index(row),
                "mod": row.data(0, Qt.ItemDataRole.UserRole + 1),
                "mod_name": row.text(2),
            }
            for row in validation_rows
        ]

        self._validated_for_sync = False
        self._update_sync_button_state()
        self._validation_sorting_enabled = self._tree.isSortingEnabled()
        self._tree.setSortingEnabled(False)
        for row in rows:
            row.setText(0, "Skip" if self._row_is_sync_skipped(row) else "Queued")

        thread = QThread(self)
        worker = _ValidationWorker(self._cache_path(), self._downloads_path(), kson, row_specs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._update_validation_progress)
        worker.finished.connect(self._finish_archive_validation)
        worker.failed.connect(self._fail_archive_validation)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_validation_worker)
        self._validation_thread = thread
        self._validation_worker = worker
        self._refresh_btn.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._stop_download_btn.setEnabled(True)
        self._sync_btn.setEnabled(False)
        self._summary_label.setText(f"Validating 0/{len(validation_rows)}")
        self._details.setPlainText("Validating downloaded archives...")
        thread.start()

    def _archive_service(self) -> ArchiveService:
        return ArchiveService(self._downloads_path(), self._cache_path())

    def _apply_validation_result(self, row: QTreeWidgetItem, result: dict):
        archive_path_text = str(result.get("archive_path") or "").strip()
        archive_path = Path(archive_path_text) if archive_path_text else None
        self._set_validation_row(
            row,
            str(result.get("state") or "Skipped"),
            str(result.get("mod_name") or ""),
            str(result.get("archive_name") or ""),
            str(result.get("expected_hash") or ""),
            archive_path,
            str(result.get("actual_hash") or ""),
            str(result.get("result") or ""),
        )

    def _update_validation_progress(self, current: int, total: int, row_index: int, result: dict):
        row = self._tree.topLevelItem(row_index)
        if row is None:
            return
        self._apply_validation_result(row, result)
        self._summary_label.setText(f"Validating {current}/{total}: {result.get('mod_name') or row.text(2)}")

    def _finish_archive_validation(self, counts: dict):
        self._restore_validation_sorting()
        user_skipped = self._user_skipped_row_count()
        total_skipped = counts["skipped"] + user_skipped
        self._summary_label.setText(
            f"{counts['ok']} ok | {counts['empty']} empty | {counts['missing']} missing | "
            f"{counts['mismatch']} mismatch | {total_skipped} skipped"
        )
        if counts["missing"] or counts["mismatch"] or counts["skipped"]:
            _log_warning(
                f"Archive validation needs attention: {counts['ok']} ok, {counts['empty']} empty, "
                f"{counts['missing']} missing, {counts['mismatch']} mismatch, {counts['skipped']} validation skipped, "
                f"{user_skipped} user skipped."
            )
        else:
            _log_info(f"Archive validation passed: {counts['ok']} ok, {counts['empty']} empty, {user_skipped} user skipped.")
        if counts["missing"] == 0 and counts["mismatch"] == 0 and counts["skipped"] == 0:
            self._refresh_validated_for_current_rows()
        else:
            self._validated_for_sync = False
        self._set_download_missing_available(self._has_missing_download_rows())
        self._update_sync_button_state()
        self._update_details()

    def _fail_archive_validation(self, message: str):
        self._restore_validation_sorting()
        self._validated_for_sync = False
        stopped = "stopped" in message.lower()
        self._summary_label.setText("Validation stopped" if stopped else "Validation failed")
        self._details.setPlainText(message if stopped else f"Archive validation failed:\n{message}")
        _log_warning(f"Archive validation {'stopped' if stopped else 'failed'}: {message}")
        self._set_download_missing_available(False if stopped else self._has_missing_download_rows())
        self._update_sync_button_state()

    def _restore_validation_sorting(self):
        if self._validation_sorting_enabled is None:
            return
        self._tree.setSortingEnabled(self._validation_sorting_enabled)
        self._validation_sorting_enabled = None

    def _clear_validation_worker(self):
        self._restore_validation_sorting()
        self._validation_thread = None
        self._validation_worker = None
        if self._fetch_thread is None:
            self._refresh_btn.setEnabled(True)
            self._download_btn.setEnabled(self._download_missing_available)
        self._stop_download_btn.setEnabled(False)
        self._update_sync_button_state()


    def _sync_validated_build(self):
        if not self._validated_for_sync or self._sync_thread is not None or self._sync_busy or self._validation_thread is not None:
            return
        kson_path = self._cache_path()
        if not kson_path.exists():
            self._details.setPlainText("No cached KSON is available to sync.")
            _log_warning("Sync skipped: no cached KSON.")
            return
        response = QMessageBox.warning(
            self,
            "Confirm Sync",
            "\n".join(
                [
                    "Sync will delete and rebuild your mod list.",
                    "",
                    "To keep custom mods from being overwritten, add [NODELETE] to the beginning of their name.",
                    "",
                    "Do you want to continue?",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        sync_kson_path = self._write_sync_kson_for_current_rows(kson_path)
        if sync_kson_path is None:
            self._details.setPlainText("Sync failed before starting: could not prepare the skipped-mod list.")
            _log_warning("Sync skipped: could not prepare filtered KSON.")
            return
        thread = QThread(self)
        worker = _SyncWorker(sync_kson_path, self._downloads_path(), Path(self._organizer.modsPath()), Path(self._organizer.profilePath()))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._update_sync_progress)
        worker.finished.connect(self._finish_sync)
        worker.failed.connect(self._fail_sync)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_sync_worker)
        self._sync_thread = thread
        self._sync_worker = worker
        self._sync_progress_lines = []
        self._sync_busy = True
        self._update_sync_button_state()
        self._stop_download_btn.setEnabled(True)
        self._details.setPlainText("Starting sync...")
        _log_info("Sync started.")
        thread.start()

    def _update_sync_progress(self, current: int, total: int, mod_name: str, status: str):
        self._summary_label.setText(f"Syncing {current}/{total}: {mod_name}")
        self._sync_progress_lines.append(f"[{current}/{total}] {mod_name}: {status}")
        self._sync_progress_lines = self._sync_progress_lines[-80:]
        self._details.setPlainText("\n".join(self._sync_progress_lines))
        self._details.verticalScrollBar().setValue(self._details.verticalScrollBar().maximum())

    def _finish_sync(self, result: SyncInstallResult):
        details = [f"Synced {result.mod_count} mod(s).", f"Updated: {Path(self._organizer.profilePath()) / 'modlist.txt'}"]
        if result.warnings:
            details.extend(["", "Warnings:", *result.warnings[:30]])
        self._details.setPlainText("\n".join(details))
        self._summary_label.setText(f"Synced {result.mod_count} mods")
        _log_info(f"Sync finished: {result.mod_count} mod(s).")
        for warning in result.warnings[:30]:
            _log_warning(warning)
        refresh_mo2(self._organizer, self)
        QTimer.singleShot(750, self._run_post_sync_steps)

    def _fail_sync(self, message: str):
        stopped = "stopped" in message.lower()
        self._details.setPlainText(message if stopped else f"Sync failed:\n{message}")
        self._summary_label.setText("Sync stopped" if stopped else "Sync failed")
        _log_warning(f"Sync {'stopped' if stopped else 'failed'}: {message}")
        self._sync_busy = False
        self._stop_download_btn.setEnabled(False)
        self._update_sync_button_state()
        refresh_mo2(self._organizer, self)

    def _clear_sync_worker(self):
        self._sync_thread = None
        self._sync_worker = None
        if self._sync_temp_kson_path is not None:
            try:
                self._sync_temp_kson_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._sync_temp_kson_path = None
        self._stop_download_btn.setEnabled(False)
        self._update_sync_button_state()


    def _run_post_sync_steps(self):
        patcher_tab = getattr(self._game, "_patcher_tab", None)
        run_after_sync = getattr(patcher_tab, "run_after_sync", None)
        if not callable(run_after_sync):
            _log_warning("Patcher tab is not available after sync.")
            self._sync_busy = False
            self._update_sync_button_state()
            self._refresh_related_tabs()
            return

        self._summary_label.setText("Running patcher")
        self._details.appendPlainText("\nRunning patcher...")
        try:
            run_after_sync()
            self._details.appendPlainText("Patcher finished.")
            self._run_texture_auto_fix_after_sync()
            self._summary_label.setText("Sync, patcher, and texture autofix complete")
        except Exception as exc:
            self._summary_label.setText("Sync complete; post-sync step failed")
            self._details.appendPlainText(f"Post-sync step failed:\n{exc}")
            _log_warning(f"Post-sync step failed: {exc}")
        self._sync_busy = False
        self._update_sync_button_state()
        self._refresh_related_tabs()

    def _update_sync_button_state(self):
        self._sync_btn.setEnabled(
            self._validated_for_sync
            and not self._sync_busy
            and self._sync_thread is None
            and self._fetch_thread is None
            and self._validation_thread is None
        )

    def _set_download_missing_available(self, available: bool):
        self._download_missing_available = available
        self._download_btn.setEnabled(available)

    def _has_missing_download_rows(self) -> bool:
        return any(
            self._row_needs_missing_download(self._tree.topLevelItem(index))
            for index in range(self._tree.topLevelItemCount())
        )

    def _row_needs_missing_download(self, row: QTreeWidgetItem) -> bool:
        if self._row_is_sync_skipped(row):
            return False
        if row.text(0) not in {"Missing", "Hash Miss", "Hash Fail", "Download Fail", "Failed", "Pending", "Stopped"}:
            return False
        mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
        if not isinstance(mod, dict):
            return False
        return bool(self._expected_archive_name(mod) or str(mod.get("url") or "").strip())


    def _run_texture_auto_fix_after_sync(self):
        texture_tab = getattr(self._game, "_texture_tab", None)
        run_auto_fix = getattr(texture_tab, "run_auto_fix_after_sync", None)
        if not callable(run_auto_fix):
            _log_warning("Texture tab is not available after sync.")
            return

        self._summary_label.setText("Running texture autofix")
        self._details.appendPlainText("Running texture autofix...")
        run_auto_fix()
        self._details.appendPlainText("Texture autofix finished.")


    def _refresh_related_tabs(self):
        for attr_name in ("_patcher_tab", "_texture_tab"):
            tab = getattr(self._game, attr_name, None)
            refresh = getattr(tab, "refresh", None)
            if callable(refresh):
                refresh()

    def _set_validation_row(
        self,
        row: QTreeWidgetItem,
        state: str,
        mod_name: str,
        archive_name: str,
        expected_hash: str,
        archive_path: Path | None,
        actual_hash: str,
        result: str,
    ):
        row.setText(0, state)
        row.setData(0, Qt.ItemDataRole.UserRole + 3, str(archive_path) if archive_path is not None else "")
        details = "\n".join(
            [
                f"Mod: {mod_name}",
                f"Validation: {state}",
                f"Result: {result}",
                "",
                f"Archive name: {archive_name or '(none)'}",
                f"Archive path: {archive_path or '(not found)'}",
                f"Expected archive XXH3: {expected_hash or '(none)'}",
                f"Actual archive XXH3: {actual_hash or '(none)'}",
                f"Cache file: {self._cache_path()}",
            ]
        )
        for column in range(row.columnCount()):
            row.setData(column, Qt.ItemDataRole.UserRole, details)
        if self._tree.currentItem() is row:
            self._details.setPlainText(details)

    def _row_archive_path(self, row: QTreeWidgetItem) -> Path | None:
        value = str(row.data(0, Qt.ItemDataRole.UserRole + 3) or "").strip()
        if not value:
            return None
        path = Path(value)
        return path if path.exists() else None

    def _row_is_sync_skipped(self, row: QTreeWidgetItem) -> bool:
        return bool(row.data(0, Qt.ItemDataRole.UserRole + 2))

    def _apply_sync_skip_row_style(self, row: QTreeWidgetItem, skipped: bool):
        brush = QBrush(tree_conflict_row_color(self._tree, mo2_archive_conflict_purple())) if skipped else QBrush()
        for column in range(self._tree.columnCount()):
            row.setBackground(column, brush)

    def _refresh_sync_skip_row_styles(self):
        for index in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(index)
            self._apply_sync_skip_row_style(row, self._row_is_sync_skipped(row))

    def _set_row_sync_skipped(self, row: QTreeWidgetItem, mod: dict, skipped: bool):
        row.setData(0, Qt.ItemDataRole.UserRole + 2, skipped)
        if skipped:
            mod["_sync_skip"] = True
            self._set_validation_row(
                row,
                "Skip",
                self._kson_mod_name(mod),
                self._expected_archive_name(mod),
                str(mod.get("archive_xxh3") or "").strip().lower(),
                None,
                "",
                "This mod is skipped and will not be downloaded or synced.",
            )
        else:
            mod.pop("_sync_skip", None)
            row.setText(0, "Ready")
            self._update_row_sync_details(row, mod)
        self._apply_sync_skip_row_style(row, skipped)
        self._write_persisted_sync_skip(mod, skipped)
        self._write_cached_kson_mod_update(mod)
        self._refresh_validated_for_current_rows()
        self._set_download_missing_available(self._download_missing_available and self._has_missing_download_rows())
        self._update_sync_button_state()
        if self._tree.currentItem() is row:
            self._update_details()

    def _update_row_sync_details(self, row: QTreeWidgetItem, mod: dict):
        skipped = self._kson_mod_skipped(mod)
        details = "\n".join(
            [
                f"Mod: {self._kson_mod_name(mod)}",
                f"Sync: {'Skipped' if skipped else 'Included'}",
                f"Archive name: {self._display_archive_name(mod) or '(none)'}",
                f"Cache file: {self._cache_path()}",
            ]
        )
        for column in range(row.columnCount()):
            row.setData(column, Qt.ItemDataRole.UserRole, details)

    def _refresh_validated_for_current_rows(self):
        valid_states = {"Hash OK", "Empty OK"}
        has_included = False
        for index in range(self._tree.topLevelItemCount()):
            row = self._tree.topLevelItem(index)
            if self._row_is_sync_skipped(row):
                continue
            has_included = True
            if row.text(0) not in valid_states:
                self._validated_for_sync = False
                return
        self._validated_for_sync = has_included

    def _user_skipped_row_count(self) -> int:
        return sum(
            1
            for index in range(self._tree.topLevelItemCount())
            if self._row_is_sync_skipped(self._tree.topLevelItem(index))
        )

    def _write_sync_kson_for_current_rows(self, source_path: Path) -> Path | None:
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            mods = payload.get("mods", [])
            if not isinstance(mods, list):
                return None

            skipped_keys: set[tuple[str, str]] = set()
            for index in range(self._tree.topLevelItemCount()):
                row = self._tree.topLevelItem(index)
                mod = row.data(0, Qt.ItemDataRole.UserRole + 1)
                if not isinstance(mod, dict) or not self._row_is_sync_skipped(row):
                    continue
                skipped_keys.add((self._kson_mod_name(mod), str(mod.get("priority") or "").strip()))

            filtered_mods = []
            for mod in mods:
                if not isinstance(mod, dict):
                    continue
                key = (self._kson_mod_name(mod), str(mod.get("priority") or "").strip())
                if key in skipped_keys or self._kson_mod_skipped(mod):
                    continue
                filtered_mods.append(mod)
            payload["mods"] = filtered_mods

            if self._sync_temp_kson_path is not None:
                try:
                    self._sync_temp_kson_path.unlink(missing_ok=True)
                except Exception:
                    pass
                self._sync_temp_kson_path = None

            handle, temp_name = tempfile.mkstemp(
                prefix=f"{self._build_key()}_sync_",
                suffix=".kson",
                dir=str(self._cache_path().parent),
            )
            os.close(handle)
            temp_path = Path(temp_name)
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._sync_temp_kson_path = temp_path
            return temp_path
        except Exception:
            return None


    def _build_key(self) -> str:
        return "kotor2" if self._game.gameShortName().lower() == "kotor2" else "kotor"


    def _kson_dir(self) -> Path:
        return Path(self._organizer.profilePath()) / "kson"


    def _cache_path(self) -> Path:
        return self._kson_dir() / f"{self._build_key()}_latest_build.kson"


    def _sync_skip_state_path(self) -> Path:
        return Path(self._organizer.profilePath()) / f"{self._build_key()}_sync_skips.json"


    def _cached_kson_version_text(self) -> str:
        cache_path = self._cache_path()
        source_url = ""
        selected_name = ""
        if cache_path.exists():
            try:
                kson = json.loads(cache_path.read_text(encoding="utf-8"))
                source_url = str(kson.get("_source_url") or "")
                selected_name = str(kson.get("_selected_kson_name") or "")
            except Exception:
                source_url = ""
        for name in (selected_name, Path(source_url).name if source_url else "", cache_path.name):
            version_text = _kson_version_text_from_name(name)
            if version_text != "unknown":
                return version_text
        return self._latest_local_kson_version_text()


    def _latest_local_kson_version_text(self) -> str:
        candidates = [
            _kson_version_text_from_name(path.name)
            for path in self._kson_dir().glob("*.kson")
            if path.name != self._cache_path().name
            and self._is_game_kson_path(path.name)
        ]
        known = [candidate for candidate in candidates if candidate != "unknown"]
        return max(known) if known else "unknown"


    def _is_game_kson_path(self, path: str) -> bool:
        name = Path(path).name.lower()
        if not name.endswith(".kson"):
            return False
        if self._build_key() == "kotor2":
            return name.startswith("kotor2")
        return name.startswith("kotor") and not name.startswith("kotor2")


    def _archive_path(self, *archive_names: str) -> Path | None:
        return self._archive_service().resolve_named_archive_path(*archive_names)

    @staticmethod
    def _normalize_release_date(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            return text

    def _read_archive_meta(self, archive_path: Path) -> configparser.ConfigParser | None:
        meta_path = archive_path.with_name(f"{archive_path.name}.meta")
        if not meta_path.exists():
            return None
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read(meta_path, encoding="utf-8")
        except Exception:
            return None
        if not parser.has_section("General"):
            return None
        return parser

    def _archive_meta_value(self, archive_path: Path, key: str) -> str:
        parser = self._read_archive_meta(archive_path)
        if parser is None:
            return ""
        return parser.get("General", key, fallback="").strip()

    def _query_deadlystream_versions(self, url: str) -> tuple[dict | None, str]:
        scraper = Path(__file__).resolve().parent / "DeadlyScraper.exe"
        if not scraper.exists():
            return None, "DeadlyScraper.exe is missing."
        result = subprocess.run(
            [str(scraper), url, "--check-all-versions"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            startupinfo=self._subprocess_startupinfo(),
            creationflags=self._subprocess_creationflags(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return None, detail or f"DeadlyScraper version query failed with exit code {result.returncode}."
        try:
            payload = json.loads(result.stdout)
        except Exception as exc:
            return None, f"DeadlyScraper returned invalid JSON: {exc}"
        if not isinstance(payload, dict):
            return None, "DeadlyScraper returned an unexpected payload."
        return payload, ""

    def _resolve_deadlystream_download_url(self, mod: dict, url: str) -> tuple[str, str]:
        expected_release_date = self._normalize_release_date(str(mod.get("release_date") or ""))
        if not expected_release_date:
            return url, ""
        payload, error = self._query_deadlystream_versions(url)
        if payload is None:
            return "", f"DeadlyScraper version query failed for {self._kson_mod_name(mod)}: {error}"
        current_release_date = self._normalize_release_date(
            str(payload.get("EffectiveDate") or payload.get("PublishedDate") or payload.get("CurrentVersionReleaseDate") or "")
        )
        if current_release_date == expected_release_date:
            return url, f"DeadlyScraper confirmed current DeadlyStream version date {expected_release_date}."
        for version in payload.get("VersionHistory", []):
            if not isinstance(version, dict):
                continue
            release_date = self._normalize_release_date(str(version.get("ReleaseDate") or ""))
            if release_date != expected_release_date:
                continue
            download_url = str(version.get("ChangelogUrl") or version.get("DownloadPageUrl") or "").strip()
            if download_url:
                version_label = str(version.get("VersionLabel") or "").strip()
                if version_label:
                    return download_url, f"DeadlyScraper selected DeadlyStream version {version_label} for release date {expected_release_date}."
                return download_url, f"DeadlyScraper selected the DeadlyStream version published on {expected_release_date}."
        return "", f"No DeadlyStream version matches KSON release date {expected_release_date} for {self._kson_mod_name(mod)}."

    @staticmethod
    def _is_tslrcm_expected_archive_name(name: str) -> bool:
        return ArchiveService.is_tslrcm_expected_archive_name(name)


    @staticmethod
    def _seven_zip_exe() -> str:
        return ArchiveService.seven_zip_exe()


    @staticmethod
    def _subprocess_startupinfo():
        return ArchiveService.subprocess_startupinfo()


    @staticmethod
    def _subprocess_creationflags() -> int:
        return ArchiveService.subprocess_creationflags()


    def _downloads_path(self) -> Path:
        return Path(self._organizer.downloadsPath())


    def _nexus_game_name(self) -> str:
        try:
            value = self._game.gameNexusName()
            if value:
                return str(value).strip().lower()
        except Exception:
            pass
        return self._game.gameShortName().lower()


    def _nexus_game_id(self) -> str:
        game_name = self._nexus_game_name()
        if game_name == "kotor":
            return "234"
        if game_name == "kotor2":
            return "198"
        try:
            value = self._game.gameNexusID()
            if value:
                return str(value)
        except Exception:
            pass
        return "234" if self._build_key() == "kotor" else "198"


    @staticmethod
    def _nexus_mod_id(url: str) -> str:
        match = re.search(r"/mods/(\d+)", url)
        return match.group(1) if match else ""


    @staticmethod
    def _edge_path() -> Path | None:
        candidates = [
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None


    @staticmethod
    def _prepare_edge_profile(profile_dir: Path, downloads_path: Path):
        profile_dir.mkdir(parents=True, exist_ok=True)
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
        prefs.setdefault("profile", {})
        prefs["profile"].setdefault("default_content_setting_values", {})
        prefs["profile"]["exit_type"] = "Normal"
        prefs["profile"]["exited_cleanly"] = True
        prefs["profile"]["edge_crash_exit_count"] = 0
        prefs.setdefault("session_restore_prompt", {})
        prefs["session_restore_prompt"]["ignored"] = True
        prefs.setdefault("sessions", {})
        prefs["sessions"]["event_log"] = []
        prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
        (profile_dir / "First Run").write_text("", encoding="ascii")
        Kotor2SyncTab._clear_edge_session_files(profile_dir)


    @staticmethod
    def _sanitize_edge_profile(profile_dir: Path):
        default_dir = profile_dir / "Default"
        prefs_path = default_dir / "Preferences"
        if prefs_path.exists():
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            except Exception:
                prefs = {}
            prefs.setdefault("profile", {})
            prefs["profile"].setdefault("default_content_setting_values", {})
            prefs["profile"]["exit_type"] = "Normal"
            prefs["profile"]["exited_cleanly"] = True
            prefs["profile"]["edge_crash_exit_count"] = 0
            prefs.setdefault("session_restore_prompt", {})
            prefs["session_restore_prompt"]["ignored"] = True
            prefs.setdefault("sessions", {})
            prefs["sessions"]["event_log"] = []
            try:
                prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
            except Exception:
                pass
        Kotor2SyncTab._clear_edge_session_files(profile_dir)


    @staticmethod
    def _clear_edge_session_files(profile_dir: Path):
        for relative_name in (
            "Default/Current Session",
            "Default/Current Tabs",
            "Default/Last Session",
            "Default/Last Tabs",
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
        ):
            candidate = profile_dir / relative_name
            if candidate.exists():
                try:
                    candidate.unlink()
                except Exception:
                    pass
        sessions_dir = profile_dir / "Default" / "Sessions"
        if sessions_dir.exists():
            for child in sessions_dir.iterdir():
                try:
                    if child.is_file():
                        child.unlink()
                except Exception:
                    pass


    @staticmethod
    def _request_browser_close(pid: int):
        if os.name != "nt" or pid <= 0:
            return
        try:
            user32 = ctypes.windll.user32
            enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def callback(hwnd, _lparam):
                window_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
                if window_pid.value == pid and user32.IsWindowVisible(hwnd):
                    user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                return True

            callback_proc = enum_windows_proc(callback)
            user32.EnumWindows(callback_proc, 0)
        except Exception:
            pass


    def _write_archive_meta(self, mod: dict, archive_path: Path):
        archive_name = archive_path.name
        meta_path = archive_path.with_name(f"{archive_name}.meta")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        if meta_path.exists():
            try:
                parser.read(meta_path, encoding="utf-8")
            except Exception:
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
        if not parser.has_section("General"):
            parser.add_section("General")

        url = str(mod.get("url") or "").strip()
        host = urlparse(url).netloc.lower()
        repository = str(mod.get("repository") or "").strip()
        if not repository:
            if "nexusmods.com" in host:
                repository = "Nexus"
            elif "deadlystream.com" in host:
                repository = "DeadlyStream"

        fields = {
            "installed": "false",
            "uninstalled": "false",
            "gameName": self._nexus_game_name() if repository.lower() == "nexus" else self._build_key(),
            "name": archive_path.stem,
            "modName": self._kson_mod_name(mod),
            "version": str(mod.get("version") or "").strip(),
            "newestVersion": str(mod.get("version") or "").strip(),
            "manualURL": url,
            "url": url,
            "repository": repository,
            "ArchiveReleaseDate": str(mod.get("release_date") or "").strip(),
            "KsonArchiveXXH3": str(mod.get("archive_xxh3") or "").strip(),
        }

        mod_id = str(mod.get("mod_id") or mod.get("modID") or "").strip() or self._nexus_mod_id(url)
        file_id = str(mod.get("file_id") or mod.get("fileID") or "").strip()
        if mod_id:
            fields["modID"] = mod_id
        if file_id:
            fields["fileID"] = file_id

        for key, value in fields.items():
            if value:
                parser.set("General", key, value)
        with meta_path.open("w", encoding="utf-8") as handle:
            parser.write(handle)

    def _expected_archive_name(self, mod: dict) -> str:
        return self._archive_service().expected_archive_name(mod)

    def _display_archive_name(self, mod: dict) -> str:
        return self._archive_service().display_archive_name(mod)

    def _capture_downloaded_archive_metadata(self, mod: dict, archive_path: Path):
        changed = False
        if not str(mod.get("archive_name") or "").strip():
            mod["archive_name"] = archive_path.name
            changed = True
        if not str(mod.get("archive_xxh3") or "").strip():
            try:
                mod["archive_xxh3"] = file_hash(archive_path).lower()
                changed = True
            except Exception:
                pass
        if "local_archive_name" in mod:
            mod.pop("local_archive_name", None)
            changed = True
        if changed:
            self._write_cached_kson_mod_update(mod)

    def _write_cached_kson_mod_update(self, mod: dict):
        cache_path = self._cache_path()
        kson = self._read_cached_kson()
        if kson is None:
            return
        mods = kson.get("mods", [])
        if not isinstance(mods, list):
            return
        target_name = self._kson_mod_name(mod)
        target_priority = str(mod.get("priority") or "").strip()
        updated = False
        for item in mods:
            if not isinstance(item, dict):
                continue
            if self._kson_mod_name(item) != target_name:
                continue
            if target_priority and str(item.get("priority") or "").strip() != target_priority:
                continue
            item["archive_name"] = self._expected_archive_name(mod)
            item["archive_xxh3"] = str(mod.get("archive_xxh3") or "").strip()
            if self._kson_mod_skipped(mod):
                item["_sync_skip"] = True
            else:
                item.pop("_sync_skip", None)
            item.pop("local_archive_name", None)
            updated = True
            break
        if not updated:
            return
        try:
            cache_path.write_text(json.dumps(kson, indent=2), encoding="utf-8")
        except Exception:
            return


    def _read_cached_kson(self) -> dict | None:
        cache_path = self._cache_path()
        if not cache_path.exists():
            return None
        try:
            kson = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(kson, dict):
            return None
        self._apply_persisted_sync_skips(kson)
        return kson


    def _read_persisted_sync_skips(self) -> dict:
        path = self._sync_skip_state_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        skipped = payload.get("skipped", {}) if isinstance(payload, dict) else {}
        return skipped if isinstance(skipped, dict) else {}


    def _write_persisted_sync_skip(self, mod: dict, skipped: bool):
        keys = self._sync_skip_keys(mod)
        if not keys:
            return
        skipped_mods = self._read_persisted_sync_skips()
        if skipped:
            entry = {
                "mod_name": self._kson_mod_name(mod),
                "priority": str(mod.get("priority") or "").strip(),
                "url": str(mod.get("url") or "").strip(),
                "archive_name": self._expected_archive_name(mod),
            }
            for key in keys:
                skipped_mods[key] = entry
        else:
            mod_name = self._kson_mod_name(mod).casefold()
            for key in list(skipped_mods):
                value = skipped_mods.get(key)
                value_name = str(value.get("mod_name") or "").strip().casefold() if isinstance(value, dict) else ""
                if key in keys or (mod_name and value_name == mod_name):
                    skipped_mods.pop(key, None)

        payload = {
            "version": 1,
            "game": self._build_key(),
            "skipped": skipped_mods,
        }
        try:
            path = self._sync_skip_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            _log_warning(f"Failed to persist sync skip state: {exc}")


    def _apply_persisted_sync_skips(self, kson: dict):
        mods = kson.get("mods", [])
        if not isinstance(mods, list):
            return
        skipped_mods = self._read_persisted_sync_skips()
        if not skipped_mods:
            return
        skipped_keys = set(skipped_mods)
        for mod in mods:
            if not isinstance(mod, dict):
                continue
            if self._sync_skip_keys(mod) & skipped_keys:
                mod["_sync_skip"] = True
            else:
                mod.pop("_sync_skip", None)


    def _sync_skip_keys(self, mod: dict) -> set[str]:
        mod_name = self._kson_mod_name(mod).casefold()
        if not mod_name:
            return set()
        keys = {f"{mod_name}|name"}
        url = str(mod.get("url") or "").strip().casefold()
        if url:
            keys.add(f"{mod_name}|url:{url}")
        archive_name = self._expected_archive_name(mod).casefold()
        if archive_name:
            keys.add(f"{mod_name}|archive:{archive_name}")
        legacy_key = self._sync_skip_legacy_key(mod)
        if legacy_key:
            keys.add(legacy_key)
        return keys


    def _sync_skip_legacy_key(self, mod: dict) -> str:
        mod_name = self._kson_mod_name(mod).casefold()
        if not mod_name:
            return ""
        priority = str(mod.get("priority") or "").strip()
        return f"{mod_name}|priority:{priority}"


    @staticmethod
    def _kson_mod_name(mod) -> str:
        if isinstance(mod, dict):
            name = mod.get("mod_name") or mod.get("name") or mod.get("Mod Name")
            return str(name).strip() if name else ""
        if isinstance(mod, str):
            return mod.strip()
        return ""


    @staticmethod
    def _kson_mod_enabled(mod) -> bool:
        if not isinstance(mod, dict):
            return True
        return bool(mod.get("enabled", True))

    @staticmethod
    def _kson_mod_skipped(mod) -> bool:
        if not isinstance(mod, dict):
            return False
        value = mod.get("_sync_skip", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _source_label(url: str) -> str:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if "deadlystream.com" in host:
            return "DeadlyStream"
        if "nexusmods.com" in host:
            return "Nexus Mods"
        if "mega.nz" in host:
            return "MEGA"
        if "github.com" in host:
            return "GitHub"
        return host or "(none)"



    @staticmethod
    def _patch_order_count(value) -> int:
        if isinstance(value, dict):
            for key in ("mods", "order", "entries"):
                inner = value.get(key)
                if isinstance(inner, list):
                    return len(inner)
            return len(value)
        if isinstance(value, list):
            return len(value)
        return 0
