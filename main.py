import sys
import os
import time
import json
from collections import deque
import psutil

from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QMessageBox, QHeaderView, QGroupBox, QFormAlignmentProxy
)
from PySide6.QtGui import QColor, QIcon

import pyqtgraph as pg

CONFIG_FILE = "config.json"

# ==========================================
# 1. DATA STRUCTURES & SAFETY GUARD
# ==========================================
class MetricSample:
    def __init__(self, cpu_pct, mem_pct, disk_pct, battery_pct, plugged):
        self.timestamp = time.time()
        self.cpu_pct = cpu_pct
        self.mem_pct = mem_pct
        self.disk_pct = disk_pct
        self.battery_pct = battery_pct
        self.plugged = plugged


class RingBuffer:
    def __init__(self, capacity=3600):
        self.capacity = capacity
        self.data = deque(maxlen=capacity)

    def add(self, sample):
        self.data.append(sample)

    def snapshot(self):
        return list(self.data)


class SafetyGuard:
    PROTECTED_NAMES = {
        "system", "smss.cs", "csrss.exe", "wininit.exe", "services.exe",
        "lsass.exe", "winlogon.exe", "explorer.exe", "launchd", "kernel_task",
        "systemd", "init", "python", "python3", "pycharm", "code"
    }

    @staticmethod
    def is_protected(proc_name, pid):
        if pid <= 1:
            return True
        if proc_name.lower() in SafetyGuard.PROTECTED_NAMES:
            return True
        return False


# ==========================================
# 2. BACKGROUND WORKERS (THREADS)
# ==========================================
class SystemSampler(QThread):
    sample_ready = Signal(object)

    def __init__(self, interval_s=1.0):
        super().__init__()
        self.interval_s = interval_s
        self._running = True
        # Initial call to prime CPU percent calculation
        psutil.cpu_percent(interval=None)

    def run(self):
        while self._running:
            time.sleep(self.interval_s)
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                battery = psutil.sensors_battery()
                bat_pct = battery.percent if battery else 100.0
                plugged = battery.power_plugged if battery else True

                sample = MetricSample(cpu, mem, disk, bat_pct, plugged)
                self.sample_ready.emit(sample)
            except Exception as e:
                print(f"Sampler error: {e}")

    def stop(self):
        self._running = False
        self.wait()


class ProcessSampler(QThread):
    processes_ready = Signal(list)

    def __init__(self, interval_s=3.0, top_n=15):
        super().__init__()
        self.interval_s = interval_s
        self.top_n = top_n
        self._running = True

    def run(self):
        while self._running:
            time.sleep(self.interval_s)
            procs = []
            try:
                for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent', 'create_time']):
                    try:
                        info = p.info
                        # Normalize None values
                        info['cpu_percent'] = info['cpu_percent'] or 0.0
                        info['memory_percent'] = info['memory_percent'] or 0.0
                        procs.append(info)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                # Sort by CPU usage descending
                procs = sorted(procs, key=lambda x: x['cpu_percent'], reverse=True)[:self.top_n]
                self.processes_ready.emit(procs)
            except Exception as e:
                print(f"Process Sampler error: {e}")

    def stop(self):
        self._running = False
        self.wait()


# ==========================================
# 3. ALERT ENGINE & NOTIFIER
# ==========================================
class AlertRule:
    def __init__(self, metric, threshold, hold_s=3, cooldown_s=30):
        self.metric = metric  # 'cpu', 'mem', 'disk'
        self.threshold = threshold
        self.hold_s = hold_s
        self.cooldown_s = cooldown_s
        self.enabled = True
        self._violation_start = None
        self._last_alert_time = 0

    def evaluate(self, sample, notify_callback):
        if not self.enabled:
            return

        val = getattr(sample, f"{self.metric}_pct", 0)
        now = time.time()

        if val >= self.threshold:
            if self._violation_start is None:
                self._violation_start = now
            elif (now - self._violation_start) >= self.hold_s:
                if (now - self._last_alert_time) >= self.cooldown_s:
                    self._last_alert_time = now
                    notify_callback(f"High {self.metric.upper()} Usage Alert!", 
                                    f"{self.metric.upper()} usage is at {val:.1f}% (Threshold: {self.threshold}%)")
        else:
            self._violation_start = None


# ==========================================
# 4. COMPACT WINDOW (Always on Top)
# ==========================================
class CompactWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Eye of Agamotto [Compact]")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.resize(250, 120)

        layout = QVBoxLayout()
        self.label = QLabel("Waiting for metrics...")
        self.label.setStyleSheet("font-size: 14px; font-weight: bold; color: #00FF7F;")
        layout.addWidget(self.label)
        self.setLayout(layout)

    def update_metrics(self, sample):
        text = f"CPU: {sample.cpu_pct}%\nMEM: {sample.mem_pct}%\nDISK: {sample.disk_pct}%\nBattery: {sample.battery_pct}%"
        self.label.setText(text)


