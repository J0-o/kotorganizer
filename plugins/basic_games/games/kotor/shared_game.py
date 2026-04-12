import os
from pathlib import Path
import winreg

import mobase
from PyQt6.QtCore import QDir, QTimer, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from basic_games.basic_features import BasicModDataChecker, GlobPatterns
from basic_games.basic_features.utils import is_directory


class KotorModDataCheckerBase(BasicModDataChecker):
    _valid_map = {
        "override": (
            ".2da", ".are", ".bik", ".dlg", ".dds", ".fac", ".git",
            ".ifo", ".jrl", ".lip", ".lyt", ".mdl", ".mdx", ".mp3",
            ".ncs", ".nss", ".ssf", ".tga", ".tpc", ".txi", ".utc",
            ".utd", ".ute", ".uti", ".utm", ".utp", ".uts", ".utt",
            ".utw", ".wav", ".wok",
        ),
        "movies": (".bik",),
        "data": (".bif",),
        "lips": (".mod",),
        "modules": (".erf", ".rim", ".mod"),
        "streammusic": (".wav",),
        "streamsounds": (".wav",),
        "streamvoice": (".wav",),
        "texturepacks": (".erf",),
    }

    _ignored_exts = (
        ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".gif",
        ".md", ".rtf", ".doc", ".docx", ".ini", ".html", ".url",
        ".log", ".bak", ".xml", ".docx#",
    )

    _restricted_dirs = {"data"}

    def __init__(self):
        all_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        self._all_valid_exts = {ext.lower() for ext in all_exts}
        super().__init__(GlobPatterns(all_exts))

    def _iter_dirs(self, node):
        for entry in list(node):
            if is_directory(entry):
                yield entry
                yield from self._iter_dirs(entry)

    def _find_dirs_named(self, node, name_lower: str):
        name_lower = name_lower.lower()
        return [directory for directory in self._iter_dirs(node) if directory.name().lower() == name_lower]

    def _is_ignored_source_dir_name(self, name: str) -> bool:
        return name.lower().startswith("source")

    def _is_valid_mod_file(self, file_node) -> bool:
        if is_directory(file_node):
            return False
        file_name = file_node.name().lower()
        if file_name == "dialog.tlk":
            return True
        _, ext = os.path.splitext(file_name)
        if ext in self._ignored_exts:
            return False
        return ext in self._all_valid_exts

    def _file_is_valid_for_path(self, file_node, path: str) -> bool:
        if is_directory(file_node):
            return False
        file_name = file_node.name().lower()
        for folder, exts in self._valid_map.items():
            if folder in path.lower():
                return any(file_name.endswith(ext) for ext in exts)
        return False

    def _cleanup_root(self, filetree: mobase.IFileTree):
        valid_top = set(self._valid_map.keys()) | {"override", "tslpatchdata"}
        ignored = self._ignored_exts
        valid_exts = set(ext for exts in self._valid_map.values() for ext in exts)

        for entry in list(filetree):
            if entry.name().lower() in valid_top:
                continue
            entry.detach()

        override_dirs = self._find_dirs_named(filetree, "override")
        if not override_dirs:
            return

        override = override_dirs[0]
        for child in list(override):
            if is_directory(child):
                child.detach()
                continue

            _, ext = os.path.splitext(child.name().lower())
            if ext in ignored or ext not in valid_exts:
                child.detach()

    def dataLooksValid(self, filetree: mobase.IFileTree) -> mobase.ModDataChecker.CheckReturn:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        if tsl_dirs:
            return mobase.ModDataChecker.FIXABLE

        for directory in self._iter_dirs(filetree):
            if directory.name().lower() in self._restricted_dirs:
                return mobase.ModDataChecker.INVALID

        ignored = self._ignored_exts

        def parent_has_valid_dir(node) -> bool:
            parent = node.parent()
            while parent is not None and parent != filetree:
                if parent.name().lower() in self._valid_map:
                    return True
                parent = parent.parent()
            return False

        for entry in list(filetree):
            if not is_directory(entry):
                continue
            if self._is_ignored_source_dir_name(entry.name()):
                continue
            if entry.name().lower() in self._valid_map:
                continue

            for child in list(entry):
                if self._is_valid_mod_file(child):
                    return mobase.ModDataChecker.FIXABLE

            for child in list(entry):
                if not is_directory(child):
                    continue
                if child.name().lower() in self._valid_map:
                    for grandchild in list(child):
                        if self._is_valid_mod_file(grandchild):
                            return mobase.ModDataChecker.FIXABLE

        for directory in self._iter_dirs(filetree):
            if self._is_ignored_source_dir_name(directory.name()):
                continue
            if directory.name().lower() in self._valid_map:
                continue
            if parent_has_valid_dir(directory):
                continue
            for child in list(directory):
                if self._is_valid_mod_file(child):
                    return mobase.ModDataChecker.FIXABLE

        for folder in self._valid_map:
            if self._find_dirs_named(filetree, folder):
                return mobase.ModDataChecker.VALID

        all_valid_exts = tuple(ext for exts in self._valid_map.values() for ext in exts)
        if any(not is_directory(entry) and entry.name().lower().endswith(all_valid_exts) for entry in filetree):
            return mobase.ModDataChecker.FIXABLE

        if any(not is_directory(entry) and entry.name().lower() == "dialog.tlk" for entry in filetree):
            return mobase.ModDataChecker.VALID

        return mobase.ModDataChecker.INVALID

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree | None:
        tsl_dirs = self._find_dirs_named(filetree, "tslpatchdata")
        valid_dirs = []

        def _display_path(node: mobase.IFileTree) -> str:
            parts = [node.name()]
            parent = node.parent()
            while parent is not None and parent.parent() is not None:
                parts.append(parent.name())
                parent = parent.parent()
            parts.reverse()
            return "/".join(parts)

        def _is_under_tslpatchdata(node: mobase.IFileTree) -> bool:
            parent = node.parent()
            while parent is not None:
                if parent.name().lower() == "tslpatchdata":
                    return True
                parent = parent.parent()
            return False

        def _is_ignored_source_dir(node: mobase.IFileTree) -> bool:
            parent = node.parent()
            while parent is not None:
                if self._is_ignored_source_dir_name(parent.name()):
                    return True
                parent = parent.parent()
            return self._is_ignored_source_dir_name(node.name())

        def _directory_has_direct_valid_mod_file(node: mobase.IFileTree) -> bool:
            return any(self._is_valid_mod_file(child) for child in list(node) if not is_directory(child))

        def _directory_contains_valid_mod_file(node: mobase.IFileTree) -> bool:
            for child in list(node):
                if is_directory(child):
                    if (
                        child.name().lower() == "tslpatchdata"
                        or _is_under_tslpatchdata(child)
                        or _is_ignored_source_dir(child)
                    ):
                        continue
                    if _directory_contains_valid_mod_file(child):
                        return True
                    continue
                if self._is_valid_mod_file(child):
                    return True
            return False

        def _has_qualifying_child_directory(node: mobase.IFileTree) -> bool:
            for child in list(node):
                if not is_directory(child):
                    continue
                if (
                    child.name().lower() == "tslpatchdata"
                    or _is_under_tslpatchdata(child)
                    or _is_ignored_source_dir(child)
                ):
                    continue
                if _directory_contains_valid_mod_file(child):
                    return True
            return False

        def _move_valid_files_to_override(node: mobase.IFileTree):
            for child in list(node):
                if is_directory(child):
                    if (
                        child.name().lower() == "tslpatchdata"
                        or _is_under_tslpatchdata(child)
                        or _is_ignored_source_dir(child)
                    ):
                        continue
                    _move_valid_files_to_override(child)
                    continue
                if self._is_valid_mod_file(child):
                    filetree.move(child, f"override/{child.name()}")

        for directory in self._iter_dirs(filetree):
            if directory.parent() is None:
                continue
            if (
                directory.name().lower() == "tslpatchdata"
                or _is_under_tslpatchdata(directory)
                or _is_ignored_source_dir(directory)
            ):
                continue
            if _directory_has_direct_valid_mod_file(directory) or (
                _directory_contains_valid_mod_file(directory) and not _has_qualifying_child_directory(directory)
            ):
                valid_dirs.append(directory)

        root_files = [entry for entry in list(filetree) if not is_directory(entry)]
        loose_valid = [entry for entry in root_files if self._is_valid_mod_file(entry)]
        total_choices = len(valid_dirs) + (1 if loose_valid else 0)

        if tsl_dirs or total_choices > 1:
            tsl_options: list[str] = []
            tsl_mapping: dict[str, mobase.IFileTree] = {}
            loose_options: list[str] = []
            loose_mapping: dict[str, mobase.IFileTree | None] = {}

            for directory in tsl_dirs:
                display_name = _display_path(directory) if directory.parent() is not None else directory.name()
                name = display_name
                suffix = 2
                while name in tsl_mapping:
                    name = f"{display_name} ({suffix})"
                    suffix += 1
                tsl_options.append(name)
                tsl_mapping[name] = directory

            if loose_valid:
                loose_options.append("(root)")
                loose_mapping["(root)"] = None

            for directory in valid_dirs:
                name = _display_path(directory)
                suffix = 2
                while name in loose_mapping:
                    name = f"{_display_path(directory)} ({suffix})"
                    suffix += 1
                loose_options.append(name)
                loose_mapping[name] = directory

            def _choose_install_sources(
                tsl_labels: list[str], loose_labels: list[str]
            ) -> tuple[str | None, list[str], bool]:
                dialog = QDialog()
                dialog.setWindowTitle("Select Install Sources")
                layout = QVBoxLayout(dialog)
                layout.addWidget(QLabel("Choose either one TSLPatcher folder or one or more loose-file sources. If more are needed, then install the archive again, as a separate mod."))

                content_row = QHBoxLayout()
                layout.addLayout(content_row)

                tsl_panel = QWidget(dialog)
                tsl_layout = QVBoxLayout(tsl_panel)
                tsl_layout.setContentsMargins(0, 0, 0, 0)
                tsl_layout.addWidget(QLabel("TSLPatcher folders"))
                tsl_group = QButtonGroup(dialog)
                tsl_group.setExclusive(True)
                tsl_buttons: list[QRadioButton] = []
                none_button = QRadioButton("None", tsl_panel)
                tsl_group.addButton(none_button)
                tsl_layout.addWidget(none_button)
                tsl_buttons.append(none_button)
                for label in tsl_labels:
                    button = QRadioButton(label, tsl_panel)
                    tsl_group.addButton(button)
                    tsl_layout.addWidget(button)
                    tsl_buttons.append(button)
                if len(tsl_labels) == 1 and not loose_labels:
                    tsl_buttons[-1].setChecked(True)
                else:
                    none_button.setChecked(True)
                tsl_layout.addStretch(1)
                content_row.addWidget(tsl_panel, 1)

                separator = QFrame(dialog)
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                content_row.addWidget(separator)

                loose_panel = QWidget(dialog)
                loose_layout = QVBoxLayout(loose_panel)
                loose_layout.setContentsMargins(0, 0, 0, 0)
                loose_layout.addWidget(QLabel("Loose-file mod sources"))
                loose_layout.addWidget(
                    QLabel("Check sources to install, then drag to set consolidation order.")
                )
                location_list = QListWidget(loose_panel)
                location_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
                location_list.setDefaultDropAction(Qt.DropAction.MoveAction)
                location_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
                for label in loose_labels:
                    item = QListWidgetItem(label)
                    item.setFlags(
                        item.flags()
                        | Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsDragEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEnabled
                    )
                    item.setCheckState(Qt.CheckState.Checked)
                    location_list.addItem(item)
                if not loose_labels:
                    location_list.setEnabled(False)
                loose_layout.addWidget(location_list)

                controls = QHBoxLayout()
                select_all_button = QPushButton("Select All", loose_panel)
                clear_all_button = QPushButton("Clear All", loose_panel)
                controls.addWidget(select_all_button)
                controls.addWidget(clear_all_button)
                controls.addStretch(1)
                loose_layout.addLayout(controls)
                content_row.addWidget(loose_panel, 1)

                def _set_loose_checks(state: Qt.CheckState):
                    for i in range(location_list.count()):
                        location_list.item(i).setCheckState(state)

                def _sync_from_tsl():
                    tsl_selected = any(button.isChecked() and button is not none_button for button in tsl_buttons)
                    location_list.setEnabled(not tsl_selected and bool(loose_labels))
                    select_all_button.setEnabled(not tsl_selected and bool(loose_labels))
                    clear_all_button.setEnabled(not tsl_selected and bool(loose_labels))
                    if tsl_selected:
                        _set_loose_checks(Qt.CheckState.Unchecked)

                def _sync_from_loose(item: QListWidgetItem):
                    if item.checkState() == Qt.CheckState.Checked:
                        for button in tsl_buttons:
                            if button.isChecked():
                                button.setChecked(False)
                        none_button.setChecked(True)
                        _sync_from_tsl()

                for button in tsl_buttons:
                    button.toggled.connect(_sync_from_tsl)

                location_list.itemChanged.connect(_sync_from_loose)
                select_all_button.clicked.connect(lambda: _set_loose_checks(Qt.CheckState.Checked))
                clear_all_button.clicked.connect(lambda: _set_loose_checks(Qt.CheckState.Unchecked))
                _sync_from_tsl()

                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                )
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
                layout.addWidget(buttons)

                if dialog.exec() == int(QDialog.DialogCode.Accepted):
                    selected_tsl = next(
                        (
                            button.text()
                            for button in tsl_buttons
                            if button.isChecked() and button is not none_button
                        ),
                        None,
                    )
                    selected_loose = [
                        location_list.item(i).text()
                        for i in range(location_list.count())
                        if location_list.item(i).checkState() == Qt.CheckState.Checked
                    ]
                    return selected_tsl, selected_loose, True
                return None, [], False

            selected_tsl, selected_loose, ok = _choose_install_sources(tsl_options, loose_options)
            if ok and selected_tsl:
                selected = tsl_mapping[selected_tsl]
                filetree.move(selected, "tslpatchdata")
                for top in list(filetree):
                    if top.name().lower() != "tslpatchdata":
                        top.detach()
                return filetree

            if ok and selected_loose:
                chosen_nodes = [loose_mapping[label] for label in selected_loose if label in loose_mapping]
                if not self._find_dirs_named(filetree, "override"):
                    filetree.addDirectory("override")

                for node in chosen_nodes:
                    if node is None:
                        for file_node in loose_valid:
                            filetree.move(file_node, f"override/{file_node.name()}")
                    else:
                        _move_valid_files_to_override(node)

                for top in list(filetree):
                    if top.name().lower() != "override":
                        top.detach()

                return filetree

            if ok:
                return filetree

            return None

        root_dirs = [entry for entry in list(filetree) if is_directory(entry)]
        if len(root_dirs) == 1:
            keep = root_dirs[0]

            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            def _move_files_only(node: mobase.IFileTree):
                for child in list(node):
                    if is_directory(child):
                        _move_files_only(child)
                    else:
                        filetree.move(child, f"override/{child.name()}")

            _move_files_only(keep)
            self._cleanup_root(filetree)
            return filetree

        if loose_valid:
            if not self._find_dirs_named(filetree, "override"):
                filetree.addDirectory("override")

            for file_node in loose_valid:
                filetree.move(file_node, f"override/{file_node.name()}")

            self._cleanup_root(filetree)
            return filetree

        return filetree


