from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionSlider,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from patcher_constants import (
    COL_CONFLICTS,
    COL_PRIORITY,
    MO2_MOD_INDEX_ROLE,
    ROLE_CONFLICT_SORT,
)
from ui_theme import mo2_mod_contains_file_color



class _PatcherConflictOverview(QWidget):

    def __init__(self, tree: QTreeWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._tree = tree
        self._row_colors: list[QColor | None] = []
        self.setMinimumWidth(8)
        self.setMaximumWidth(8)


    def set_row_colors(self, row_colors: list[QColor | None]):
        self._row_colors = row_colors
        self.update()


    def _track_rect(self) -> tuple[int, int]:
        scroll_bar = self._tree.verticalScrollBar()
        if scroll_bar is None:
            return 0, self.height()

        option = QStyleOptionSlider()
        scroll_bar.initStyleOption(option)
        style = scroll_bar.style()
        sub_line_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarSubLine,
            scroll_bar,
        )
        add_line_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarAddLine,
            scroll_bar,
        )

        top = max(0, sub_line_rect.height())
        bottom = self.height() - max(0, add_line_rect.height())
        if self._tree.horizontalScrollBar().isVisible():
            bottom -= self._tree.horizontalScrollBar().height()
        if bottom <= top:
            return 0, self.height()
        return top, bottom


    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        row_count = len(self._row_colors)
        if row_count == 0 or self.height() <= 0:
            return

        track_top, track_bottom = self._track_rect()
        track_height = track_bottom - track_top
        if track_height <= 0:
            return

        width = self.width()
        for index, color in enumerate(self._row_colors):
            if color is None:
                continue
            top = track_top + int(index * track_height / row_count)
            bottom = track_top + int((index + 1) * track_height / row_count)
            height = max(2, bottom - top)
            painter.fillRect(0, top, width, height, color)



class _PatcherItem(QTreeWidgetItem):

    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        if tree and tree.sortColumn() == COL_PRIORITY:
            try:
                return int(self.text(COL_PRIORITY)) < int(other.text(COL_PRIORITY))
            except Exception:
                pass
        if tree and tree.sortColumn() == COL_CONFLICTS:
            try:
                return int(self.data(COL_CONFLICTS, ROLE_CONFLICT_SORT) or 0) < int(
                    other.data(COL_CONFLICTS, ROLE_CONFLICT_SORT) or 0
                )
            except Exception:
                pass
        return super().__lt__(other)



class _ModListContainsFileDelegate(QStyledItemDelegate):
    def __init__(self, previous_delegate, parent: QWidget | None = None):
        super().__init__(parent)
        self._previous_delegate = previous_delegate
        self._highlight_mod_name = ""
        self._highlight_mod_index: int | None = None


    def set_highlighted_mod(self, mod_name: str, mod_index: int | None):
        self._highlight_mod_name = mod_name.strip()
        self._highlight_mod_index = mod_index
        parent = self.parent()
        if isinstance(parent, QWidget):
            if hasattr(parent, "viewport"):
                parent.viewport().update()
            else:
                parent.update()


    def _matches_highlight(self, index) -> bool:
        if not self._highlight_mod_name:
            return False
        if self._highlight_mod_index is not None:
            try:
                if int(index.data(MO2_MOD_INDEX_ROLE)) == self._highlight_mod_index:
                    return True
            except (TypeError, ValueError):
                pass
        name_index = index.sibling(index.row(), 0)
        return str(name_index.data(Qt.ItemDataRole.DisplayRole) or "") == self._highlight_mod_name


    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        if self._previous_delegate is not None:
            self._previous_delegate.paint(painter, option, index)
        else:
            super().paint(painter, option, index)
        if self._matches_highlight(index):
            painter.fillRect(option.rect, mo2_mod_contains_file_color())



class _ModListScrollbarMarkerOverlay(QWidget):
    def __init__(self, mod_list_widget, parent: QWidget):
        super().__init__(parent)
        self._mod_list_widget = mod_list_widget
        self._highlight_mod_name = ""
        self._highlight_mod_index: int | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        parent.installEventFilter(self)
        self._sync_geometry()
        self.show()
        self.raise_()


    def _sync_geometry(self):
        parent = self.parent()
        if isinstance(parent, QWidget):
            self.setGeometry(parent.rect())


    def eventFilter(self, obj, event):
        if obj is self.parent() and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.Paint,
            QEvent.Type.Move,
        }:
            self._sync_geometry()
            self.raise_()
        return super().eventFilter(obj, event)


    def set_highlighted_mod(self, mod_name: str, mod_index: int | None):
        self._highlight_mod_name = mod_name.strip()
        self._highlight_mod_index = mod_index
        self.update()


    def _matches_highlight(self, index) -> bool:
        if not self._highlight_mod_name:
            return False
        if self._highlight_mod_index is not None:
            try:
                if int(index.data(MO2_MOD_INDEX_ROLE)) == self._highlight_mod_index:
                    return True
            except (TypeError, ValueError):
                pass
        return str(index.data(Qt.ItemDataRole.DisplayRole) or "") == self._highlight_mod_name


    def _visible_indices(self, model, parent=None) -> list:
        rows = model.rowCount(parent) if parent is not None else model.rowCount()
        indices = []
        for row in range(rows):
            index = model.index(row, 0, parent) if parent is not None else model.index(row, 0)
            indices.append(index)
            if self._mod_list_widget.isExpanded(index):
                indices.extend(self._visible_indices(model, index))
        return indices


    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._highlight_mod_name:
            return
        model = self._mod_list_widget.model()
        if model is None:
            return
        indices = self._visible_indices(model)
        if not indices:
            return

        scroll_bar = self.parent()
        option = QStyleOptionSlider()
        scroll_bar.initStyleOption(option)
        style = scroll_bar.style()
        handle_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarSlider,
            scroll_bar,
        )
        inner_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            option,
            QStyle.SubControl.SC_ScrollBarGroove,
            scroll_bar,
        )
        if inner_rect.height() <= 3:
            return

        painter = QPainter(self)
        color = mo2_mod_contains_file_color()
        painter.setPen(color)
        painter.setBrush(color)
        painter.translate(inner_rect.topLeft() + QPoint(0, 3))
        scale = (inner_rect.height() - 3) / len(indices)
        marker_width = max(1, handle_rect.width() - 5)
        for row, index in enumerate(indices):
            if self._matches_highlight(index):
                painter.drawRect(2, int(row * scale) - 2, marker_width, 3)



class _PatcherCheckboxDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        item_option = QStyleOptionViewItem(option)
        self.initStyleOption(item_option, index)
        super().paint(painter, item_option, index)
        tree = option.widget
        if isinstance(tree, QTreeWidget):
            style = tree.style()
            indicator_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                item_option,
                tree,
            )
            if indicator_rect.isValid() and not indicator_rect.isEmpty():
                indicator_option = QStyleOptionViewItem(item_option)
                indicator_option.rect = indicator_rect
                indicator_option.state &= ~(
                    QStyle.StateFlag.State_On
                    | QStyle.StateFlag.State_Off
                    | QStyle.StateFlag.State_NoChange
                )
                if item_option.checkState == Qt.CheckState.Checked:
                    indicator_option.state |= QStyle.StateFlag.State_On
                elif item_option.checkState == Qt.CheckState.PartiallyChecked:
                    indicator_option.state |= QStyle.StateFlag.State_NoChange
                else:
                    indicator_option.state |= QStyle.StateFlag.State_Off
                style.drawPrimitive(
                    QStyle.PrimitiveElement.PE_IndicatorItemViewItemCheck,
                    indicator_option,
                    painter,
                    tree,
                )
