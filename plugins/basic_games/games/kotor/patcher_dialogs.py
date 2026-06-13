from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from patcher_entries import PatchEntry as _PatcherEntry
from ui_theme import configure_tree_widget



class _PatcherDetailsDialog(QDialog):

    def __init__(
        self,
        parent: QWidget | None,
        owner: "Kotor2PatcherTab",
        entry: _PatcherEntry,
        conflict_rows: list[tuple[str, str]],
        info_text: str,
        info_path: Path | None,
        ini_text: str,
        log_text: str,
    ):
        super().__init__(parent)
        self._owner = owner
        self._entry = entry
        self.setWindowTitle(f"{entry.mod_name} / {entry.patch_name}")
        self.resize(880, 620)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs)

        info_tab = QWidget(self)
        info_layout = QVBoxLayout(info_tab)
        info_meta = QPlainTextEdit(self)
        info_meta.setReadOnly(True)
        info_meta.setPlainText(
            "\n".join(
                [
                    f"Mod: {entry.mod_name}",
                    f"Patch: {entry.patch_name}",
                    f"Description: {entry.description or '(none)'}",
                    f"Priority: {entry.priority}",
                    f"Enabled: {entry.enabled}",
                    f"INI: {entry.ini_short_path}",
                ]
            )
        )
        info_rtf = QPlainTextEdit(self)
        info_rtf.setReadOnly(True)
        info_rtf.setPlainText(info_text or "No info file found.")
        info_layout.addWidget(info_meta, 1)
        if info_path and info_path.exists():
            open_btn = QPushButton("Open info file", self)
            open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(info_path))))
            info_layout.addWidget(open_btn, 0)
        info_layout.addWidget(info_rtf, 4)
        tabs.addTab(info_tab, "Info")

        ini_view = QPlainTextEdit(self)
        ini_view.setReadOnly(True)
        ini_view.setPlainText(ini_text or "No INI text found.")
        tabs.addTab(ini_view, "Ini")

        operations = QPlainTextEdit(self)
        operations.setReadOnly(True)
        operations.setPlainText(
            "\n\n".join(
                [
                    "\n".join(
                        [
                            f"Type: {operation.resource_type}",
                            f"Action: {operation.action}",
                            f"Target: {operation.target}",
                            f"Location: {operation.location}",
                            f"Scope: {', '.join(operation.scope) if operation.scope else '(none)'}",
                            f"Section: {operation.source_section}",
                        ]
                    )
                    for operation in entry.operations
                ]
            )
            or "No parsed operations."
        )
        tabs.addTab(operations, "Operations")

        conflicts_tab = QWidget(self)
        conflicts_layout = QVBoxLayout(conflicts_tab)
        conflicts_tree = QTreeWidget(self)
        conflicts_tree.setColumnCount(2)
        conflicts_tree.setHeaderLabels(["Conflicting Mod", "Patch"])
        configure_tree_widget(
            conflicts_tree,
            selection_mode=QAbstractItemView.SelectionMode.SingleSelection,
            uniform_row_heights=True,
        )
        conflicts_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        conflicts_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        conflicts_view = QPlainTextEdit(self)
        conflicts_view.setReadOnly(True)
        conflicts_view.setPlaceholderText("Select a conflicting patch to view shared operations.")
        for label, details in conflict_rows:
            if " / " in label:
                mod_name, patch_name = label.split(" / ", 1)
            else:
                mod_name, patch_name = label, ""
            row = QTreeWidgetItem([mod_name, patch_name])
            row.setData(0, Qt.ItemDataRole.UserRole, details)
            conflicts_tree.addTopLevelItem(row)
        if conflict_rows:
            conflicts_tree.setCurrentItem(conflicts_tree.topLevelItem(0))
            conflicts_view.setPlainText(str(conflicts_tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) or ""))
        else:
            conflicts_view.setPlainText("No enabled patch conflicts for this patch.")
        conflicts_tree.itemClicked.connect(
            lambda item, _column: conflicts_view.setPlainText(str(item.data(0, Qt.ItemDataRole.UserRole) or ""))
        )
        conflicts_layout.addWidget(conflicts_tree, 2)
        conflicts_layout.addWidget(conflicts_view, 3)
        tabs.addTab(conflicts_tab, "Conflicts")

        log_view = QPlainTextEdit(self)
        log_view.setReadOnly(True)
        log_view.setPlainText(log_text or "No log file found for this patch.")
        tabs.addTab(log_view, "Log")

        test_tab = QWidget(self)
        test_layout = QVBoxLayout(test_tab)
        test_buttons = QHBoxLayout()
        prepare_test_btn = QPushButton("Prepare Test", self)
        prepare_test_btn.clicked.connect(self._prepare_test_install)
        run_test_btn = QPushButton("Run Test", self)
        run_test_btn.clicked.connect(self._run_test_install)
        open_test_btn = QPushButton("Open Test Folder", self)
        open_test_btn.clicked.connect(self._open_test_folder)
        test_buttons.addWidget(prepare_test_btn)
        test_buttons.addWidget(run_test_btn)
        test_buttons.addWidget(open_test_btn)
        test_buttons.addStretch()
        test_layout.addLayout(test_buttons)

        test_log = QPlainTextEdit(self)
        test_log.setReadOnly(True)
        test_log.setPlaceholderText("Single-patch prepare/run logs will appear here.")
        self._test_log = test_log
        test_layout.addWidget(test_log, 1)
        tabs.addTab(test_tab, "Test")


    def _prepare_test_install(self):
        self._test_log.setPlainText(self._owner._prepare_test_entry(self._entry))


    def _run_test_install(self):
        self._test_log.setPlainText(self._owner._run_test_entry(self._entry))


    def _open_test_folder(self):
        test_dir = self._owner._test_entry_target_dir(self._entry)
        if test_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(test_dir)))



