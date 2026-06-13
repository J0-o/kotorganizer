import configparser
import logging
import re
from pathlib import Path

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtGui import QAction, QColor, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QPushButton,
    QStyle,
    QToolButton,
    QTreeWidget,
    QWidget,
)


logger = logging.getLogger("mobase")



def blend_colors(base: QColor, overlay: QColor, alpha: float) -> QColor:
    alpha = max(0.0, min(1.0, alpha))
    return QColor(
        int(base.red() * (1.0 - alpha) + overlay.red() * alpha),
        int(base.green() * (1.0 - alpha) + overlay.green() * alpha),
        int(base.blue() * (1.0 - alpha) + overlay.blue() * alpha),
    )



def decode_qvariant_color(value: str) -> QColor | None:
    match = re.fullmatch(r"@Variant\((.*)\)", value.strip())
    if not match:
        return None

    raw = match.group(1)
    data = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            if raw[i + 1] == "0":
                data.append(0)
                i += 2
                continue
            if raw[i + 1] == "x" and i + 3 < len(raw):
                try:
                    data.append(int(raw[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
        data.append(ord(raw[i]) & 0xFF)
        i += 1

    if len(data) < 8:
        return None

    rgb16 = [int.from_bytes(data[-8 + j * 2 : -6 + j * 2], "big") for j in range(3)]
    return QColor(*(channel // 257 for channel in rgb16))



def mo2_setting_color(setting_name: str, fallback: QColor | None = None) -> QColor:
    ini_path = Path(__file__).resolve().parents[4] / "ModOrganizer.ini"
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(ini_path, encoding="utf-8")
        value = parser.get("Settings", setting_name, fallback="")
    except Exception:
        value = ""

    color = decode_qvariant_color(value)
    if color and color.isValid():
        return color
    return fallback if fallback is not None else QColor(255, 0, 0)



def mo2_conflict_green() -> QColor:
    return mo2_setting_color("overwrittenLooseFilesColor", QColor(0, 160, 0))



def mo2_conflict_red() -> QColor:
    return mo2_setting_color("overwritingLooseFilesColor", QColor(220, 0, 0))



def mo2_archive_conflict_purple() -> QColor:
    return mo2_setting_color("overwritingArchiveFilesColor", QColor(96, 16, 128))



def mo2_mod_contains_file_color() -> QColor:
    return mo2_setting_color("modlistContainsFileColor", QColor(0, 0, 255, 64))



def configure_refresh_button(button: QPushButton) -> None:
    button.setText("Refresh")
    button.setIcon(button.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
    button.setIconSize(QSize(16, 16))



def configure_download_button(button: QPushButton) -> None:
    button.setIcon(button.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
    button.setIconSize(QSize(16, 16))



def tree_base_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.Base)



def tree_alt_base_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.AlternateBase)



def tree_highlight_color(tree: QTreeWidget) -> QColor:
    return tree.palette().color(QPalette.ColorRole.Highlight)



def tree_hover_color(tree: QTreeWidget, alpha: float = 0.34) -> QColor:
    return blend_colors(tree_alt_base_color(tree), tree_highlight_color(tree), alpha)



def tree_conflict_row_color(tree: QTreeWidget, conflict_color: QColor, alpha: float = 0.24) -> QColor:
    return blend_colors(tree_base_color(tree), conflict_color, alpha)



def tree_selected_marker_color(tree: QTreeWidget) -> QColor:
    return tree_highlight_color(tree)



def tree_major_conflict_color(tree: QTreeWidget, conflict_color: QColor | None = None, alpha: float = 0.34) -> QColor:
    return blend_colors(tree_alt_base_color(tree), conflict_color or mo2_conflict_red(), alpha)



def tree_minor_conflict_color(tree: QTreeWidget, conflict_color: QColor | None = None, alpha: float = 0.20) -> QColor:
    return blend_colors(tree_base_color(tree), conflict_color or mo2_conflict_red(), alpha)



def tree_row_padding_stylesheet() -> str:
    return "QTreeWidget::item { padding: 0.3em 0; }"



def configure_tree_widget(
    tree: QTreeWidget,
    *,
    selection_mode: QAbstractItemView.SelectionMode,
    uniform_row_heights: bool = False,
    sorting_enabled: bool = True,
    alternating_rows: bool = True,
    root_decorated: bool = False,
    mouse_tracking: bool = False,
) -> None:
    tree.setRootIsDecorated(root_decorated)
    tree.setUniformRowHeights(uniform_row_heights)
    tree.setAlternatingRowColors(alternating_rows)
    tree.setSelectionMode(selection_mode)
    tree.setSortingEnabled(sorting_enabled)
    tree.setMouseTracking(mouse_tracking)



def set_header_resize_mode(header: QHeaderView, mode: QHeaderView.ResizeMode, count: int) -> None:
    for col in range(count):
        header.setSectionResizeMode(col, mode)



def refresh_mo2(organizer, widget: QWidget | None = None) -> None:
    QTimer.singleShot(250, lambda: _refresh_mo2_now(organizer, widget))



def _refresh_mo2_now(organizer, widget: QWidget | None = None) -> bool:
    refresh = getattr(organizer, "refresh", None)
    if callable(refresh):
        try:
            refresh()
            logger.info("[KOTOR] Triggered MO2 refresh through organizer.refresh().")
            return True
        except Exception as exc:
            logger.warning(f"[KOTOR] organizer.refresh() failed: {exc}")

    if widget is None:
        logger.warning("[KOTOR] Could not trigger MO2 refresh: no widget fallback.")
        return False

    window = widget.window()
    for action in window.findChildren(QAction):
        text = action.text().replace("&", "").strip().lower()
        object_name = action.objectName().lower()
        tool_tip = action.toolTip().replace("&", "").strip().lower()
        if text == "refresh" or tool_tip == "refresh" or "refresh" in object_name:
            action.trigger()
            logger.info(f"[KOTOR] Triggered MO2 refresh through QAction {action.objectName()}.")
            return True

    for button in window.findChildren((QPushButton, QToolButton)):
        text = button.text().replace("&", "").strip().lower()
        object_name = button.objectName().lower()
        tool_tip = button.toolTip().replace("&", "").strip().lower()
        status_tip = button.statusTip().replace("&", "").strip().lower()
        if text == "refresh" or tool_tip == "refresh" or status_tip == "refresh" or "refresh" in object_name:
            button.click()
            logger.info(f"[KOTOR] Triggered MO2 refresh through button {button.objectName()}.")
            return True

    logger.warning("[KOTOR] Could not find an MO2 refresh action or button.")
    return False
