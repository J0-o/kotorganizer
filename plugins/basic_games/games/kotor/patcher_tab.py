import configparser
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import mobase
from PyQt6.QtCore import QPoint, QSize, QTimer, Qt, QUrl
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from patcher_entries import PatchEntry as _PatcherEntry
from patcher_entries import collect_patch_entries, find_patch_dir, read_ini_with_fallbacks
from ui_theme import (
    configure_refresh_button,
    configure_tree_widget,
    mo2_archive_conflict_purple,
    mo2_conflict_green,
    mo2_conflict_red,
    refresh_mo2,
    set_header_resize_mode,
    tree_conflict_row_color,
    tree_row_padding_stylesheet,
    tree_selected_marker_color,
)

logger = logging.getLogger("mobase")
PATCHER_MOD_BASE_NAME = "[ PATCHER FILES ]"
PATCHER_CATEGORY_NAME = "[ PATCHER ]"

from patcher_constants import (
    COL_CONFLICTS,
    COL_DESCRIPTION,
    COL_ENABLED,
    COL_MOD,
    COL_PATCH,
    COL_PRIORITY,
    PATCHER_CONFLICT_INFORMATIONAL,
    PATCHER_CONFLICT_MIXED,
    PATCHER_CONFLICT_OVERWRITTEN,
    PATCHER_CONFLICT_OVERWRITE,
    PATCHER_TREE_COLUMN_COUNT,
    ROLE_CONFLICT_SORT,
    ROLE_DESTINATION,
    ROLE_FILES,
    ROLE_INI_PATH,
    ROLE_INSTALL_PATHS,
    ROLE_OVERVIEW_COLOR,
    ROLE_REQUIRED,
    MO2_MOD_INDEX_ROLE,
)
from patcher_delegates import (
    _ModListContainsFileDelegate,
    _ModListScrollbarMarkerOverlay,
    _PatcherCheckboxDelegate,
    _PatcherConflictOverview,
    _PatcherItem,
)
from patcher_dialogs import _PatcherDetailsDialog, _PatcherRunnerDialog



def _rtf_to_text(rtf: str) -> str:
    out: list[str] = []
    stack: list[tuple[bool, bool]] = []
    ignorable = False
    uc_skip = 1
    skip = 0
    i = 0
    length = len(rtf)
    destinations = {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "header",
        "footer",
        "headerl",
        "headerr",
        "footerl",
        "footerr",
        "ftnsep",
        "ftnsepc",
        "ftncn",
        "annotation",
        "xmlopen",
        "xmlattrname",
        "xmlattrvalue",
        "xmlclose",
        "fldinst",
        "fldrslt",
    }

    while i < length:
        ch = rtf[i]
        if skip:
            skip -= 1
        elif ch == "{":
            stack.append((ignorable, False))
        elif ch == "}":
            if stack:
                ignorable, _ = stack.pop()
        elif ch == "\\":
            i += 1
            if i >= length:
                break
            ch = rtf[i]
            if ch in "\\{}":
                if not ignorable:
                    out.append(ch)
            elif ch == "*":
                ignorable = True
            elif ch == "'":
                if i + 2 < length and not ignorable:
                    try:
                        out.append(bytes.fromhex(rtf[i + 1 : i + 3]).decode("cp1252", errors="ignore"))
                    except ValueError:
                        pass
                i += 2
            else:
                start = i
                while i < length and rtf[i].isalpha():
                    i += 1
                word = rtf[start:i]
                sign = 1
                if i < length and rtf[i] == "-":
                    sign = -1
                    i += 1
                num_start = i
                while i < length and rtf[i].isdigit():
                    i += 1
                num = sign * int(rtf[num_start:i]) if i > num_start else None
                if i < length and rtf[i] == " ":
                    pass
                else:
                    i -= 1

                if word in destinations:
                    ignorable = True
                if word == "par" or word == "line":
                    if not ignorable:
                        out.append("\n")
                elif word == "tab":
                    if not ignorable:
                        out.append("\t")
                elif word == "uc" and num is not None:
                    uc_skip = num
                elif word == "u":
                    if not ignorable and num is not None:
                        if num < 0:
                            num += 65536
                        out.append(chr(num))
                    skip = uc_skip
        elif ch in "\r\n":
            pass
        elif not ignorable:
            out.append(ch)
        i += 1

    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()



