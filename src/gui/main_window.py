from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QFileDialog, QCheckBox, QAbstractItemView)
from PySide6.QtCore import Qt
import socket
import psutil
import csv
import logging
from models.register_data import ModbusDataType, DataConverter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Server Simulator")
        self.resize(800, 600)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # Server Settings
        settings_layout = QHBoxLayout()
        
        settings_layout.addWidget(QLabel("Slave Address:"))
        self.slave_address_input = QLineEdit("1")
        settings_layout.addWidget(self.slave_address_input)

        settings_layout.addWidget(QLabel("NIC:"))
        self.nic_selector = QComboBox()
        self.nic_map = self.get_available_nics()
        self.nic_selector.addItems(self.nic_map.keys())
        settings_layout.addWidget(self.nic_selector)

        self.start_button = QPushButton("Start Server")
        self.start_button.setCheckable(True)
        self.start_button.clicked.connect(self.toggle_server)
        settings_layout.addWidget(self.start_button)
        
        self.save_button = QPushButton("Save Config")
        self.save_button.clicked.connect(self.save_config)
        settings_layout.addWidget(self.save_button)
        
        self.load_button = QPushButton("Load Config")
        self.load_button.clicked.connect(self.load_config)
        settings_layout.addWidget(self.load_button)
        
        self.zero_based_checkbox = QCheckBox("Zero-Based Addressing")
        self.zero_based_checkbox.stateChanged.connect(self.on_zero_based_changed)
        settings_layout.addWidget(self.zero_based_checkbox)
        
        layout.addLayout(settings_layout)

        # Register Group Selector
        group_layout = QHBoxLayout()
        group_layout.addWidget(QLabel("Register Group:"))
        self.group_selector = QComboBox()
        self.group_selector.addItems(["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"])
        self.group_selector.currentTextChanged.connect(self.switch_register_group)
        group_layout.addWidget(self.group_selector)
        layout.addLayout(group_layout)

        # Store data in memory (0-indexed addresses)
        self.data = {
            "Coils": [{"addr": i, "type": "Boolean", "val": "False"} for i in range(100)],
            "Discrete Inputs": [{"addr": i, "type": "Boolean", "val": "False"} for i in range(100)],
            "Holding Registers": [{"addr": i, "type": "Unsigned Integer", "val": "0"} for i in range(100)],
            "Input Registers": [{"addr": i, "type": "Unsigned Integer", "val": "0"} for i in range(100)],
        }
        self.current_group = "Coils"

        # Registers Table
        self.table = QTableWidget(100, 4)
        self.table.setHorizontalHeaderLabels(["Address", "Data Type", "Value", "Raw"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.load_table_data()
        layout.addWidget(self.table)

    def _get_display_address(self, addr):
        is_zero_based = self.zero_based_checkbox.isChecked()
        mapping = {
            "Coils": 0 if is_zero_based else 1,
            "Discrete Inputs": 10000 if is_zero_based else 10001,
            "Holding Registers": 40000 if is_zero_based else 40001,
            "Input Registers": 30000 if is_zero_based else 30001
        }
        return mapping[self.current_group] + addr

    def load_table_data(self):
        self.table.blockSignals(True)
        for i in range(100):
            item = self.data[self.current_group][i]
            # Display full Modbus address based on group
            self.table.setItem(i, 0, QTableWidgetItem(str(self._get_display_address(item["addr"]))))
            
            # Clear row thoroughly
            old_widget1 = self.table.cellWidget(i, 1)
            if old_widget1: old_widget1.deleteLater()
            self.table.setCellWidget(i, 1, None)
            
            old_widget2 = self.table.cellWidget(i, 2)
            if old_widget2: old_widget2.deleteLater()
            self.table.setCellWidget(i, 2, None)
            
            # Remove items
            self.table.setItem(i, 1, None)
            self.table.setItem(i, 2, None)
            self.table.setItem(i, 3, None)
            
            # Handle Data Type column
            if self.current_group in ["Coils", "Discrete Inputs"]:
                combo = QComboBox()
                combo.addItems(["Boolean"])
                combo.setCurrentText("Boolean")
                combo.setEnabled(False)
                self.table.setCellWidget(i, 1, combo)
                
                # Handle Value column for Boolean
                val = item["val"] == "True"
                checkbox = QCheckBox()
                checkbox.setChecked(val)
                checkbox.stateChanged.connect(lambda state, row=i: self.update_bool_val(row, state))
                self.table.setCellWidget(i, 2, checkbox)
            else:
                combo = QComboBox()
                combo.addItems([t.value for t in ModbusDataType])
                combo.setCurrentText(item["type"])
                self.table.setCellWidget(i, 1, combo)
                
                # For integers, use QLineEdit for input
                val_edit = QLineEdit(item["val"])
                val_edit.textChanged.connect(lambda text, row=i: self.update_int_val(row, text))
                self.table.setCellWidget(i, 2, val_edit)
            
            self.table.setItem(i, 3, QTableWidgetItem("0"))
        self.table.blockSignals(False)

    def update_bool_val(self, row, state):
        val = state == Qt.Checked
        self.data[self.current_group][row]["val"] = str(val)
        if hasattr(self, 'server'):
            reg_type = self._map_group_to_type(self.current_group)
            self.server.update_register(reg_type, row, int(val))

    def update_int_val(self, row, text):
        self.data[self.current_group][row]["val"] = text
        if hasattr(self, 'server'):
            reg_type = self._map_group_to_type(self.current_group)
            try:
                self.server.update_register(reg_type, row, int(text))
            except ValueError:
                pass # Ignore invalid input

    def on_zero_based_changed(self, state):
        if hasattr(self, 'server'):
            self.server.set_zero_based(self.zero_based_checkbox.isChecked())
        self.load_table_data()

    def switch_register_group(self, group):
        # Save current
        for i in range(100):
            if self.current_group in ["Coils", "Discrete Inputs"]:
                checkbox = self.table.cellWidget(i, 2)
                self.data[self.current_group][i]["val"] = str(checkbox.isChecked()) if checkbox else "False"
            else:
                lineedit = self.table.cellWidget(i, 2)
                self.data[self.current_group][i]["val"] = lineedit.text() if lineedit else "0"
                self.data[self.current_group][i]["type"] = self.table.cellWidget(i, 1).currentText()
        
        self.current_group = group
        self.load_table_data()

    def toggle_server(self, checked):
        from modbus.server_wrapper import ModbusServer
        if checked:
            slave_id = int(self.slave_address_input.text())
            host = self.get_selected_nic_ip()
            is_zero_based = self.zero_based_checkbox.isChecked()
            
            self.server = ModbusServer(slave_id=slave_id, host=host, zero_based=is_zero_based)
            
            # Populate all groups
            for group, items in self.data.items():
                reg_type = self._map_group_to_type(group)
                initial_values = [0] * 100
                for row, item in enumerate(items):
                    val_str = item["val"]
                    if group in ["Coils", "Discrete Inputs"]:
                        initial_values[row] = 1 if val_str == "True" else 0
                    else:
                        try:
                            val = int(val_str)
                            initial_values[row] = val
                        except ValueError:
                            initial_values[row] = 0
                
                self.server.set_initial_values(reg_type, initial_values)
            
            self.server.start()
            self.start_button.setText("Stop Server")
        else:
            if hasattr(self, 'server'):
                self.server.stop()
                self.server.join(timeout=2.0)
            self.start_button.setText("Start Server")

    def _map_group_to_type(self, group):
        mapping = {
            "Coils": "coils",
            "Discrete Inputs": "discrete_inputs",
            "Holding Registers": "holding_registers",
            "Input Registers": "input_registers"
        }
        return mapping[group]

    def get_available_nics(self):
        nics = {"All (0.0.0.0)": "0.0.0.0"}
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    nics[f"{interface} ({addr.address})"] = addr.address
        return nics
    
    def get_selected_nic_ip(self):
        key = self.nic_selector.currentText()
        return self.nic_map.get(key, "0.0.0.0")

    def save_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Configuration", "", "CSV Files (*.csv)")
        if path:
            with open(path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Group", "Address", "Data Type", "Value"])
                for group, items in self.data.items():
                    for item in items:
                        writer.writerow([group, item["addr"], item["type"], item["val"]])

    def load_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Configuration", "", "CSV Files (*.csv)")
        if path:
            with open(path, 'r') as f:
                reader = csv.reader(f)
                next(reader) # Skip header
                for row in reader:
                    group, addr, dtype, val = row
                    # Find item
                    for item in self.data[group]:
                        if str(item["addr"]) == addr:
                            item["type"] = dtype
                            item["val"] = val
                            break
                    
                self.load_table_data()