# ==========================================
# 5. MAIN APPLICATION WINDOW
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Eye of Agamotto - Real-Time System Monitor")
        self.resize(950, 650)

        self.ring_buffer = RingBuffer(capacity=3600)
        self.rules = [
            AlertRule('cpu', threshold=85.0),
            AlertRule('mem', threshold=90.0),
            AlertRule('disk', threshold=95.0)
        ]
        
        self.load_settings()

        # Main Layout / Tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.init_dashboard_tab()
        self.init_processes_tab()

        # Compact Window Instance
        self.compact_window = CompactWindow(self)

        # Start Sampler Threads
        self.system_sampler = SystemSampler(interval_s=1.0)
        self.system_sampler.sample_ready.connect(self.on_new_sample)
        self.system_sampler.start()

        self.process_sampler = ProcessSampler(interval_s=3.0, top_n=20)
        self.process_sampler.processes_ready.connect(self.on_new_processes)
        self.process_sampler.start()

    def init_dashboard_tab(self):
        dash_widget = QWidget()
        layout = QVBoxLayout()

        # Top Control Bar
        control_layout = QHBoxLayout()
        self.compact_btn = QPushButton("Toggle Compact Mode")
        self.compact_btn.clicked.connect(self.toggle_compact_mode)
        control_layout.addWidget(self.compact_btn)
        control_layout.addStretch()
        layout.addLayout(control_layout)

        # Live Graphs via pyqtgraph
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#1e1e1e')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setYRange(0, 100)
        self.plot_widget.setTitle("System Resource Usage (%)", color="w", size="12pt")
        
        # Curves
        self.cpu_curve = self.plot_widget.plot(pen=pg.mkPen(color='#FF5733', width=2), name='CPU')
        self.mem_curve = self.plot_widget.plot(pen=pg.mkPen(color='#33FF57', width=2), name='Memory')
        self.disk_curve = self.plot_widget.plot(pen=pg.mkPen(color='#3357FF', width=2), name='Disk')

        layout.addWidget(self.plot_widget)
        dash_widget.setLayout(layout)
        self.tabs.addTab(dash_widget, "Dashboard & Graphs")

    def init_processes_tab(self):
        proc_widget = QWidget()
        layout = QVBoxLayout()

        # Process Table
        self.proc_table = QTableWidget()
        self.proc_table.setColumnCount(5)
        self.proc_table.setHorizontalHeaderLabels(["PID", "Name", "User", "CPU %", "Memory %"])
        self.proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.proc_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.proc_table)

        # Terminate Button
        btn_layout = QHBoxLayout()
        self.term_btn = QPushButton("Terminate Selected Process")
        self.term_btn.setStyleSheet("background-color: #d9534f; color: white; font-weight: bold;")
        self.term_btn.clicked.connect(self.terminate_selected_process)
        btn_layout.addStretch()
        btn_layout.addWidget(self.term_btn)
        layout.addLayout(btn_layout)

        proc_widget.setLayout(layout)
        self.tabs.addTab(proc_widget, "Running Processes")

    @Slot(object)
    def on_new_sample(self, sample):
        self.ring_buffer.add(sample)
        
        # Update Graphs
        history = self.ring_buffer.snapshot()
        x_data = list(range(len(history)))
        cpu_data = [s.cpu_pct for s in history]
        mem_data = [s.mem_pct for s in history]
        disk_data = [s.disk_pct for s in history]

        self.cpu_curve.setData(x_data, cpu_data)
        self.mem_curve.setData(x_data, mem_data)
        self.disk_curve.setData(x_data, disk_data)

        # Evaluate Alerts
        for rule in self.rules:
            rule.evaluate(sample, self.show_desktop_notification)

        # Update Compact Window if visible
        if self.compact_window.isVisible():
            self.compact_window.update_metrics(sample)

    @Slot(list)
    def on_new_processes(self, procs):
        self.proc_table.setRowCount(len(procs))
        for row_idx, p in enumerate(procs):
            self.proc_table.setItem(row_idx, 0, QTableWidgetItem(str(p['pid'])))
            self.proc_table.setItem(row_idx, 1, QTableWidgetItem(str(p['name'])))
            self.proc_table.setItem(row_idx, 2, QTableWidgetItem(str(p['username'])))
            self.proc_table.setItem(row_idx, 3, QTableWidgetItem(f"{p['cpu_percent']:.1f}"))
            self.proc_table.setItem(row_idx, 4, QTableWidgetItem(f"{p['memory_percent']:.1f}"))

    def show_desktop_notification(self, title, message):
        # Fallback cross-platform message box alert if system tray notification isn't configured
        print(f"[ALERT] {title}: {message}")

    def terminate_selected_process(self):
        selected_rows = self.proc_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "Warning", "Please select a process to terminate.")
            return

        row = selected_rows[0].row()
        pid = int(self.proc_table.item(row, 0).text())
        name = self.proc_table.item(row, 1).text()

        # Safety Guard Check
        if SafetyGuard.is_protected(name, pid):
            QMessageBox.critical(self, "Access Denied", f"SafetyGuard prevented termination of protected process: {name} (PID: {pid})")
            return

        confirm = QMessageBox.question(
            self, "Confirm Termination",
            f"Are you sure you want to terminate process '{name}' (PID: {pid})?",
            QMessageBox.Yes | QMessageBox.No
        )

        if confirm == QMessageBox.Yes:
            try:
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=3)
                QMessageBox.information(self, "Success", f"Process {name} terminated successfully.")
            except psutil.AccessDenied:
                QMessageBox.critical(self, "Error", "Access denied: Insufficient permissions to terminate this process.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to terminate process: {e}")

    def toggle_compact_mode(self):
        if self.compact_window.isVisible():
            self.compact_window.hide()
        else:
            self.compact_window.show()

    def load_settings(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    # Load rules thresholds if present
                    pass
            except Exception:
                pass

    def save_settings(self):
        try:
            data = {"rules": [{"metric": r.metric, "threshold": r.threshold} for r in self.rules]}
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    def closeEvent(self, event):
        self.system_sampler.stop()
        self.process_sampler.stop()
        self.save_settings()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())