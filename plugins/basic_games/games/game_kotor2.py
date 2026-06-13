import logging
import os
import sys
from pathlib import Path

_plugin_file = Path(__file__).resolve()
_plugin_dir = _plugin_file.parent
_plugin_dir_str = str(_plugin_dir)
_shared_dir = _plugin_dir / "kotor"
_shared_dir_str = str(_shared_dir)
_plugin_dir_added = False
if _plugin_dir_str not in sys.path:
    sys.path.insert(0, _plugin_dir_str)
    _plugin_dir_added = True
_shared_dir_added = False
if _shared_dir_str not in sys.path:
    sys.path.insert(0, _shared_dir_str)
    _shared_dir_added = True

import mobase
from PyQt6.QtCore import QDir
from PyQt6.QtWidgets import QMainWindow

from basic_games.basic_game import BasicGame
from basic_games.basic_features import (
    BasicLocalSavegames,
    BasicGameSaveGameInfo,
)
from import_probe import KOTOR2_IMPORT_PROBE
from shared_game import KotorGameMixin, KotorModDataCheckerBase
from saves_tab import Kotor2SaveGame, parse_kotor2_save_metadata

logger = logging.getLogger("mobase")
if _plugin_dir_added:
    logger.info(f"[KOTOR2] inserted plugin dir into sys.path: {_plugin_dir_str}")
if _shared_dir_added:
    logger.info(f"[KOTOR2] inserted shared dir into sys.path: {_shared_dir_str}")
logger.info(f"[KOTOR2] plugin file path: {_plugin_file} | plugin dir: {_plugin_dir}")
for _idx, _entry in enumerate(sys.path):
    logger.info(f"[KOTOR2] sys.path[{_idx}]: {_entry}")
logger.info(f"[KOTOR2] import probe: {KOTOR2_IMPORT_PROBE}")

class Kotor2ModDataChecker(KotorModDataCheckerBase):
    pass


class StarWarsKotor2Game(KotorGameMixin, BasicGame, mobase.IPluginFileMapper):

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        self._texture_tab = None
        self._patcher_tab = None
        self._sync_tab = None
        self._info_tab = None
        self._platform_logged = False

    Name = "STAR WARS Knights of the Old Republic II The Sith Lords"
    Author = "J"
    Version = "2.2.6"

    GameName = Name
    GameShortName = "kotor2"
    GameNexusName = "kotor2"
    GameNexusId = 198
    GameSteamId = 208580
    GameGogId = 1421404581
    GameBinary = "swkotor2.exe"
    GameDataPath = "%GAME_PATH%"
    _logger = logger
    _log_prefix = "KOTOR2"
    _workshop_app_id = "208580"
    _workshop_game_name = "KOTOR II"
    _workshop_warning_text = (
        "Steam Workshop content detected for KOTOR II. Workshop mods are unsupported in Mod Orgainizer 2."
    )


    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._organizer = organizer

        self._register_feature(BasicLocalSavegames(self.savesDirectory()))
        self._register_feature(BasicGameSaveGameInfo(Kotor2SaveGame, parse_kotor2_save_metadata))
        self._register_feature(Kotor2ModDataChecker())
        organizer.onUserInterfaceInitialized(self._init_custom_tabs)
        organizer.onAboutToRun(self._before_run)

        try:
            mg = self._organizer.managedGame()
            if mg and (mg == self or mg.gameName() == self.gameName()) and self.gameDirectory().exists():
                self._log_platform_once(force=True)
        except Exception:
            logger.info("[KOTOR2] Platform logging failed")

        if self._organizer.managedGame() and self._organizer.managedGame().gameName() == self.gameName():
            for d in self.game_directories():
                os.makedirs(d.absolutePath(), exist_ok=True)

        return True


    def _init_custom_tabs(self, main_window: QMainWindow):
        self._init_custom_tabs_common(main_window)


    def iniFiles(self):
        return [self.gameDirectory().absoluteFilePath("swkotor2.ini")]


    def executables(self):
        self._log_platform_once()
        exe_path = self.gameDirectory().absoluteFilePath(self.binaryName())
        logger.info(f"[KOTOR2 Plugin] registering executables: {exe_path}")
        return [
            mobase.ExecutableInfo("KOTOR2", exe_path),
        ]


    def listSaves(self, folder: QDir) -> list[mobase.ISaveGame]:
        saves = []
        root = Path(folder.absolutePath())
        for sub in root.iterdir():
            if sub.is_dir() and any(f.suffix == ".sav" for f in sub.iterdir()):
                saves.append(Kotor2SaveGame(sub))
        return saves



def createPlugin() -> mobase.IPluginGame:
    return StarWarsKotor2Game()