class Kotor2PatcherTab(QWidget):

    def __init__(self, parent: QWidget | None, organizer: mobase.IOrganizer, game):
        super().__init__(parent)
        self._organizer = organizer
        self._game = game
        self._json_path = Path(self._organizer.profilePath()) / "tslpatch_order.json"
        self._active_conflict_key: str | None = None
        self._entries: list[_PatcherEntry] = []
        self._last_profile_order: tuple[str, ...] = tuple()
        self._pending_checkbox_sync = False
        self._pending_click_entry_key: str | None = None
        self._stop_patcher_requested = False
        self._current_patcher_process: subprocess.Popen[str] | None = None
        self._runner_dialog: _PatcherRunnerDialog | None = None
        self._runner_log_text = ""
        self._refresh_pending = False
        self._mod_highlight_warning_logged = False
        self._mod_list_highlight_delegate: _ModListContainsFileDelegate | None = None
        self._mod_list_scrollbar_marker: _ModListScrollbarMarkerOverlay | None = None
        self._patcher_row_size = QSize()

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._summary_label = QLabel("No patches loaded")
        refresh_btn = QPushButton("Refresh")
        configure_refresh_button(refresh_btn)
        refresh_btn.clicked.connect(self._parse_and_refresh)
        runner_btn = QPushButton("Open Patcher")
        runner_btn.clicked.connect(self._open_runner_dialog)
        header.addWidget(refresh_btn)
        header.addWidget(self._summary_label)
        header.addStretch()
        header.addWidget(runner_btn)
        layout.addLayout(header)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(PATCHER_TREE_COLUMN_COUNT)
        self._tree.setHeaderLabels(["Ena", "Conflicts", "Mod", "Patch", "Description", "Priority"])
        configure_tree_widget(
            self._tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
            alternating_rows=False,
            mouse_tracking=True,
        )
        self._apply_tree_style()
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.currentItemChanged.connect(self._on_current_item_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.setItemDelegateForColumn(COL_ENABLED, _PatcherCheckboxDelegate(self._tree))
        header_view = self._tree.header()
        header_view.setSectionsClickable(True)
        set_header_resize_mode(header_view, QHeaderView.ResizeMode.Interactive, PATCHER_TREE_COLUMN_COUNT)
        self._tree.setIconSize(QSize(20, 20))
        self._patcher_row_size = QSize(
            0,
            max(
                self._tree.fontMetrics().height() + int(round(self._tree.fontMetrics().height() * 0.6)),
                self._tree.iconSize().height(),
            ),
        )
        self._tree.setColumnWidth(COL_ENABLED, 42)
        self._tree.setColumnWidth(COL_CONFLICTS, 76)
        self._tree.setColumnWidth(COL_MOD, 220)
        self._tree.setColumnWidth(COL_PATCH, 130)
        self._tree.setColumnWidth(COL_DESCRIPTION, 560)
        self._tree.setColumnWidth(COL_PRIORITY, 56)
        self._tree.sortItems(COL_PRIORITY, Qt.SortOrder.AscendingOrder)
        self._conflict_overview = _PatcherConflictOverview(self._tree)
        tree_layout = QHBoxLayout()
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(2)
        tree_layout.addWidget(self._tree, 1)
        tree_layout.addWidget(self._conflict_overview, 0)
        layout.addLayout(tree_layout, 3)
        header_view.sortIndicatorChanged.connect(self._update_conflict_overview)

        self._order_watch_timer = QTimer(self)
        self._order_watch_timer.setInterval(500)
        self._order_watch_timer.timeout.connect(self._check_mod_order_changed)
        self._checkbox_sync_timer = QTimer(self)
        self._checkbox_sync_timer.setSingleShot(True)
        self._checkbox_sync_timer.setInterval(120)
        self._checkbox_sync_timer.timeout.connect(self._flush_item_changes)
        self._click_select_timer = QTimer(self)
        self._click_select_timer.setSingleShot(True)
        self._click_select_timer.setInterval(180)
        self._click_select_timer.timeout.connect(self._flush_pending_click)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._refresh_now)

        organizer.onProfileChanged(lambda a, b: self.schedule_refresh())
        organizer.modList().onModInstalled(lambda mod: self.schedule_refresh())
        organizer.modList().onModRemoved(lambda mod: self.schedule_refresh())
        organizer.modList().onModStateChanged(lambda mods: self.schedule_refresh())

        self.schedule_refresh(immediate=True)


    def _parse_and_refresh(self):
        if self._tree.topLevelItemCount():
            self._write_json()
        self.schedule_refresh(immediate=True)


    def _profile_mod_order(self) -> list[str]:
        return list(self._organizer.modList().allModsByProfilePriority())


    def _current_profile_name(self) -> str:
        profile_name = Path(self._organizer.profilePath()).name.strip()
        profile_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", profile_name)
        profile_name = re.sub(r"\s+", " ", profile_name).strip(" .")
        return profile_name or "Default"


    def _patcher_mod_name(self) -> str:
        return f"{PATCHER_MOD_BASE_NAME} {self._current_profile_name()}"


    def _patcher_mod_dir(self) -> Path:
        return Path(self._organizer.modsPath()) / self._patcher_mod_name()


    @staticmethod
    def _is_patcher_output_mod(mod_name: str) -> bool:
        return mod_name == PATCHER_MOD_BASE_NAME or mod_name.startswith(f"{PATCHER_MOD_BASE_NAME} ")


    def _activate_patcher_output_mod(self, patcher_mod_name: str):
        try:
            mod_list = self._organizer.modList()
            if mod_list.getMod(patcher_mod_name) is None:
                refresh_mo2(self._organizer, self)
            mod_list.setActive(patcher_mod_name, True)
        except Exception as exc:
            logger.debug("Unable to activate generated patcher output mod %s: %s", patcher_mod_name, exc)


    def _mo2_mod_display_name(self, mod_name: str) -> str:
        try:
            display_name = str(self._organizer.modList().displayName(mod_name) or "").strip()
            if display_name:
                return display_name
        except Exception:
            pass
        return mod_name


    def _find_mo2_mod_model_index(self, model, display_name: str, parent=None):
        row_count = model.rowCount(parent) if parent is not None else model.rowCount()
        for row in range(row_count):
            index = model.index(row, 0, parent) if parent is not None else model.index(row, 0)
            if str(index.data(Qt.ItemDataRole.DisplayRole) or "") == display_name:
                return index
            child = self._find_mo2_mod_model_index(model, display_name, index)
            if child is not None:
                return child
        return None


    def _highlight_mo2_mod(self, mod_name: str):
        mod_name = mod_name.strip()
        if not mod_name:
            return
        mod_list_widget = self.window().findChild(QWidget, "modList")
        if mod_list_widget is None:
            return

        try:
            if not isinstance(mod_list_widget.itemDelegate(), _ModListContainsFileDelegate):
                previous_delegate = mod_list_widget.itemDelegate()
                self._mod_list_highlight_delegate = _ModListContainsFileDelegate(previous_delegate, mod_list_widget)
                mod_list_widget.setItemDelegate(self._mod_list_highlight_delegate)
            else:
                self._mod_list_highlight_delegate = mod_list_widget.itemDelegate()

            scroll_bar = mod_list_widget.verticalScrollBar()
            if self._mod_list_scrollbar_marker is None or self._mod_list_scrollbar_marker.parent() is not scroll_bar:
                self._mod_list_scrollbar_marker = _ModListScrollbarMarkerOverlay(mod_list_widget, scroll_bar)

            display_name = self._mo2_mod_display_name(mod_name)
            model_index = self._find_mo2_mod_model_index(mod_list_widget.model(), display_name)
            mod_index = None
            if model_index is not None:
                try:
                    mod_index = int(model_index.data(MO2_MOD_INDEX_ROLE))
                except (TypeError, ValueError):
                    mod_index = None
            self._mod_list_highlight_delegate.set_highlighted_mod(display_name, mod_index)
            self._mod_list_scrollbar_marker.set_highlighted_mod(display_name, mod_index)
        except Exception as ex:
            if not self._mod_highlight_warning_logged:
                self._mod_highlight_warning_logged = True
                logger.debug("Unable to highlight MO2 mod list entry for %s: %s", mod_name, ex)


    @staticmethod
    def _patch_conflict_icon(conflict_state: str) -> QIcon:
        resource_paths = {
            PATCHER_CONFLICT_MIXED: ":/MO/gui/emblem_conflict_mixed",
            PATCHER_CONFLICT_OVERWRITE: ":/MO/gui/emblem_conflict_overwrite",
            PATCHER_CONFLICT_OVERWRITTEN: ":/MO/gui/emblem_conflict_overwritten",
            PATCHER_CONFLICT_INFORMATIONAL: ":/MO/gui/archive_conflict_mixed",
        }
        return QIcon(resource_paths.get(conflict_state, ""))


    @staticmethod
    def _patch_conflict_sort_weight(conflict_state: str) -> int:
        weights = {
            PATCHER_CONFLICT_OVERWRITTEN: 1,
            PATCHER_CONFLICT_INFORMATIONAL: 2,
            PATCHER_CONFLICT_OVERWRITE: 3,
            PATCHER_CONFLICT_MIXED: 4,
        }
        return weights.get(conflict_state, 0)


    @staticmethod
    def _hard_operation_conflict_keys(entry: _PatcherEntry) -> set[str]:
        return {
            key
            for operation in entry.operations
            if operation.action == "replace"
            for key in operation.conflict_keys()
        }


    @staticmethod
    def _informational_operation_conflict_keys(entry: _PatcherEntry) -> set[str]:
        keys: set[str] = set()
        for operation in entry.operations:
            if operation.action not in {"install", "patch"}:
                continue
            target = operation.target.strip().lower()
            if not target:
                continue
            keys.add(f"{operation.action}:{operation.resource_type}:{target}")
        return keys


    def _build_patch_conflict_states(
        self, entries: list[_PatcherEntry]
    ) -> dict[str, tuple[str, str]]:
        enabled_entries = [entry for entry in entries if entry.enabled]
        hard_keys_by_entry = {
            f"{entry.mod_name}::{entry.patch_name}": self._hard_operation_conflict_keys(entry)
            for entry in enabled_entries
        }
        info_keys_by_entry = {
            f"{entry.mod_name}::{entry.patch_name}": self._informational_operation_conflict_keys(entry)
            for entry in enabled_entries
        }
        order_index = {f"{entry.mod_name}::{entry.patch_name}": index for index, entry in enumerate(enabled_entries)}
        conflicts: dict[str, dict[str, set[str]]] = {
            entry_key: {"overwrites": set(), "overwritten": set(), "same": set()}
            for entry_key, keys in hard_keys_by_entry.items()
            if keys
        }
        informational_conflicts: dict[str, set[str]] = {
            entry_key: set()
            for entry_key, keys in info_keys_by_entry.items()
            if keys
        }

        for left_index, left in enumerate(enabled_entries):
            left_key = f"{left.mod_name}::{left.patch_name}"
            left_hard_keys = hard_keys_by_entry.get(left_key, set())
            left_info_keys = info_keys_by_entry.get(left_key, set())
            for right in enabled_entries[left_index + 1 :]:
                right_key = f"{right.mod_name}::{right.patch_name}"
                left_label = f"{left.mod_name} / {left.patch_name}"
                right_label = f"{right.mod_name} / {right.patch_name}"
                if left_info_keys.intersection(info_keys_by_entry.get(right_key, set())):
                    informational_conflicts.setdefault(left_key, set()).add(right_label)
                    informational_conflicts.setdefault(right_key, set()).add(left_label)

                shared_keys = left_hard_keys.intersection(hard_keys_by_entry.get(right_key, set()))
                if not shared_keys:
                    continue

                left_rank = (left.priority, order_index[left_key])
                right_rank = (right.priority, order_index[right_key])
                if left.priority == right.priority:
                    conflicts.setdefault(left_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "same"
                    ].add(right_label)
                    conflicts.setdefault(right_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "same"
                    ].add(left_label)
                elif left_rank > right_rank:
                    conflicts.setdefault(left_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "overwrites"
                    ].add(right_label)
                    conflicts.setdefault(right_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "overwritten"
                    ].add(left_label)
                else:
                    conflicts.setdefault(left_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "overwritten"
                    ].add(right_label)
                    conflicts.setdefault(right_key, {"overwrites": set(), "overwritten": set(), "same": set()})[
                        "overwrites"
                    ].add(left_label)

        states: dict[str, tuple[str, str]] = {}
        for entry_key, buckets in conflicts.items():
            has_overwrites = bool(buckets["overwrites"])
            has_overwritten = bool(buckets["overwritten"])
            has_same = bool(buckets["same"])
            if has_same or (has_overwrites and has_overwritten):
                state = PATCHER_CONFLICT_MIXED
                summary = "Patch operation conflicts overwrite and are overwritten."
            elif has_overwrites:
                state = PATCHER_CONFLICT_OVERWRITE
                summary = "Patch operation conflicts overwrite lower-priority patches."
            elif has_overwritten:
                state = PATCHER_CONFLICT_OVERWRITTEN
                summary = "Patch operation conflicts are overwritten by higher-priority patches."
            else:
                continue

            lines = [summary]
            if buckets["overwrites"]:
                lines.append("Overwrites: " + "; ".join(sorted(buckets["overwrites"])))
            if buckets["overwritten"]:
                lines.append("Overwritten by: " + "; ".join(sorted(buckets["overwritten"])))
            if buckets["same"]:
                lines.append("Same-priority conflicts: " + "; ".join(sorted(buckets["same"])))
            states[entry_key] = (state, "\n".join(lines))
        for entry_key, labels in informational_conflicts.items():
            if not labels or entry_key in states:
                continue
            states[entry_key] = (
                PATCHER_CONFLICT_INFORMATIONAL,
                "Patch operations touch the same broad targets.\nInformational overlaps: " + "; ".join(sorted(labels)),
            )
        return states


    def _apply_tree_style(self):
        self._tree.setStyleSheet(tree_row_padding_stylesheet())


    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.Type.PaletteChange, event.Type.StyleChange):
            self._apply_tree_style()
            self._rebuild_tree_from_entries()


    def showEvent(self, event):
        super().showEvent(event)
        self._last_profile_order = tuple(self._profile_mod_order())
        self._order_watch_timer.start()
        if self._refresh_pending or not self._tree.topLevelItemCount():
            self.schedule_refresh(immediate=True)


    def hideEvent(self, event):
        self._order_watch_timer.stop()
        super().hideEvent(event)


    def _check_mod_order_changed(self):
        current_order = tuple(self._profile_mod_order())
        if current_order == self._last_profile_order:
            return
        self._last_profile_order = current_order
        self.schedule_refresh()


    def _find_patch_dir(self, mod_path: Path) -> Path | None:
        return find_patch_dir(mod_path)


    def _disable_active_tslpatcher_mods(self) -> list[str]:
        disabled: list[str] = []
        mod_list = self._organizer.modList()
        mods_root = Path(self._organizer.modsPath())
        for mod_name in self._profile_mod_order():
            if self._is_patcher_output_mod(mod_name):
                continue
            if not (mod_list.state(mod_name) & mobase.ModState.ACTIVE):
                continue
            mod_path = mods_root / mod_name
            if not mod_path.exists() or not mod_path.is_dir():
                continue
            if self._find_patch_dir(mod_path) is None:
                continue
            mod_list.setActive(mod_name, False)
            disabled.append(mod_name)
        return disabled


    def _load_enabled_state(self) -> dict[tuple[str, str], bool]:
        enabled: dict[tuple[str, str], bool] = {}
        mod_list = self._organizer.modList()
        for mod_name in self._profile_mod_order():
            enabled[(mod_name, "Default")] = bool(mod_list.state(mod_name) & mobase.ModState.ACTIVE)
        if self._json_path.exists():
            try:
                data = json.loads(self._json_path.read_text(encoding="utf-8"))
                for row in data.get("patches", []):
                    key = (str(row.get("mod_name", "")), str(row.get("patch_name", "")))
                    enabled[key] = bool(row.get("enabled", False))
                return enabled
            except Exception as e:
                logger.warning("[KOTOR2] Failed to read patcher JSON state: %s", e)
        return enabled


    def _collect_patch_entries(self) -> list[_PatcherEntry]:
        mods_root = Path(self._organizer.modsPath())
        order = self._profile_mod_order()
        enabled_state = self._load_enabled_state()
        mod_list = self._organizer.modList()
        active_state = {
            mod_name: bool(mod_list.state(mod_name) & mobase.ModState.ACTIVE)
            for mod_name in order
        }
        entries = collect_patch_entries(mods_root, order, enabled_state, active_state)
        self._ensure_patcher_category(entries)
        return entries


    def _ensure_patcher_category(self, entries: list[_PatcherEntry]) -> None:
        mod_list = self._organizer.modList()
        for mod_name in sorted({entry.mod_name for entry in entries}):
            try:
                mod = mod_list.getMod(mod_name)
                if mod is None:
                    continue
                categories = {str(category) for category in mod.categories()}
                if PATCHER_CATEGORY_NAME not in categories:
                    mod.addCategory(PATCHER_CATEGORY_NAME)
            except Exception as exc:
                logger.warning("[KOTOR] Failed to assign patcher category to %s: %s", mod_name, exc)


    @staticmethod
    def _split_semicolon_list(value: str) -> list[str]:
        return [part.strip() for part in value.split(";") if part.strip()]


    @staticmethod
    def _normalize_relpath(value: str) -> str:
        return value.strip().strip("\\/").replace("/", "\\").lower()


    @staticmethod
    def _is_texture_target(target: str) -> bool:
        suffix = Path(target).suffix.lower()
        return suffix in {".tpc", ".tga", ".txi", ".mdl", ".mdx", ".wav"}


    def _entry_vfs_targets(self, entry: _PatcherEntry) -> set[str]:
        targets: set[str] = set()
        required_targets = {
            self._normalize_relpath(required)
            for required in self._split_semicolon_list(entry.required)
        }

        for destination in self._split_semicolon_list(entry.destination):
            normalized = self._normalize_relpath(destination)
            if normalized and (normalized in required_targets or not self._is_texture_target(normalized)):
                targets.add(normalized)

        for operation in entry.operations:
            if operation.resource_type == "tlk":
                targets.add("dialog.tlk")
                continue

            target = self._normalize_relpath(operation.target)
            location = self._normalize_relpath(operation.location)

            if operation.resource_type == "file" and "::" in target:
                container, inner_target = target.split("::", 1)
                if Path(container).suffix:
                    if container in required_targets or not self._is_texture_target(container):
                        targets.add(container)
                elif container:
                    combined = self._normalize_relpath(f"{container}\\{inner_target}")
                    if combined in required_targets or not self._is_texture_target(combined):
                        targets.add(combined)
                else:
                    if inner_target in required_targets or not self._is_texture_target(inner_target):
                        targets.add(inner_target)
                continue

            if location and Path(location).suffix:
                combined = location
            elif location in {"", "global"}:
                combined = target
            else:
                combined = self._normalize_relpath(f"{location}\\{target}")
            if combined in required_targets or not self._is_texture_target(combined):
                targets.add(combined)

        targets.update(target for target in required_targets if target)

        return {target for target in targets if target and "::" not in target}


    def _resolve_vfs_file(self, target: str) -> tuple[Path | None, str, str]:
        normalized = self._normalize_relpath(target)
        if not normalized:
            return None, "", "target='' -> not found"

        parts = [part for part in normalized.split("\\") if part]
        if not parts:
            return None, normalized, f"target='{normalized}' -> not found"

        trace = [f"target='{normalized}'"]
        mods_root = Path(self._organizer.modsPath())
        active_mods: list[Path] = []
        for mod_name in reversed(self._profile_mod_order()):
            if self._is_patcher_output_mod(mod_name):
                continue
            if not (self._organizer.modList().state(mod_name) & mobase.ModState.ACTIVE):
                continue
            mod_path = mods_root / mod_name
            if mod_path.exists() and mod_path.is_dir():
                active_mods.append(mod_path)

        for mod_path in active_mods:
            if len(parts) == 1:
                direct_candidate = mod_path / parts[0]
                if direct_candidate.exists() and direct_candidate.is_file():
                    trace.append(f"resolved in active mod: {direct_candidate}")
                    return direct_candidate, normalized, "\n".join(trace)

                for root_name in self._game.getModMappings().keys():
                    candidate = mod_path / root_name / parts[0]
                    if candidate.exists() and candidate.is_file():
                        resolved = self._normalize_relpath(f"{root_name}\\{parts[0]}")
                        trace.append(f"resolved in active mod root '{root_name}': {candidate}")
                        return candidate, resolved, "\n".join(trace)
            else:
                candidate = mod_path.joinpath(*parts)
                if candidate.exists() and candidate.is_file():
                    trace.append(f"resolved in active mod: {candidate}")
                    return candidate, normalized, "\n".join(trace)

        game_roots = {key.lower(): Path(path_list[0]) for key, path_list in self._game.getModMappings().items() if path_list}
        if len(parts) > 1 and parts[0].lower() in game_roots:
            game_candidate = game_roots[parts[0].lower()].joinpath(*parts[1:])
            if game_candidate.exists() and game_candidate.is_file():
                trace.append(f"resolved in mapped game root '{parts[0].lower()}': {game_candidate}")
                return game_candidate, normalized, "\n".join(trace)

        if len(parts) == 1:
            dialog_path = Path(self._game.gameDirectory().absolutePath()) / parts[0]
            if dialog_path.exists() and dialog_path.is_file():
                trace.append(f"resolved in game dir: {dialog_path}")
                return dialog_path, normalized, "\n".join(trace)

            for root_name, root_path in game_roots.items():
                game_candidate = root_path / parts[0]
                if game_candidate.exists() and game_candidate.is_file():
                    resolved = self._normalize_relpath(f"{root_name}\\{parts[0]}")
                    trace.append(f"resolved in mapped game root '{root_name}': {game_candidate}")
                    return game_candidate, resolved, "\n".join(trace)

        trace.append("not found in active mods or mapped game roots")
        return None, normalized, "\n".join(trace)


    def _clear_patcher_mod_dir(self, patcher_dir: Path):
        patcher_dir.mkdir(parents=True, exist_ok=True)
        for child in patcher_dir.iterdir():
            if child.name.lower() == "meta.ini":
                continue
            if child.is_dir():
                self._remove_tree(child)
            else:
                try:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
                except FileNotFoundError:
                    pass


    @staticmethod
    def _ensure_dummy_game_exes(patcher_dir: Path):
        dummy_bytes = bytes(range(256))
        for exe_name in ("swkotor2.exe", "swkotor.exe"):
            exe_path = patcher_dir / exe_name
            if exe_path.exists():
                continue
            exe_path.write_bytes(dummy_bytes)


    @staticmethod
    def _remove_dummy_game_exes(patcher_dir: Path):
        for exe_name in ("swkotor2.exe", "swkotor.exe"):
            exe_path = patcher_dir / exe_name
            try:
                if exe_path.exists():
                    os.chmod(exe_path, stat.S_IWRITE)
                    exe_path.unlink()
            except FileNotFoundError:
                pass


    def _prepare_target_dir_for_entries(
        self,
        target_dir: Path,
        entries: list[_PatcherEntry],
        target_name: str,
        log_prefix: str = "",
        update_runner_log: bool = True,
    ) -> str:
        self._clear_patcher_mod_dir(target_dir)
        self._ensure_dummy_game_exes(target_dir)

        targets_by_entry = [
            (entry, sorted(self._entry_vfs_targets(entry)))
            for entry in entries
        ]
        total_targets = sum(len(targets) for _, targets in targets_by_entry)
        copied = 0
        processed = 0
        seen_destinations: set[str] = set()
        resolution_log: list[str] = []
        resolution_cache: dict[str, tuple[Path | None, str, str]] = {}

        for entry_index, (entry, targets) in enumerate(targets_by_entry, start=1):
            if self._stop_patcher_requested:
                return f"{log_prefix.rstrip()}\n\nPrepare stopped by user." if log_prefix else "Prepare stopped by user."

            label = f"{entry.mod_name} / {entry.patch_name}"
            progress = "\n".join(
                [
                    f"Preparing {target_name}...",
                    f"Patch {entry_index}/{len(targets_by_entry)}: {label}",
                    f"Targets processed: {processed}/{total_targets}",
                    f"Files copied: {copied}",
                ]
            )
            if update_runner_log:
                self._set_runner_activity(f"Preparing {entry_index}/{len(targets_by_entry)}")
                self._set_status_with_prefix(log_prefix, progress)

            for target in targets:
                if self._stop_patcher_requested:
                    return f"{log_prefix.rstrip()}\n\nPrepare stopped by user." if log_prefix else "Prepare stopped by user."

                normalized_target = self._normalize_relpath(target)
                cached_result = resolution_cache.get(normalized_target)
                if cached_result is None:
                    cached_result = self._resolve_vfs_file(normalized_target)
                    resolution_cache[normalized_target] = cached_result
                source, relative, resolution = cached_result
                processed += 1
                if not source or not source.exists():
                    resolution_log.append(f"[MISS] {resolution}")
                    continue

                destination = target_dir / relative
                destination_key = str(destination).lower()
                if destination_key in seen_destinations:
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                seen_destinations.add(destination_key)
                copied += 1
                resolution_log.append(f"[COPY] {resolution}\ncopy target='{relative}'")

        return "\n".join(
            [
                f"Prepared {target_name}.",
                f"Patches scanned: {len(targets_by_entry)}",
                f"Targets processed: {processed}",
                f"Files copied: {copied}",
                "",
                "Resolution log:",
                *resolution_log,
            ]
        )


    def _set_status_text(self, text: str):
        self._runner_log_text = text
        if self._runner_dialog is not None:
            self._runner_dialog.set_log_text(text)
        QApplication.processEvents()


    def _set_status_with_prefix(self, prefix: str, text: str):
        combined = f"{prefix.rstrip()}\n\n{text}" if prefix else text
        self._set_status_text(combined)


    def _append_status_text(self, text: str):
        if self._runner_log_text:
            self._runner_log_text = f"{self._runner_log_text.rstrip()}\n\n{text}"
        else:
            self._runner_log_text = text
        if self._runner_dialog is not None:
            self._runner_dialog.set_log_text(self._runner_log_text)
        QApplication.processEvents()


    def _set_runner_busy(self, running: bool, status: str = "Running"):
        if self._runner_dialog is not None:
            self._runner_dialog.set_running(running, status)


    def _set_runner_activity(self, status: str):
        if self._runner_dialog is not None:
            self._runner_dialog.set_busy_status(status)


    def _open_runner_dialog(self):
        if self._runner_dialog is None:
            self._runner_dialog = _PatcherRunnerDialog(self, self)
            if self._runner_log_text:
                self._runner_dialog.set_log_text(self._runner_log_text)
        self._runner_dialog.show()
        self._runner_dialog.raise_()
        self._runner_dialog.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)


    def _stop_patcher(self):
        self._stop_patcher_requested = True
        self._append_status_text("Stop requested.")
        if self._runner_dialog is not None:
            self._runner_dialog.set_running(True, "Stopping")
        process = self._current_patcher_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass


    def _prepare_patcher_mod(self, manage_busy: bool = True):
        enabled_entries = [entry for entry in self._entries if entry.enabled]
        if not enabled_entries:
            self._set_status_text("No enabled patches to prepare.")
            return

        patcher_mod_name = self._patcher_mod_name()
        patcher_dir = self._patcher_mod_dir()
        self._stop_patcher_requested = False
        if manage_busy:
            self._set_runner_busy(True, "Preparing")
        try:
            self._set_status_text(f"Preparing {patcher_mod_name}...\nClearing target folder...")
            disabled_mods = self._disable_active_tslpatcher_mods()
            if disabled_mods:
                self._refresh_now()
                self._append_status_text(
                    "Disabled active TSLPatcher mods in MO2 before prepare:\n" + "\n".join(disabled_mods)
                )
            log_prefix = self._runner_log_text
            prepare_log = self._prepare_target_dir_for_entries(patcher_dir, enabled_entries, patcher_mod_name, log_prefix)
            self._activate_patcher_output_mod(patcher_mod_name)
            self._set_status_with_prefix(log_prefix, prepare_log)
        finally:
            if manage_busy:
                self._set_runner_busy(False)


    def clear_generated_patcher_mod(self):
        self._clear_patcher_mod_dir(self._patcher_mod_dir())


    def _test_entry_target_dir(self, entry: _PatcherEntry) -> Path:
        return Path(__file__).resolve().parent / "test" / self._safe_name(f"{entry.mod_name}_{entry.patch_name}")


    def _prepare_test_entry(self, entry: _PatcherEntry) -> str:
        self._stop_patcher_requested = False
        test_dir = self._test_entry_target_dir(entry)
        return self._prepare_target_dir_for_entries(
            test_dir,
            [entry],
            f"test folder for {entry.mod_name} / {entry.patch_name}",
            update_runner_log=False,
        )


    def _run_test_entry(self, entry: _PatcherEntry) -> str:
        exe_path = Path(__file__).resolve().parent / "HoloPatcher.exe"
        temp_root = Path(__file__).resolve().parent / "temp"
        log_dir = Path(__file__).resolve().parent / "logs"
        label = f"{entry.mod_name} / {entry.patch_name}"
        test_dir = self._test_entry_target_dir(entry)

        if not exe_path.exists():
            return f"HoloPatcher not found:\n{exe_path}"

        self._stop_patcher_requested = False
        prepare_log = self._prepare_test_entry(entry)
        if self._stop_patcher_requested:
            return f"{prepare_log}\n\nRun stopped by user during prepare."

        temp_root.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            f"=== Test Run: {label} ===",
            "",
            prepare_log,
            "",
            f"Installing into: {test_dir}",
        ]

        try:
            temp_mod, error = self._stage_patch_for_run(entry, temp_root)
            if temp_mod is None:
                lines.extend(["", f"SKIPPED: {error}"])
                return "\n".join(lines)

            temp_patch = temp_mod / "tslpatchdata"
            cmd = [
                str(exe_path),
                "--install",
                "--game-dir",
                str(test_dir),
                "--tslpatchdata",
                str(temp_patch),
            ]
            process = subprocess.Popen(cmd)
            self._current_patcher_process = process
            while process.poll() is None:
                QApplication.processEvents()
                if self._stop_patcher_requested:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    break
                time.sleep(0.05)

            install_log = temp_mod / "installlog.txt"
            if install_log.exists():
                shutil.copy2(install_log, log_dir / f"{self._safe_name(label)}_test.txt")
                raw_install_log = install_log.read_text(encoding="utf-8", errors="ignore").strip()
                install_log_text, patch_error_count, patch_warning_count, patch_aborted = self._parse_install_log_summary(raw_install_log)
                if install_log_text:
                    lines.extend(["", "HoloPatcher log:", install_log_text])
            else:
                patch_error_count = 0
                patch_warning_count = 0
                patch_aborted = False

            lines.append("")
            if self._stop_patcher_requested:
                lines.append("STOPPED")
            elif patch_aborted or patch_error_count > 0:
                lines.append("FAILED: install log reported errors")
            elif process.returncode == 0:
                status = "SUCCESS"
                if patch_warning_count > 0:
                    status += f" ({patch_warning_count} warning(s))"
                lines.append(status)
            else:
                lines.append(f"FAILED: exit {process.returncode}")
            return "\n".join(lines)
        except Exception as exc:
            lines.extend(["", f"ERROR: {exc}"])
            return "\n".join(lines)
        finally:
            self._current_patcher_process = None
            self._remove_tree_if_exists(temp_root)


    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^\w\-.]+", "_", value)


    @classmethod
    def _remove_tree_if_exists(cls, path: Path) -> None:
        if path.exists():
            cls._remove_tree(path)


    @staticmethod
    def _remove_tree(path: Path) -> None:
        def _retry_writeable(function, failed_path, exc_info):
            try:
                os.chmod(failed_path, stat.S_IWRITE)
                function(failed_path)
            except Exception:
                raise exc_info[1]

        shutil.rmtree(path, onerror=_retry_writeable)


    def _run_order_entries(self) -> list[_PatcherEntry]:
        enabled_by_key: dict[str, bool] = {}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            enabled_by_key[self._entry_key(item)] = item.checkState(COL_ENABLED) == Qt.CheckState.Checked

        enabled_entries = [
            entry
            for entry in self._entries
            if enabled_by_key.get(f"{entry.mod_name}::{entry.patch_name}", entry.enabled)
        ]
        return [
            entry
            for _original_index, entry in sorted(
                enumerate(enabled_entries),
                key=lambda item: (item[1].priority, item[0]),
            )
        ]


    @staticmethod
    def _parse_install_log_summary(install_log_text: str) -> tuple[str, int, int, bool]:
        cleaned_lines: list[str] = []
        error_count = 0
        warning_count = 0
        aborted = False

        match = re.search(
            r"installation is complete with\s+(\d+)\s+errors?\s+and\s+(\d+)\s+warnings?",
            install_log_text,
            flags=re.IGNORECASE,
        )
        if match:
            error_count = int(match.group(1))
            warning_count = int(match.group(2))

        for line in install_log_text.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("total patches:"):
                continue
            if "installation was aborted with errors" in lower or "importerror:" in lower:
                aborted = True
            cleaned_lines.append(line)

        cleaned_text = "\n".join(cleaned_lines).strip()
        return cleaned_text, error_count, warning_count, aborted


    def _find_entry_patch_dir(self, entry: _PatcherEntry) -> Path | None:
        mod_path = Path(self._organizer.modsPath()) / entry.mod_name
        return self._find_patch_dir(mod_path)


    def _entry_ini_path(self, entry: _PatcherEntry) -> Path | None:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return None
        ini_path = patch_dir / Path(entry.ini_short_path.replace("/", "\\"))
        if ini_path.exists():
            return ini_path
        fallback = patch_dir / "changes.ini"
        return fallback if fallback.exists() else None


    def _entry_open_folder_path(self, entry: _PatcherEntry) -> Path | None:
        ini_path = self._entry_ini_path(entry)
        if ini_path is not None:
            return ini_path.parent
        return self._find_entry_patch_dir(entry)


    def _entry_namespace_info_name(self, entry: _PatcherEntry) -> str:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return ""

        namespaces_ini = patch_dir / "namespaces.ini"
        if not namespaces_ini.exists():
            return ""

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            read_ini_with_fallbacks(parser, namespaces_ini)
        except Exception:
            return ""

        if not parser.has_section(entry.patch_name):
            return ""

        return parser.get(entry.patch_name, "InfoName", fallback="").strip()


    def _entry_info_rtf_path(self, entry: _PatcherEntry) -> Path | None:
        ini_path = self._entry_ini_path(entry)
        patch_dir = self._find_entry_patch_dir(entry)
        info_name = self._entry_namespace_info_name(entry)
        candidates: list[Path | None] = []

        if info_name:
            info_rel = Path(info_name.replace("/", "\\"))
            if info_rel.is_absolute():
                candidates.append(info_rel)
            else:
                if ini_path:
                    candidates.append(ini_path.parent / info_rel)
                if patch_dir:
                    candidates.append(patch_dir / info_rel)

        candidates.extend(
            [
                (ini_path.parent / "info.rtf") if ini_path else None,
                (patch_dir / "info.rtf") if patch_dir else None,
            ]
        )

        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None


    def _entry_log_path(self, entry: _PatcherEntry) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        return log_dir / f"{self._safe_name(f'{entry.mod_name} / {entry.patch_name}')}.txt"


    def _extract_rtf_text(self, rtf_path: Path) -> str | None:
        if not rtf_path.exists():
            return None

        try:
            return _rtf_to_text(rtf_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None


    def _stage_patch_for_run(self, entry: _PatcherEntry, temp_root: Path) -> tuple[Path | None, str]:
        patch_dir = self._find_entry_patch_dir(entry)
        if patch_dir is None:
            return None, "No tslpatchdata folder found"

        ini_rel = Path(entry.ini_short_path.replace("/", "\\"))
        ini_abs = patch_dir / ini_rel
        if not ini_abs.exists():
            fallback = patch_dir / "changes.ini"
            if fallback.exists():
                ini_abs = fallback
            else:
                return None, f"INI not found: {entry.ini_short_path}"

        temp_mod = temp_root / self._safe_name(f"{entry.mod_name}_{entry.patch_name}")
        temp_patch = temp_mod / "tslpatchdata"
        if temp_mod.exists():
            try:
                self._remove_tree(temp_mod)
            except Exception as exc:
                return None, f"Failed to clear temp folder: {exc}"
        temp_patch.mkdir(parents=True, exist_ok=True)

        ini_folder = ini_abs.parent
        shutil.copytree(ini_folder, temp_patch, dirs_exist_ok=True)

        info_path = temp_patch / "info.rtf"
        if not info_path.exists():
            info_path.write_text(r"{\rtf1\ansi Patcher auto-generated info.rtf}", encoding="ascii")

        namespace_path = temp_patch / "namespaces.ini"
        if namespace_path.exists():
            try:
                namespace_path.unlink()
            except OSError:
                pass

        copied_ini = temp_patch / ini_abs.name
        fixed_ini = temp_patch / "changes.ini"
        if not copied_ini.exists():
            return None, f"INI missing after copy: {copied_ini}"
        if copied_ini.name.lower() != "changes.ini":
            if fixed_ini.exists():
                fixed_ini.unlink()
            copied_ini.rename(fixed_ini)

        return temp_mod, ""


    def _run_patcher(self):
        enabled_entries = self._run_order_entries()
        if not enabled_entries:
            self._set_status_text("No enabled patches to run.")
            return

        patcher_dir = self._patcher_mod_dir()
        exe_path = Path(__file__).resolve().parent / "HoloPatcher.exe"
        temp_root = Path(__file__).resolve().parent / "temp"
        log_dir = Path(__file__).resolve().parent / "logs"

        if not exe_path.exists():
            self._set_status_text(f"HoloPatcher not found:\n{exe_path}")
            return

        self._stop_patcher_requested = False
        self._set_runner_busy(True, "Running")
        try:
            self._prepare_patcher_mod(manage_busy=False)
            if self._stop_patcher_requested:
                self._append_status_text("Run stopped by user during prepare.")
                return
            temp_root.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            lines = ["=== Patcher Run ===", ""]
            self._append_status_text("\n".join(lines))
            failures = 0
            warning_count = 0
            error_count = 0
            warning_mods: list[str] = []
            error_mods: list[str] = []

            for index, entry in enumerate(enabled_entries, start=1):
                if self._stop_patcher_requested:
                    lines.append("Run stopped by user.")
                    break
                label = f"{entry.mod_name} / {entry.patch_name}"
                lines.append(f"[{index}/{len(enabled_entries)}] {label}")
                self._set_runner_activity(f"Running {index}/{len(enabled_entries)}")
                self._set_status_text(f"{self._runner_log_text.rstrip()}\n[{index}/{len(enabled_entries)}] {label}")

                temp_mod, error = self._stage_patch_for_run(entry, temp_root)
                if temp_mod is None:
                    lines.append(f"  SKIPPED: {error}")
                    failures += 1
                    self._append_status_text(f"[{index}/{len(enabled_entries)}] {label}\n  SKIPPED: {error}")
                    continue

                temp_patch = temp_mod / "tslpatchdata"
                cmd = [
                    str(exe_path),
                    "--install",
                    "--game-dir",
                    str(patcher_dir),
                    "--tslpatchdata",
                    str(temp_patch),
                ]
                try:
                    process = subprocess.Popen(
                        cmd,
                    )
                    self._current_patcher_process = process
                    while process.poll() is None:
                        QApplication.processEvents()
                        if self._stop_patcher_requested:
                            try:
                                process.terminate()
                            except Exception:
                                pass
                            break
                        time.sleep(0.05)
                    install_log = temp_mod / "installlog.txt"
                    install_log_text = ""
                    patch_aborted = False
                    patch_error_count = 0
                    patch_warning_count = 0
                    if install_log.exists():
                        shutil.copy2(install_log, log_dir / f"{self._safe_name(label)}.txt")
                        raw_install_log = install_log.read_text(encoding="utf-8", errors="ignore").strip()
                        install_log_text, patch_error_count, patch_warning_count, patch_aborted = self._parse_install_log_summary(raw_install_log)
                        warning_count += patch_warning_count
                        error_count += patch_error_count
                        if patch_aborted and patch_error_count == 0:
                            error_count += 1
                        if patch_warning_count and label not in warning_mods:
                            warning_mods.append(label)
                        if patch_error_count and label not in error_mods:
                            error_mods.append(label)
                        if patch_aborted and label not in error_mods:
                            error_mods.append(label)
                    if self._stop_patcher_requested:
                        lines.append("  STOPPED")
                        failures += 1
                        block = f"[{index}/{len(enabled_entries)}] {label}"
                        if install_log_text:
                            block += f"\n\nHoloPatcher log:\n{install_log_text}"
                        block += "\n\n  STOPPED"
                        self._append_status_text(block)
                        break
                    if patch_aborted or patch_error_count > 0:
                        lines.append("  FAILED: install log reported errors")
                        failures += 1
                        status_line = "  FAILED: install log reported errors"
                    elif process.returncode == 0:
                        lines.append("  SUCCESS")
                        status_line = "  SUCCESS"
                    else:
                        lines.append(f"  FAILED: exit {process.returncode}")
                        failures += 1
                        status_line = f"  FAILED: exit {process.returncode}"
                    block = f"[{index}/{len(enabled_entries)}] {label}"
                    if install_log_text:
                        block += f"\n\nHoloPatcher log:\n{install_log_text}"
                    block += f"\n\n{status_line}"
                    self._append_status_text(block)
                except Exception as exc:
                    lines.append(f"  ERROR: {exc}")
                    failures += 1
                    self._append_status_text(f"[{index}/{len(enabled_entries)}] {label}\n  ERROR: {exc}")
                finally:
                    self._current_patcher_process = None
                    self._remove_tree_if_exists(temp_mod)

            self._remove_tree_if_exists(temp_root)
            lines.append("")
            lines.append(f"Completed with {failures} failure(s).")
            summary_lines = [f"Completed with {failures} failure(s).", ""]
            summary_lines.append(f"Total errors: {error_count}")
            if error_mods:
                summary_lines.append("Mods with errors:")
                summary_lines.extend(error_mods)
            else:
                summary_lines.append("Mods with errors: none")
            summary_lines.append("")
            summary_lines.append(f"Total warnings: {warning_count}")
            if warning_mods:
                summary_lines.append("Mods with warnings:")
                summary_lines.extend(warning_mods)
            else:
                summary_lines.append("Mods with warnings: none")
            self._append_status_text("\n".join(summary_lines))
        finally:
            self._set_runner_busy(False)
            self._current_patcher_process = None
            self._remove_dummy_game_exes(patcher_dir)
            refresh_mo2(self._organizer, self)


    @staticmethod
    def _entry_key(item: QTreeWidgetItem) -> str:
        return f"{item.text(COL_MOD)}::{item.text(COL_PATCH)}"


    def _entry_by_key(self, entry_key: str) -> _PatcherEntry | None:
        return next(
            (entry for entry in self._entries if f"{entry.mod_name}::{entry.patch_name}" == entry_key),
            None,
        )


    def _selected_conflict_rows(self, active_item: QTreeWidgetItem) -> list[tuple[str, str]]:
        if active_item.checkState(COL_ENABLED) != Qt.CheckState.Checked:
            return []

        active_entry = self._entry_by_key(self._entry_key(active_item))
        if active_entry is None:
            return []
        active_hard_keys = self._hard_operation_conflict_keys(active_entry)
        active_info_keys = self._informational_operation_conflict_keys(active_entry)
        if not active_hard_keys and not active_info_keys:
            return []

        conflicts: list[tuple[str, str]] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item is active_item:
                continue
            if item.checkState(COL_ENABLED) != Qt.CheckState.Checked:
                continue
            entry = self._entry_by_key(self._entry_key(item))
            if entry is None:
                continue
            shared_hard_keys = sorted(active_hard_keys.intersection(self._hard_operation_conflict_keys(entry)))
            shared_info_keys = sorted(active_info_keys.intersection(self._informational_operation_conflict_keys(entry)))
            if not shared_hard_keys and not shared_info_keys:
                continue
            other_label = f"{item.text(COL_MOD)} / {item.text(COL_PATCH)}"
            details: list[str] = []
            if shared_hard_keys:
                details.append("Hard conflicts (replace):\n" + "\n".join(shared_hard_keys))
            if shared_info_keys:
                details.append("Informational overlaps (install/patch):\n" + "\n".join(shared_info_keys))
            conflicts.append((other_label, "\n\n".join(details)))
        return conflicts


    def _update_conflict_overview(self, *_args):
        if not hasattr(self, "_conflict_overview"):
            return
        row_colors: list[QColor | None] = []
        selected_item = self._tree.currentItem()
        selected_marker = tree_selected_marker_color(self._tree)
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            color_name = item.data(COL_ENABLED, ROLE_OVERVIEW_COLOR)
            color = QColor(str(color_name)) if color_name else None
            if item is selected_item:
                color = color if color is not None else selected_marker
            row_colors.append(color)
        self._conflict_overview.set_row_colors(row_colors)


    def _build_conflict_styles(self, entries: list[_PatcherEntry]) -> tuple[dict[str, QBrush], dict[str, QColor]]:
        conflict_brushes: dict[str, QBrush] = {}
        overview_colors: dict[str, QColor] = {}
        if not self._active_conflict_key:
            return conflict_brushes, overview_colors

        active_entry = next(
            (
                entry for entry in entries
                if f"{entry.mod_name}::{entry.patch_name}" == self._active_conflict_key and entry.enabled
            ),
            None,
        )
        if active_entry is None:
            return conflict_brushes, overview_colors

        active_hard_keys = self._hard_operation_conflict_keys(active_entry)
        active_info_keys = self._informational_operation_conflict_keys(active_entry)
        if not active_hard_keys and not active_info_keys:
            return conflict_brushes, overview_colors

        priority_order = {
            f"{entry.mod_name}::{entry.patch_name}": index
            for index, (_original_index, entry) in enumerate(
                sorted(enumerate(entries), key=lambda item: (item[1].priority, item[0]))
            )
        }
        active_rank = priority_order.get(self._active_conflict_key)
        if active_rank is None:
            return conflict_brushes, overview_colors

        overview_colors[self._active_conflict_key] = tree_selected_marker_color(self._tree)
        for entry in entries:
            if not entry.enabled:
                continue
            entry_key = f"{entry.mod_name}::{entry.patch_name}"
            if entry_key == self._active_conflict_key:
                continue

            has_hard_conflict = bool(active_hard_keys.intersection(self._hard_operation_conflict_keys(entry)))
            has_info_conflict = bool(active_info_keys.intersection(self._informational_operation_conflict_keys(entry)))
            if has_hard_conflict:
                entry_rank = priority_order.get(entry_key)
                if entry_rank is not None and entry_rank < active_rank:
                    conflict_color = tree_conflict_row_color(self._tree, mo2_conflict_green(), 0.34)
                else:
                    conflict_color = tree_conflict_row_color(self._tree, mo2_conflict_red(), 0.34)
                conflict_brushes[entry_key] = QBrush(conflict_color)
                overview_colors[entry_key] = conflict_color
            elif has_info_conflict:
                conflict_color = tree_conflict_row_color(self._tree, mo2_archive_conflict_purple(), 0.34)
                conflict_brushes[entry_key] = QBrush(conflict_color)
                overview_colors[entry_key] = conflict_color
        return conflict_brushes, overview_colors


    def _rebuild_tree_from_entries(self):
        conflict_brushes, overview_colors = self._build_conflict_styles(self._entries)
        patch_conflict_states = self._build_patch_conflict_states(self._entries)

        self._tree.blockSignals(True)
        self._tree.clear()
        for entry in self._entries:
            item = _PatcherItem(["", "", entry.mod_name, entry.patch_name, entry.description, str(entry.priority)])
            for col in range(PATCHER_TREE_COLUMN_COUNT):
                item.setSizeHint(col, self._patcher_row_size)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(COL_ENABLED, Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked)
            item.setData(COL_PRIORITY, Qt.ItemDataRole.UserRole, entry.priority)
            item.setData(COL_ENABLED, ROLE_INI_PATH, entry.ini_short_path)
            item.setData(COL_ENABLED, ROLE_DESTINATION, entry.destination)
            item.setData(COL_ENABLED, ROLE_INSTALL_PATHS, entry.install_paths)
            item.setData(COL_ENABLED, ROLE_REQUIRED, entry.required)
            item.setData(COL_ENABLED, ROLE_FILES, entry.files)
            item.setToolTip(COL_DESCRIPTION, entry.description)
            item.setToolTip(
                COL_DESCRIPTION,
                entry.description if not entry.files else f"{entry.description}\n\nFiles: {entry.files}",
            )
            item_key = f"{entry.mod_name}::{entry.patch_name}"
            conflict_state = patch_conflict_states.get(item_key)
            if conflict_state:
                state, tooltip = conflict_state
                item.setIcon(COL_CONFLICTS, self._patch_conflict_icon(state))
                item.setToolTip(COL_CONFLICTS, tooltip)
                item.setData(COL_CONFLICTS, ROLE_CONFLICT_SORT, self._patch_conflict_sort_weight(state))
            else:
                item.setData(COL_CONFLICTS, ROLE_CONFLICT_SORT, 0)
            brush = conflict_brushes.get(item_key)
            overview_color = overview_colors.get(item_key)
            item.setData(COL_ENABLED, ROLE_OVERVIEW_COLOR, overview_color.name() if overview_color else "")
            if brush is not None:
                for col in range(PATCHER_TREE_COLUMN_COUNT):
                    item.setBackground(col, brush)
            self._tree.addTopLevelItem(item)
        self._tree.blockSignals(False)
        self._tree.sortItems(self._tree.sortColumn(), self._tree.header().sortIndicatorOrder())
        self._restore_active_selection()
        self._update_conflict_overview()


    def _restore_active_selection(self):
        if not self._active_conflict_key:
            return
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if self._entry_key(item) == self._active_conflict_key:
                self._tree.setCurrentItem(item)
                return


    def _write_json(self):
        payload = {"patches": []}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            payload["patches"].append({
                "enabled": item.checkState(COL_ENABLED) == Qt.CheckState.Checked,
                "priority": int(item.text(COL_PRIORITY)) if item.text(COL_PRIORITY).isdigit() else -1,
                "mod_name": item.text(COL_MOD),
                "patch_name": item.text(COL_PATCH),
                "description": item.text(COL_DESCRIPTION),
                "ini_short_path": item.data(COL_ENABLED, ROLE_INI_PATH) or "",
                "destination": item.data(COL_ENABLED, ROLE_DESTINATION) or "",
                "install_paths": item.data(COL_ENABLED, ROLE_INSTALL_PATHS) or "",
                "files": item.data(COL_ENABLED, ROLE_FILES) or "",
                "required": item.data(COL_ENABLED, ROLE_REQUIRED) or "",
            })
        self._json_path.parent.mkdir(parents=True, exist_ok=True)
        self._json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


    def schedule_refresh(self, immediate: bool = False):
        self._refresh_pending = True
        if not self.isVisible() and not immediate:
            return
        self._refresh_timer.start(0 if immediate else self._refresh_timer.interval())


    def refresh(self):
        self.schedule_refresh(immediate=True)


    def run_after_sync(self):
        self._open_runner_dialog()
        self._refresh_pending = False
        self._last_profile_order = tuple(self._profile_mod_order())
        self._entries = self._collect_patch_entries()
        self._rebuild_tree_from_entries()
        self._update_summary()
        self._write_json()
        self._run_patcher()


    def _refresh_now(self):
        if not self.isVisible() and self._tree.topLevelItemCount():
            return
        self._refresh_pending = False
        self._last_profile_order = tuple(self._profile_mod_order())
        self._entries = self._collect_patch_entries()
        self._rebuild_tree_from_entries()
        self._update_summary()
        self._write_json()


    def _update_summary(self):
        total = self._tree.topLevelItemCount()
        enabled = sum(
            1 for i in range(total) if self._tree.topLevelItem(i).checkState(COL_ENABLED) == Qt.CheckState.Checked
        )
        self._summary_label.setText(f"{enabled}/{total} patches enabled")

    def _on_item_changed(self, _item: QTreeWidgetItem, _column: int):
        self._update_summary()
        self._pending_checkbox_sync = True
        self._checkbox_sync_timer.start()


    def _flush_item_changes(self):
        if not self._pending_checkbox_sync:
            return
        self._pending_checkbox_sync = False
        self._write_json()
        enabled_by_key = {
            self._entry_key(self._tree.topLevelItem(i)): self._tree.topLevelItem(i).checkState(COL_ENABLED)
            == Qt.CheckState.Checked
            for i in range(self._tree.topLevelItemCount())
        }
        for entry in self._entries:
            entry.enabled = enabled_by_key.get(f"{entry.mod_name}::{entry.patch_name}", entry.enabled)
        self._rebuild_tree_from_entries()


    def _flush_pending_click(self):
        if not self._pending_click_entry_key:
            return
        self._active_conflict_key = self._pending_click_entry_key
        self._pending_click_entry_key = None
        self._rebuild_tree_from_entries()


    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int):
        if _column == COL_ENABLED:
            return
        self._highlight_mo2_mod(item.text(COL_MOD))
        self._pending_click_entry_key = self._entry_key(item)
        self._click_select_timer.start()


    def _on_current_item_changed(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None):
        if item is None:
            return
        self._highlight_mo2_mod(item.text(COL_MOD))


    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int):
        self._click_select_timer.stop()
        self._pending_click_entry_key = None
        self._highlight_mo2_mod(item.text(COL_MOD))
        self._show_item_information(item)


    def _on_tree_context_menu(self, pos: QPoint):
        item = self._tree.itemAt(pos)
        if item is None:
            return

        self._click_select_timer.stop()
        self._pending_click_entry_key = None
        entry_key = self._entry_key(item)
        entry = next(
            (entry for entry in self._entries if f"{entry.mod_name}::{entry.patch_name}" == entry_key),
            None,
        )
        menu = QMenu(self)
        info_action = menu.addAction("Information")
        open_folder_action = menu.addAction("Open in Explorer")
        if entry is None or self._entry_open_folder_path(entry) is None:
            open_folder_action.setEnabled(False)
        chosen_action = menu.exec(self._tree.viewport().mapToGlobal(pos))
        if chosen_action is info_action:
            self._show_item_information(item)
        elif chosen_action is open_folder_action and entry is not None:
            folder_path = self._entry_open_folder_path(entry)
            if folder_path is not None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_path)))


    def _show_item_information(self, item: QTreeWidgetItem):
        entry_key = self._entry_key(item)
        entry = next(
            (entry for entry in self._entries if f"{entry.mod_name}::{entry.patch_name}" == entry_key),
            None,
        )
        if entry is None:
            return
        info_path = self._entry_info_rtf_path(entry)
        ini_path = self._entry_ini_path(entry)
        log_path = self._entry_log_path(entry)
        info_text = self._extract_rtf_text(info_path) if info_path else ""
        ini_text = ini_path.read_text(encoding="utf-8", errors="ignore") if ini_path else ""
        log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        conflict_rows = self._selected_conflict_rows(item)
        dialog = _PatcherDetailsDialog(self, self, entry, conflict_rows, info_text, info_path, ini_text, log_text)
        dialog.exec()