class _PatcherRunnerDialog(QDialog):

    def __init__(self, parent: QWidget | None, owner: "Kotor2PatcherTab"):
        super().__init__(parent)
        self._owner = owner
        self.setWindowTitle("Patcher")
        self.resize(860, 620)
        self._busy_frames = ("|", "/", "-", "\\")
        self._busy_frame = 0
        self._busy_text = "Ready"

        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        self._run_patcher_btn = QPushButton("Start", self)
        self._run_patcher_btn.clicked.connect(self._owner._run_patcher)
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.setAutoDefault(False)
        self._stop_btn.setDefault(False)
        self._stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._owner._stop_patcher)
        buttons.addWidget(self._run_patcher_btn)
        buttons.addWidget(self._stop_btn)
        self._status_label = QLabel("Ready", self)
        buttons.addWidget(self._status_label)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._log_box = QPlainTextEdit(self)
        self._log_box.setReadOnly(True)
        self._log_box.setPlaceholderText("Patcher prepare/run logs will appear here.")
        layout.addWidget(self._log_box, 1)

        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(180)
        self._busy_timer.timeout.connect(self._advance_busy_status)


    def set_log_text(self, text: str):
        self._log_box.setPlainText(text)
        self._log_box.verticalScrollBar().setValue(self._log_box.verticalScrollBar().maximum())


    def _advance_busy_status(self):
        self._busy_frame = (self._busy_frame + 1) % len(self._busy_frames)
        self._status_label.setText(f"{self._busy_frames[self._busy_frame]} {self._busy_text}")


    def set_busy_status(self, status: str):
        self._busy_text = status
        if self._busy_timer.isActive():
            self._status_label.setText(f"{self._busy_frames[self._busy_frame]} {self._busy_text}")


    def set_running(self, running: bool, status: str = "Running"):
        self._run_patcher_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        if running:
            self._busy_text = status
            self._busy_frame = 0
            self._status_label.setText(f"{self._busy_frames[self._busy_frame]} {self._busy_text}")
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self._status_label.setText("Ready")
