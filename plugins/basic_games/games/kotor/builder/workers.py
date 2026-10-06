from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QTreeWidgetItem

from .exporter import BuildInputMod, build_instruction_set



class _NumericTreeWidgetItem(QTreeWidgetItem):
    _ROW_SIZE = QSize(0, 26)


    def __init__(self, *args):
        super().__init__(*args)
        for column in range(self.columnCount()):
            self.setSizeHint(column, self._ROW_SIZE)


    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        left = self.data(column, Qt.ItemDataRole.UserRole + 10)
        right = other.data(column, Qt.ItemDataRole.UserRole + 10)
        if isinstance(left, int) and isinstance(right, int):
            return left < right
        return super().__lt__(other)



class _BuildWorker(QObject):
    progress = pyqtSignal(int, int, str, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


    def __init__(self, game_name: str, output_path: Path, mods: list[BuildInputMod], tslpatch_order_path: Path):
        super().__init__()
        self._game_name = game_name
        self._output_path = output_path
        self._mods = mods
        self._tslpatch_order_path = tslpatch_order_path


    def run(self):
        try:
            self.finished.emit(
                build_instruction_set(
                    self._game_name,
                    self._output_path,
                    self._mods,
                    tslpatch_order_path=self._tslpatch_order_path,
                    progress=lambda current, total, name, status: self.progress.emit(current, total, name, status),
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))
