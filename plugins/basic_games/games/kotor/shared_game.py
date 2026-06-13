import winreg
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QTimer
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from info_tab import KotorInfoTab
from moddatachecker import KotorModDataCheckerBase
from patcher_tab import Kotor2PatcherTab
from sync_tab import Kotor2SyncTab
from texture_tab import Kotor2TextureTab



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


    def _before_run(self, _app) -> bool:
        self._log_platform_once()
        return self._confirm_texture_major_errors()


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


    def _confirm_texture_major_errors(self) -> bool:
        try:
            if self._texture_tab is not None:
                major_errors = self._texture_tab.major_error_count()
            else:
                major_errors = Kotor2TextureTab.count_major_errors_for_game(self)
        except Exception as exc:
            self._logger.info("[%s] Texture warning check failed: %s", self._log_prefix, exc)
            return True

        if major_errors <= 0:
            return True

        self._logger.warning("[%s] Major texture conflicts detected: %s", self._log_prefix, major_errors)
        try:
            result = QMessageBox.warning(
                None,
                self._workshop_game_name,
                (
                    f"{major_errors} major texture conflict(s) are active.\n\n"
                    "Major texture conflicts usually mean a visible TPC texture and TXI file share the same base name. "
                    "These conflicts can cause the game to crash. "
                    "Review the Textures tab before launching if you want to fix them.\n\n"
                    "Launch anyway?"
                ),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            return result != QMessageBox.StandardButton.Cancel
        except Exception as exc:
            self._logger.debug("[%s] Texture warning dialog failed: %s", self._log_prefix, exc)
            return True


    def _init_custom_tabs_common(self, main_window: QMainWindow):
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
            self._texture_tab = Kotor2TextureTab(main_window, self._organizer, self)
            tab_widget.insertTab(insert_index, self._texture_tab, "Textures")
            textures_index = insert_index

        patcher_index = None
        for i in range(tab_widget.count()):
            if tab_widget.tabText(i).lower() == "patcher":
                patcher_index = i
                break
        if patcher_index is None:
            self._patcher_tab = Kotor2PatcherTab(main_window, self._organizer, self)
            tab_widget.insertTab(textures_index + 1, self._patcher_tab, "Patcher")
            patcher_index = textures_index + 1

        sync_index = None
        for i in range(tab_widget.count()):
            if tab_widget.tabText(i).lower() == "sync":
                sync_index = i
                break
        if sync_index is None:
            self._sync_tab = Kotor2SyncTab(main_window, self._organizer, self)
            tab_widget.insertTab(patcher_index + 1, self._sync_tab, "Sync")
            sync_index = patcher_index + 1

        info_index = None
        for i in range(tab_widget.count()):
            if tab_widget.tabText(i).lower() == "info":
                info_index = i
                break
        if info_index is None:
            self._info_tab = KotorInfoTab(main_window, self._organizer, self)
            tab_widget.insertTab(sync_index + 1, self._info_tab, "Info")
