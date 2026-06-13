from PyQt6.QtGui import QIcon

import mobase

from ..builder.window import KotorBuilderWindow



class KotorBuilderToolPlugin(mobase.IPluginTool, mobase.IPlugin):
    def __init__(self):
        mobase.IPluginTool.__init__(self)
        mobase.IPlugin.__init__(self)
        self._plugin_name = "KOTOR Builder Tool"
        self._display_name = "KOTOR Builder"
        self._version = mobase.VersionInfo(1, 0, 0)
        self._window: KotorBuilderWindow | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self):
        return self._plugin_name

    def displayName(self):
        return self._display_name

    def author(self):
        return "J"

    def description(self):
        return "Open the standalone KOTOR KSON builder window."

    def tooltip(self):
        return self.description()

    def version(self):
        return self._version

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def icon(self) -> QIcon:
        return QIcon()

    def enabledByDefault(self):
        try:
            return self._organizer.managedGame().gameShortName().lower() in {"kotor", "kotor2"}
        except Exception:
            return False

    def display(self):
        managed_game = self._organizer.managedGame()
        if managed_game is None or managed_game.gameShortName().lower() not in {"kotor", "kotor2"}:
            return
        if self._window is None:
            self._window = KotorBuilderWindow(None, self._organizer, managed_game)
        self._window.setWindowTitle(f"KOTOR Builder - {managed_game.gameName()}")
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()
