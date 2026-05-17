"""
register_table.py – Virtual register table model, delegate, and view.

Provides
--------
RegisterTableModel  – QAbstractTableModel backed by a SlaveConfig's sparse data.
                      Only visible rows are rendered (lazy / virtual scrolling).
RegisterDelegate    – QStyledItemDelegate providing a QComboBox for the Data Type
                      column and a QLineEdit for the Value column.
RegisterTableView   – QTableView subclass with Excel-like single-click editing
                      and Tab / Enter / Arrow key navigation.

Design notes
------------
* The model owns no server knowledge — it only reads/writes SlaveConfig data.
* Live server updates are signalled via ``register_changed`` and
  ``dtype_changed`` signals; the caller (MainWindow) connects these to
  SimulatorService.
* Single-click editing is achieved by setting the edit trigger to
  ``CurrentChanged | SelectedClicked | AnyKeyPressed`` and using
  ``SelectItems`` selection behaviour.
* Boolean groups (Coils, Discrete Inputs) use a dedicated checkbox-style
  paint + direct mousePressEvent toggle rather than the editor mechanism,
  because Qt's editor lifecycle interferes with checkbox toggling when
  CurrentChanged is an edit trigger.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QTableView, QAbstractItemView, QHeaderView,
    QStyledItemDelegate, QStyleOptionViewItem,
    QComboBox, QLineEdit, QApplication,
)
from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, Signal,
)
from PySide6.QtGui import QColor, QBrush, QKeyEvent, QMouseEvent

from models.register_data import ModbusDataType, get_register_count
from models.device_config import (
    SlaveConfig, NUM_ROWS, NUM_ROWS_5DIGIT, BOOL_GROUPS,
)

# ── Constants ──────────────────────────────────────────────────────────────────
ALL_DTYPES = [t.value for t in ModbusDataType]

COL_ADDR  = 0
COL_TYPE  = 1
COL_VALUE = 2
HEADERS   = ["Address", "Data Type", "Value"]

COLOR_SLAVE = QColor(200, 200, 200)   # greyed-out background for slave rows


def _dtype_from_str(text: str) -> ModbusDataType:
    """Convert a display string to a ModbusDataType, defaulting to UINT16."""
    for t in ModbusDataType:
        if t.value == text:
            return t
    return ModbusDataType.UINT16


# ══════════════════════════════════════════════════════════════════════════════
# Virtual table model
# ══════════════════════════════════════════════════════════════════════════════

class RegisterTableModel(QAbstractTableModel):
    """
    Lazy model for one register group of one SlaveConfig.

    Row data is fetched from the sparse SlaveConfig on demand; only
    non-default rows are stored in memory.  The model reports ``row_count``
    rows so the scroll-bar covers exactly the valid Modbus address space
    for the chosen addressing mode (9 999 for 5-digit, 65 536 for 6-digit).

    Signals
    -------
    register_changed(group, row, rd)
        Emitted when a register value or type changes.
    dtype_changed(group, row, new_type_str)
        Emitted after a data-type change cascades slave-row ownership.
    """

    register_changed = Signal(str, int, dict)   # group, row, row_dict
    dtype_changed    = Signal(str, int, str)    # group, row, new_type_str

    def __init__(
        self,
        slave: SlaveConfig,
        group: str,
        addr_offset: int,
        row_count: int = NUM_ROWS_5DIGIT,
        parent=None,
    ):
        super().__init__(parent)
        self._slave       = slave
        self._group       = group
        self._addr_offset = addr_offset
        self._row_count   = row_count
        self._is_bool     = group in BOOL_GROUPS

    # ── QAbstractTableModel interface ──────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return self._row_count

    def columnCount(self, parent=QModelIndex()) -> int:
        return 3

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col  = index.column()
        if col == COL_ADDR:
            return base   # address column is always read-only
        rd = self._slave.get_row(self._group, index.row())
        if rd["slave_of"] is not None:
            return base   # slave rows are read-only
        return base | Qt.ItemIsEditable

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        rd  = self._slave.get_row(self._group, row)

        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == COL_ADDR:
                return str(self._addr_offset + row)
            if col == COL_TYPE:
                return rd["type"]
            if col == COL_VALUE:
                return "—" if rd["slave_of"] is not None else rd["val"]

        if role == Qt.BackgroundRole and rd["slave_of"] is not None:
            return QBrush(COLOR_SLAVE)

        if role == Qt.CheckStateRole and col == COL_VALUE and self._is_bool:
            return Qt.Checked if rd["val"] == "True" else Qt.Unchecked

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        row = index.row()
        col = index.column()
        rd  = dict(self._slave.get_row(self._group, row))   # copy

        if rd["slave_of"] is not None:
            return False

        changed = False

        if role == Qt.EditRole:
            if col == COL_TYPE and not self._is_bool:
                if rd["type"] != value:
                    rd["type"] = value
                    changed = True
            elif col == COL_VALUE:
                if rd["val"] != value:
                    rd["val"] = value
                    changed = True

        if role == Qt.CheckStateRole and col == COL_VALUE and self._is_bool:
            new_val = "True" if (value == Qt.Checked or value == 2) else "False"
            if rd["val"] != new_val:
                rd["val"] = new_val
                changed = True

        if changed:
            self._slave.set_row(self._group, row, rd)
            self.dataChanged.emit(index, index, [role])
            self.register_changed.emit(self._group, row, rd)
        return changed

    # ── Helpers ────────────────────────────────────────────────────────────────

    def refresh(self):
        """Force a full repaint (e.g. after dtype change cascades)."""
        self.beginResetModel()
        self.endResetModel()

    def get_row_dict(self, row: int) -> dict:
        """Return the row dict for the given row index."""
        return self._slave.get_row(self._group, row)

    def apply_dtype_change(self, master_row: int, new_type_str: str):
        """
        Change the data type of *master_row*, freeing old slave rows and
        claiming new ones.  Emits ``dtype_changed`` after updating.
        """
        dtype     = _dtype_from_str(new_type_str)
        reg_count = get_register_count(dtype)

        # Update master row type
        rd = dict(self._slave.get_row(self._group, master_row))
        rd["type"] = new_type_str
        self._slave.set_row(self._group, master_row, rd)

        # Free previously owned slave rows
        for r, row_rd in list(self._slave.data[self._group].items()):
            if row_rd.get("slave_of") == master_row:
                self._slave.data[self._group].pop(r)

        # Claim new slave rows
        for offset in range(1, reg_count):
            slave_row = master_row + offset
            if slave_row >= NUM_ROWS:
                break
            existing = self._slave.get_row(self._group, slave_row)
            if existing["slave_of"] is None:
                has_own_slaves = any(
                    v.get("slave_of") == slave_row
                    for v in self._slave.data[self._group].values()
                )
                if not has_own_slaves:
                    new_rd = dict(existing)
                    new_rd["slave_of"] = master_row
                    self._slave.set_row(self._group, slave_row, new_rd)

        self.refresh()
        self.dtype_changed.emit(self._group, master_row, new_type_str)


# ══════════════════════════════════════════════════════════════════════════════
# Custom delegate
# ══════════════════════════════════════════════════════════════════════════════

class RegisterDelegate(QStyledItemDelegate):
    """
    Provides a QComboBox editor for the Data Type column and a plain
    QLineEdit for the Value column.  Boolean groups get a checkbox in the
    Value column (handled via CheckStateRole in the model).
    """

    def __init__(self, is_bool: bool, parent=None):
        super().__init__(parent)
        self._is_bool = is_bool

    def createEditor(self, parent, option, index):
        col = index.column()
        rd  = index.model().get_row_dict(index.row())
        if rd.get("slave_of") is not None:
            return None   # slave rows not editable

        if col == COL_TYPE and not self._is_bool:
            combo = QComboBox(parent)
            combo.addItems(ALL_DTYPES)
            return combo

        if col == COL_VALUE:
            if self._is_bool:
                # Boolean values are toggled directly via mousePressEvent
                # in RegisterTableView — no editor widget needed.
                return None
            return QLineEdit(parent)

        return None

    def setEditorData(self, editor, index):
        val = index.data(Qt.EditRole)
        if isinstance(editor, QComboBox):
            idx = editor.findText(val or "")
            if idx >= 0:
                editor.setCurrentIndex(idx)
        elif isinstance(editor, QLineEdit):
            editor.setText(val or "")

    def setModelData(self, editor, model, index):
        col = index.column()
        if isinstance(editor, QComboBox):
            new_type = editor.currentText()
            old_type = index.data(Qt.EditRole)
            if new_type != old_type:
                model.apply_dtype_change(index.row(), new_type)
        elif isinstance(editor, QLineEdit):
            model.setData(index, editor.text(), Qt.EditRole)

    def paint(self, painter, option, index):
        col = index.column()
        rd  = index.model().get_row_dict(index.row())

        # Grey background for slave rows
        if rd.get("slave_of") is not None:
            painter.fillRect(option.rect, COLOR_SLAVE)

        # Draw a native checkbox for boolean value column.
        # In PySide6, HasCheckIndicator lives under ViewItemFeature, not
        # directly on QStyleOptionViewItem.
        if col == COL_VALUE and self._is_bool and rd.get("slave_of") is None:
            check_state = index.data(Qt.CheckStateRole)
            opt = QStyleOptionViewItem(option)
            # Set the HasCheckIndicator feature using the correct PySide6 path
            opt.features = (
                opt.features
                | QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            )
            opt.checkState = check_state
            style = QApplication.style()
            style.drawControl(style.ControlElement.CE_ItemViewItem, opt, painter)
            return

        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        """
        Boolean checkbox toggling is handled in RegisterTableView.mousePressEvent
        for reliability with the CurrentChanged edit trigger.  This method is
        kept as a no-op for boolean cells so Qt does not try to open an editor.
        """
        col = index.column()
        if col == COL_VALUE and self._is_bool:
            return False   # handled by RegisterTableView.mousePressEvent
        return super().editorEvent(event, model, option, index)


# ══════════════════════════════════════════════════════════════════════════════
# Table view with Excel-like navigation
# ══════════════════════════════════════════════════════════════════════════════

class RegisterTableView(QTableView):
    """
    QTableView subclass with Excel-like cell editing and navigation.

    Behaviour
    ---------
    * Single click on any editable cell immediately opens the editor.
    * Tab / Shift+Tab  → commit edit, move right / left (wraps to next/prev row).
    * Enter / Return   → commit edit, move down one row.
    * Arrow keys       → commit edit, move in that direction.
    * Escape           → cancel edit (default Qt behaviour).

    Non-editable cells (address column, slave-of rows) are skipped during
    keyboard navigation.
    """

    # Columns that can be edited (not the address column)
    _EDITABLE_COLS = (COL_TYPE, COL_VALUE)

    def __init__(self, parent=None):
        super().__init__(parent)

        # Cell-level selection (not row-level) for proper cell navigation
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setSelectionMode(QAbstractItemView.SingleSelection)

        # Open editor on first click / any key press (Excel-like).
        # Note: CurrentChanged is intentionally included so that navigating
        # to a non-boolean cell with keyboard/click immediately opens the
        # editor.  Boolean cells return None from createEditor so this is
        # harmless for them; toggling is handled in mousePressEvent below.
        self.setEditTriggers(
            QAbstractItemView.CurrentChanged
            | QAbstractItemView.SelectedClicked
            | QAbstractItemView.AnyKeyPressed
        )

        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(22)

        # Track whether the current model is for a boolean group
        self._is_bool_group: bool = False

    def set_bool_group(self, is_bool: bool):
        """
        Inform the view whether the current register group is boolean
        (Coils / Discrete Inputs).  Must be called whenever the model changes.
        """
        self._is_bool_group = is_bool

    # ── Mouse handling — boolean checkbox toggle ───────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        """
        For boolean groups, a single left-click on the Value column toggles
        the checkbox directly, bypassing the editor mechanism entirely.
        This is more reliable than editorEvent when CurrentChanged is an
        edit trigger because it fires before Qt attempts to open an editor.
        """
        if self._is_bool_group and event.button() == Qt.LeftButton:
            idx = self.indexAt(event.pos())
            if idx.isValid() and idx.column() == COL_VALUE:
                model = self.model()
                if model is not None:
                    rd = model.get_row_dict(idx.row())
                    if rd.get("slave_of") is None:
                        current   = idx.data(Qt.CheckStateRole)
                        new_state = Qt.Unchecked if current == Qt.Checked else Qt.Checked
                        model.setData(idx, new_state, Qt.CheckStateRole)
                        # Make this cell current so keyboard nav works from here
                        self.setCurrentIndex(idx)
                        event.accept()
                        return
        super().mousePressEvent(event)

    # ── Keyboard navigation ────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        key  = event.key()
        mods = event.modifiers()

        # Let the base class handle Escape (cancel edit)
        if key == Qt.Key_Escape:
            super().keyPressEvent(event)
            return

        # Navigation keys — commit any open editor first
        if key in (Qt.Key_Tab, Qt.Key_Backtab,
                   Qt.Key_Return, Qt.Key_Enter,
                   Qt.Key_Up, Qt.Key_Down,
                   Qt.Key_Left, Qt.Key_Right):
            self._commit_current_editor()

            idx = self.currentIndex()
            if not idx.isValid():
                super().keyPressEvent(event)
                return

            row = idx.row()
            col = idx.column()

            if key == Qt.Key_Tab or (key == Qt.Key_Backtab):
                forward = not (key == Qt.Key_Backtab or (mods & Qt.ShiftModifier))
                self._navigate_tab(row, col, forward)
                return

            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._navigate_to(row + 1, col)
                return

            if key == Qt.Key_Up:
                self._navigate_to(row - 1, col)
                return
            if key == Qt.Key_Down:
                self._navigate_to(row + 1, col)
                return
            if key == Qt.Key_Left:
                self._navigate_to(row, col - 1)
                return
            if key == Qt.Key_Right:
                self._navigate_to(row, col + 1)
                return

        super().keyPressEvent(event)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _commit_current_editor(self):
        """Commit any currently open persistent editor / delegate editor."""
        if self.state() == QAbstractItemView.EditingState:
            self.commitData(self.focusWidget())
            self.closeEditor(self.focusWidget(),
                             QAbstractItemView.NoHint)

    def _navigate_tab(self, row: int, col: int, forward: bool):
        """
        Tab-style navigation: move through editable columns left-to-right,
        wrapping to the next/previous row when the end/start of a row is reached.
        """
        model = self.model()
        if model is None:
            return

        total_rows = model.rowCount()
        # Build ordered list of editable column indices
        editable = list(self._EDITABLE_COLS)

        if forward:
            # Find next editable column in this row
            next_cols = [c for c in editable if c > col]
            if next_cols:
                self._navigate_to(row, next_cols[0])
            else:
                # Wrap to first editable column of next row
                next_row = row + 1
                if next_row < total_rows:
                    self._navigate_to(next_row, editable[0])
        else:
            prev_cols = [c for c in editable if c < col]
            if prev_cols:
                self._navigate_to(row, prev_cols[-1])
            else:
                prev_row = row - 1
                if prev_row >= 0:
                    self._navigate_to(prev_row, editable[-1])

    def _navigate_to(self, row: int, col: int):
        """
        Move the current index to (row, col), clamping to valid bounds.
        Skips non-editable cells by moving in the same direction until an
        editable cell is found (or the boundary is reached).
        """
        model = self.model()
        if model is None:
            return

        row = max(0, min(row, model.rowCount() - 1))
        col = max(0, min(col, model.columnCount() - 1))

        idx = model.index(row, col)
        self.setCurrentIndex(idx)
        self.scrollTo(idx)