class KotorGameMixin:
    _logger = None
    _log_prefix = "KOTOR"
    _workshop_app_id = ""
    _workshop_game_name = "KOTOR"
    _workshop_warning_text = ""

    def game_directories(self) -> list[QDir]:
        return [
            self.dataDirectory(),
            self.lipsDirectory(),
            self.modulesDirectory(),
            self.moviesDirectory(),
            self.overrideDirectory(),
            self.streamMusicDirectory(),
            self.streamSoundsDirectory(),
            self.streamVoiceDirectory(),
            self.texturePacksDirectory(),
            self.savesDirectory(),
        ]

    def dataDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/Data")

    def lipsDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/Lips")

    def modulesDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/Modules")

    def moviesDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/Movies")

    def overrideDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/Override")

    def streamMusicDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/StreamMusic")

    def streamSoundsDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/StreamSounds")

    def streamVoiceDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/StreamVoice")

    def texturePacksDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/TexturePacks")

    def savesDirectory(self):
        return QDir(self.gameDirectory().absolutePath() + "/saves")

    def getModMappings(self) -> dict[str, list[str]]:
        return {
            "Data": [self.dataDirectory().absolutePath()],
            "Lips": [self.lipsDirectory().absolutePath()],
            "Modules": [self.modulesDirectory().absolutePath()],
            "Movies": [self.moviesDirectory().absolutePath()],
            "Override": [self.overrideDirectory().absolutePath()],
            "StreamMusic": [self.streamMusicDirectory().absolutePath()],
            "StreamSounds": [self.streamSoundsDirectory().absolutePath()],
            "StreamVoice": [self.streamVoiceDirectory().absolutePath()],
            "TexturePacks": [self.texturePacksDirectory().absolutePath()],
        }

    def _active_mod_paths(self):
        mods_root = Path(self._organizer.modsPath())
        modlist = self._organizer.modList().allModsByProfilePriority()

        for mod_name in modlist:
            if self._organizer.modList().state(mod_name) & mobase.ModState.ACTIVE:
                yield mods_root / mod_name

    def mappings(self) -> list[mobase.Mapping]:
        mappings = []
        game_path = Path(self.gameDirectory().absolutePath())

        for mod_path in self._active_mod_paths():
            if not mod_path.exists():
                continue
            for child in mod_path.iterdir():
                if child.name.lower() != "dialog.tlk":
                    continue
                mappings.append(
                    mobase.Mapping(
                        source=str(child),
                        destination=str(game_path / "dialog.tlk"),
                        is_directory=False,
                        create_target=False,
                    )
                )

        return mappings

    def _log_platform_once(self, force: bool = False) -> bool:
        if self._platform_logged and not force:
            return True
        try:
            game_dir = self.gameDirectory()
            steam_root = self._detect_steam_root(Path(game_dir.absolutePath()))
            self._warn_if_workshop_present(steam_root)
            self._logger.info(
                "[%s] Steam detected:%s path:%s steam_root:%s",
                self._log_prefix,
                self.is_steam(),
                game_dir.absolutePath(),
                steam_root,
            )
            self._platform_logged = True
        except Exception as exc:
            self._logger.info("[%s] Platform logging failed: %s", self._log_prefix, exc)
        return True

    def _detect_steam_root(self, game_path: Path) -> str:
        try:
            parts = [part.lower() for part in game_path.parts]
            if "steamapps" in parts:
                idx = parts.index("steamapps")
                return str(Path(*game_path.parts[:idx]))
        except Exception:
            pass

        reg_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for hive, key, value in reg_paths:
            try:
                with winreg.OpenKeyEx(hive, key, 0, winreg.KEY_READ) as reg_key:
                    data, _ = winreg.QueryValueEx(reg_key, value)
                    if data:
                        return str(Path(str(data)))
            except FileNotFoundError:
                continue
            except Exception:
                continue

        return "unknown"

    def _warn_if_workshop_present(self, steam_root: str):
        if steam_root.lower() == "unknown":
            return

        workshop_path = Path(steam_root) / "steamapps" / "workshop" / "content" / self._workshop_app_id
        try:
            if workshop_path.exists() and any(workshop_path.iterdir()):
                self._logger.warning("[%s] Steam Workshop content detected", self._log_prefix)
                try:
                    QTimer.singleShot(
                        2000,
                        lambda: QMessageBox.warning(
                            None,
                            self._workshop_game_name,
                            self._workshop_warning_text,
                        ),
                    )
                except Exception:
                    pass
        except Exception as exc:
            self._logger.debug("[%s] Workshop check failed: %s", self._log_prefix, exc)

    def _init_custom_tabs_common(self, main_window: QMainWindow, texture_tab_type, patcher_tab_type):
        if self._organizer.managedGame() != self:
            return

        tab_widget: QTabWidget | None = main_window.findChild(QTabWidget, "tabWidget")
        if not tab_widget:
            return

        data_index = None
        saves_index = None
        textures_index = None
        for i in range(tab_widget.count()):
            text = tab_widget.tabText(i).lower()
            if text == "data":
                data_index = i
            if text == "saves":
                saves_index = i
            if text == "textures":
                textures_index = i

        insert_index = tab_widget.count()
        if data_index is not None:
            insert_index = data_index + 1
        elif saves_index is not None:
            insert_index = saves_index

        if textures_index is None:
            self._texture_tab = texture_tab_type(main_window, self._organizer, self)
            tab_widget.insertTab(insert_index, self._texture_tab, "Textures")
            textures_index = insert_index

        patcher_index = None
        for i in range(tab_widget.count()):
            if tab_widget.tabText(i).lower() == "patcher":
                patcher_index = i
                break
        if patcher_index is None:
            self._patcher_tab = patcher_tab_type(main_window, self._organizer, self)
            tab_widget.insertTab(textures_index + 1, self._patcher_tab, "Patcher")
