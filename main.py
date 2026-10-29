import sys
import os
import time
import json
import csv
import sqlite3
import ctypes
import platform
import re
import subprocess
import tempfile
import random
from datetime import datetime, timezone, timedelta
from collections import deque
import psutil

# Optional GPU monitoring library (offline)
try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False

from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QComboBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QFileDialog,
    QDialog,
    QFormLayout,
    QSpinBox,
    QDoubleSpinBox,
    QDialogButtonBox,
    QTextEdit
)
from PySide6.QtGui import QColor, QIcon

import pyqtgraph as pg

CONFIG_FILE = "config.json"
DB_FILE = "telemetry_history.db"
IS_WINDOWS = sys.platform.startswith("win")

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30), "IST")
IST_OFFSET_S = 5.5 * 3600

# How much history the graph shows while "Follow live" is on
VIEW_WINDOWS = [
    ("Last 1 min", 60),
    ("Last 5 min", 300),
    ("Last 15 min", 900),
    ("Last 1 hour", 3600),
]

BYTES_PER_GB = 1024 ** 3
BYTES_PER_MB = 1024 ** 2
BYTES_PER_KB = 1024

# ------------------------------------------
# Efficiency score settings
# ------------------------------------------
EFFICIENCY_WEIGHTS = {"cpu": 0.30, "mem": 0.25, "gpu": 0.15, "net": 0.10, "disk_io": 0.10, "disk_space": 0.10}
EFFICIENCY_SMOOTH_SAMPLES = 10
EFFICIENCY_RATINGS = [
    (80, "Excellent", "#2ecc71"),
    (60, "Good", "#8BC34A"),
    (40, "Fair", "#FFC107"),
    (0, "Poor", "#e74c3c"),
]

BATTERY_HEALTH_RATINGS = [
    (90, "Excellent", "#2ecc71"),
    (80, "Good", "#8BC34A"),
    (60, "Fair", "#FFC107"),
    (0, "Poor", "#e74c3c"),
]
BATTERY_HEALTH_REFRESH_S = 1800


def badge_style(bg, fg="#111111"):
    return (f"background-color: {bg}; color: {fg}; font-weight: bold;"
            "padding: 6px 14px; border-radius: 6px;")


# ==========================================
# 0. DATABASE MANAGER (SQLite Long-Term Storage)
# ==========================================
class TelemetryDatabase:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS telemetry_logs (
                        timestamp REAL PRIMARY KEY,
                        cpu_pct REAL,
                        mem_pct REAL,
                        gpu_pct REAL,
                        net_up_kbps REAL,
                        net_down_kbps REAL,
                        disk_pct REAL,
                        disk_activity_pct REAL,
                        battery_pct REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"Database init error: {e}")

    def insert_sample(self, sample):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO telemetry_logs 
                    (timestamp, cpu_pct, mem_pct, gpu_pct, net_up_kbps, net_down_kbps, disk_pct, disk_activity_pct, battery_pct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sample.timestamp,
                    sample.cpu_pct,
                    sample.mem_pct,
                    sample.gpu_pct,
                    sample.net_up_kbps,
                    sample.net_down_kbps,
                    sample.disk_pct,
                    sample.disk_activity_pct,
                    sample.battery_pct
                ))
                conn.commit()
        except Exception as e:
            print(f"Database insert error: {e}")

    def get_daily_peaks(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        date(timestamp, 'unixepoch', 'localtime') as day,
                        MAX(cpu_pct), MAX(mem_pct), MAX(gpu_pct), MAX(net_down_kbps)
                    FROM telemetry_logs
                    GROUP BY day
                    ORDER BY day DESC
                    LIMIT 7
                """)
                return cursor.fetchall()
        except Exception:
            return []


# ==========================================
# 1. DEVICE / SYSTEM HELPERS
# ==========================================
def get_device_name():
    return platform.node() or "Unknown Device"


def get_os_info():
    return f"{platform.system()} {platform.release()}"


def get_disk_info():
    if sys.platform.startswith("win"):
        drive = os.environ.get("SystemDrive", "C:")
        return drive + "\\", drive
    if sys.platform == "darwin" and os.path.exists("/System/Volumes/Data"):
        return "/System/Volumes/Data", "Data volume"
    return "/", "/"


DISK_PATH, DISK_LABEL = get_disk_info()


def disk_io_supported():
    try:
        return bool(psutil.disk_io_counters(perdisk=True))
    except Exception:
        return False


def get_gpu_info():
    if GPU_AVAILABLE:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu = gpus[0]
                return float(gpu.load * 100.0), float(gpu.memoryUsed), gpu.name
        except Exception:
            pass
    
    simulated_name = f"Integrated / Simulated GPU ({platform.system()})"
    simulated_load = float(random.randint(5, 25))
    return simulated_load, 512.0, simulated_name


def get_cpu_freq_info():
    try:
        freq = psutil.cpu_freq()
        if freq:
            current = freq.current / 1000.0
            min_f = freq.min / 1000.0 if freq.min else 0.0
            max_f = freq.max / 1000.0 if freq.max else 0.0
            return current, min_f, max_f
    except Exception:
        pass
    return None, None, None


# ==========================================
# 1a. WINDOWS DISK "ACTIVE TIME"
# ==========================================
class _PdhValue(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_int32),
                    ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_int64)]
    _anonymous_ = ("u",)
    _fields_ = [("CStatus", ctypes.c_uint32), ("u", _Union)]


class WindowsDiskCounter:
    COUNTER = r"\PhysicalDisk(_Total)\% Idle Time"
    PDH_FMT_DOUBLE = 0x00000200

    def __init__(self):
        pdh = ctypes.windll.pdh
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                    ctypes.POINTER(ctypes.c_uint32),
                                                    ctypes.POINTER(_PdhValue)]
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        self._pdh = pdh
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()

        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            raise OSError("PdhOpenQuery failed")
        if pdh.PdhAddEnglishCounterW(self._query, self.COUNTER, 0, ctypes.byref(self._counter)) != 0:
            pdh.PdhCloseQuery(self._query)
            raise OSError("Disk performance counter not available")
        pdh.PdhCollectQueryData(self._query)

    def read_active_pct(self):
        pdh = self._pdh
        if pdh.PdhCollectQueryData(self._query) != 0:
            return None
        value = _PdhValue()
        if pdh.PdhGetFormattedCounterValue(self._counter, self.PDH_FMT_DOUBLE, None,
                                           ctypes.byref(value)) != 0:
            return None
        if value.CStatus not in (0, 1):
            return None
        return min(max(100.0 - value.doubleValue, 0.0), 100.0)

    def close(self):
        try:
            self._pdh.PdhCloseQuery(self._query)
        except Exception:
            pass


# ==========================================
# 1b. BATTERY HEALTH
# ==========================================
class BatteryHealth:
    def __init__(self, health_pct, design_cap=None, full_cap=None, unit="",
                 cycles=None, condition=None, source=""):
        self.health_pct = min(max(health_pct, 0.0), 100.0)
        self.design_cap = design_cap
        self.full_cap = full_cap
        self.unit = unit
        self.cycles = cycles
        self.condition = condition
        self.source = source


def rate_battery_health(pct):
    for threshold, name, color in BATTERY_HEALTH_RATINGS:
        if pct >= threshold:
            return name, color
    return BATTERY_HEALTH_RATINGS[-1][1], BATTERY_HEALTH_RATINGS[-1][2]


def _decode_text(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    return raw.decode("utf-8", errors="ignore")


def _parse_powercfg_xml(text):
    design = [int(x) for x in re.findall(r"<DesignCapacity>\s*(\d+)\s*</DesignCapacity>", text)]
    full = [int(x) for x in re.findall(r"<FullChargeCapacity>\s*(\d+)\s*</FullChargeCapacity>", text)]
    cycles = [int(x) for x in re.findall(r"<CycleCount>\s*(\d+)\s*</CycleCount>", text)]
    d, f = sum(design), sum(full)
    if d <= 0 or f <= 0:
        return None
    cyc = max(cycles) if cycles and max(cycles) > 0 else None
    return BatteryHealth(f / d * 100.0, d / 1000.0, f / 1000.0, "Wh", cyc, None, "powercfg")


def _parse_ps_output(text):
    parts = [x.strip() for x in text.strip().split(",")]
    if len(parts) < 2:
        return None
    try:
        d, f = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if d <= 0 or f <= 0:
        return None
    cycles = None
    if len(parts) > 2:
        try:
            cycles = int(parts[2]) or None
        except ValueError:
            pass
    return BatteryHealth(f / d * 100.0, d / 1000.0, f / 1000.0, "Wh", cycles, None, "WMI")


def _health_windows():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    tmp = os.path.join(tempfile.gettempdir(), f"eye_of_agamotto_battery_{os.getpid()}.xml")
    try:
        subprocess.run(["powercfg", "/batteryreport", "/output", tmp, "/xml"],
                       capture_output=True, timeout=20, creationflags=flags)
        with open(tmp, "rb") as fh:
            health = _parse_powercfg_xml(_decode_text(fh.read()))
        if health:
            return health
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    ps = ("$ns='root\\wmi';"
          "$d=(Get-CimInstance -Namespace $ns -ClassName BatteryStaticData | Select-Object -First 1).DesignedCapacity;"
          "$f=(Get-CimInstance -Namespace $ns -ClassName BatteryFullChargedCapacity | Select-Object -First 1).FullChargedCapacity;"
          "$c=(Get-CimInstance -Namespace $ns -ClassName BatteryCycleCount | Select-Object -First 1).CycleCount;"
          "Write-Output \"$d,$f,$c\"")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=20, creationflags=flags)
        return _parse_ps_output(r.stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def _health_linux(base="/sys/class/power_supply"):
    if not os.path.isdir(base):
        return None
    design_total = full_total = 0.0
    unit, divisor = "", 1.0
    cycles = None
    found = False

    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)

        def read(fn):
            try:
                with open(os.path.join(d, fn)) as fh:
                    return float(fh.read().strip())
            except (OSError, ValueError):
                return None

        try:
            with open(os.path.join(d, "type")) as fh:
                is_battery = fh.read().strip() == "Battery"
        except OSError:
            is_battery = name.upper().startswith("BAT")
        if not is_battery:
            continue

        full_v, design_v = read("energy_full"), read("energy_full_design")
        this_unit, this_div = "Wh", 1e6
        if not full_v or not design_v:
            full_v, design_v = read("charge_full"), read("charge_full_design")
            this_unit, this_div = "mAh", 1e3
        if not full_v or not design_v:
            continue

        full_total += full_v
        design_total += design_v
        unit, divisor = this_unit, this_div
        found = True
        c = read("cycle_count")
        if c and c > 0:
            cycles = int(c)

    if not found:
        return None
    return BatteryHealth(full_total / design_total * 100.0, design_total / divisor,
                         full_total / divisor, unit, cycles, None, "sysfs")


def _parse_macos(text):
    m = re.search(r"Maximum Capacity:\s*(\d+)\s*%", text)
    if not m:
        return None
    cyc = re.search(r"Cycle Count:\s*(\d+)", text)
    cond = re.search(r"Condition:\s*(.+)", text)
    return BatteryHealth(float(m.group(1)), cycles=int(cyc.group(1)) if cyc else None,
                         condition=cond.group(1).strip() if cond else None,
                         source="system_profiler")


def _health_macos():
    r = subprocess.run(["system_profiler", "SPPowerDataType"],
                       capture_output=True, text=True, timeout=20)
    return _parse_macos(r.stdout)


def read_battery_health():
    try:
        if sys.platform.startswith("win"):
            return _health_windows()
        if sys.platform == "darwin":
            return _health_macos()
        return _health_linux()
    except Exception:
        return None


# ==========================================
# 2. DATA STRUCTURES & SAFETY GUARD
# ==========================================
class MetricSample:
    def __init__(self, cpu_pct, mem_pct, disk_pct, battery_pct, plugged, secsleft=None,
                 disk_activity_pct=None, disk_read_mbps=None, disk_write_mbps=None,
                 disk_activity_source=None, mem_used_gb=None, mem_total_gb=None,
                 disk_used_gb=None, disk_total_gb=None, gpu_pct=None, gpu_mem_mb=None,
                 cpu_freq_ghz=None, net_up_kbps=0.0, net_down_kbps=0.0):
        self.timestamp = time.time()
        self.cpu_pct = cpu_pct
        self.mem_pct = mem_pct
        self.disk_pct = disk_pct
        self.battery_pct = battery_pct
        self.plugged = plugged
        self.secsleft = secsleft

        self.disk_activity_pct = disk_activity_pct
        self.disk_read_mbps = disk_read_mbps
        self.disk_activity_source = disk_activity_source
        self.disk_write_mbps = disk_write_mbps

        self.mem_used_gb = mem_used_gb
        self.mem_total_gb = mem_total_gb
        self.disk_used_gb = disk_used_gb
        self.disk_total_gb = disk_total_gb
        self.gpu_pct = gpu_pct
        self.gpu_mem_mb = gpu_mem_mb
        self.cpu_freq_ghz = cpu_freq_ghz
        self.net_up_kbps = net_up_kbps
        self.net_down_kbps = net_down_kbps


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
        "system", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
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
# 3. EFFICIENCY SCORE
# ==========================================
def compute_efficiency(history):
    if not history:
        return None
    recent = history[-EFFICIENCY_SMOOTH_SAMPLES:]

    def avg(attr):
        vals = [getattr(s, attr) for s in recent if getattr(s, attr) is not None]
        return sum(vals) / len(vals) if vals else None

    net_down_avg = avg("net_down_kbps") or 0.0
    net_pct = min(net_down_avg / 100.0, 100.0)

    inputs = [
        ("cpu", "CPU", avg("cpu_pct")),
        ("mem", "Memory", avg("mem_pct")),
        ("gpu", "GPU", avg("gpu_pct")),
        ("net", "Network", net_pct),
        ("disk_io", "Disk activity", avg("disk_activity_pct")),
        ("disk_space", "Disk space", avg("disk_pct")),
    ]

    total_w = 0.0
    weighted = 0.0
    lines = []
    for key, label, used in inputs:
        if used is None:
            continue
        used = min(max(used, 0.0), 100.0)
        w = EFFICIENCY_WEIGHTS.get(key, 0.10)
        weighted += w * (100.0 - used)
        total_w += w
        lines.append(f"{label}: {used:.0f}% load -> {100 - used:.0f}% free (weight {w * 100:.0f}%)")

    if total_w == 0:
        return None

    score = weighted / total_w
    rating, color = EFFICIENCY_RATINGS[-1][1], EFFICIENCY_RATINGS[-1][2]
    for threshold, name, col in EFFICIENCY_RATINGS:
        if score >= threshold:
            rating, color = name, col
            break

    tooltip = ("Efficiency = weighted average of free capacity\n"
               f"(last {len(recent)}s average)\n\n" + "\n".join(lines))
    return score, rating, color, tooltip


# ==========================================
# 4. BACKGROUND WORKERS (THREADS)
# ==========================================
class SystemSampler(QThread):
    sample_ready = Signal(object)

    def __init__(self, interval_s=1.0):
        super().__init__()
        self.interval_s = interval_s
        self._running = True
        psutil.cpu_percent(interval=None)

        self._prev_time = time.time()
        self._prev_total_io = self._safe_io(False)
        self._prev_disk_io = self._safe_io(True)
        self._prev_net_io = psutil.net_io_counters()

        self._win_counter = None
        self._win_counter_failed = False
        self.db = TelemetryDatabase()

    @staticmethod
    def _safe_io(perdisk):
        try:
            return psutil.disk_io_counters(perdisk=perdisk)
        except Exception:
            return None

    @staticmethod
    def _busy_ms(counters):
        busy = getattr(counters, "busy_time", None)
        if busy is not None:
            return busy
        return (getattr(counters, "read_time", 0) or 0) + (getattr(counters, "write_time", 0) or 0)

    def _psutil_disk_io(self):
        now = time.time()
        total_io = self._safe_io(False)
        disk_io = self._safe_io(True)
        elapsed = now - self._prev_time
        result = (None, None, None)

        if elapsed > 0 and total_io is not None and self._prev_total_io is not None:
            read_mbps = (total_io.read_bytes - self._prev_total_io.read_bytes) / BYTES_PER_MB / elapsed
            write_mbps = (total_io.write_bytes - self._prev_total_io.write_bytes) / BYTES_PER_MB / elapsed

            active = None
            if disk_io and self._prev_disk_io:
                busiest = 0.0
                for name, cur in disk_io.items():
                    prev = self._prev_disk_io.get(name)
                    if prev is None:
                        continue
                    delta_ms = self._busy_ms(cur) - self._busy_ms(prev)
                    busiest = max(busiest, delta_ms / (elapsed * 1000.0) * 100.0)
                active = min(max(busiest, 0.0), 100.0)

            result = (active, max(read_mbps, 0.0), max(write_mbps, 0.0))

        self._prev_time, self._prev_total_io, self._prev_disk_io = now, total_io, disk_io
        return result

    def _disk_activity(self):
        active, read_mbps, write_mbps = self._psutil_disk_io()
        source = "psutil" if active is not None else None

        if IS_WINDOWS and not self._win_counter_failed:
            if self._win_counter is None:
                try:
                    self._win_counter = WindowsDiskCounter()
                except Exception:
                    self._win_counter_failed = True
            else:
                real = self._win_counter.read_active_pct()
                if real is not None:
                    active, source = real, "Windows counter"

        return active, read_mbps, write_mbps, source

    def _collect_sample(self):
        now = time.time()
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(DISK_PATH)
        battery = psutil.sensors_battery()
        gpu_pct, gpu_mem, _ = get_gpu_info()
        cpu_curr, _, _ = get_cpu_freq_info()

        net_io = psutil.net_io_counters()
        elapsed = now - self._prev_time
        net_up_kbps = 0.0
        net_down_kbps = 0.0
        if elapsed > 0 and self._prev_net_io:
            net_up_kbps = (net_io.bytes_sent - self._prev_net_io.bytes_sent) / BYTES_PER_KB / elapsed
            net_down_kbps = (net_io.bytes_recv - self._prev_net_io.bytes_recv) / BYTES_PER_KB / elapsed
        self._prev_net_io = net_io

        if battery is not None:
            bat_pct = battery.percent
            plugged = battery.power_plugged
            secsleft = battery.secsleft
        else:
            bat_pct, plugged, secsleft = None, None, None

        active, read_mbps, write_mbps, source = self._disk_activity()

        sample = MetricSample(
            cpu, vm.percent, du.percent, bat_pct, plugged, secsleft,
            disk_activity_pct=active,
            disk_read_mbps=read_mbps,
            disk_write_mbps=write_mbps,
            disk_activity_source=source,
            mem_used_gb=(vm.total - vm.available) / BYTES_PER_GB,
            mem_total_gb=vm.total / BYTES_PER_GB,
            disk_used_gb=du.used / BYTES_PER_GB,
            disk_total_gb=du.total / BYTES_PER_GB,
            gpu_pct=gpu_pct,
            gpu_mem_mb=gpu_mem,
            cpu_freq_ghz=cpu_curr,
            net_up_kbps=max(net_up_kbps, 0.0),
            net_down_kbps=max(net_down_kbps, 0.0)
        )

        self.db.insert_sample(sample)
        return sample

    def run(self):
        while self._running:
            time.sleep(self.interval_s)
            try:
                self.sample_ready.emit(self._collect_sample())
            except Exception as e:
                print(f"Sampler error: {e}")

    def stop(self):
        self._running = False
        self.wait()
        if self._win_counter is not None:
            self._win_counter.close()


class BatteryHealthSampler(QThread):
    health_ready = Signal(object)

    def __init__(self, refresh_s=BATTERY_HEALTH_REFRESH_S):
        super().__init__()
        self.refresh_s = refresh_s
        self._running = True

    def run(self):
        while self._running:
            self.health_ready.emit(read_battery_health())
            waited = 0
            while self._running and waited < self.refresh_s:
                time.sleep(1)
                waited += 1

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
        self._ncpu = psutil.cpu_count() or 1

        for _p in psutil.process_iter(['cpu_percent']):
            pass

    def run(self):
        while self._running:
            time.sleep(self.interval_s)
            procs = []
            try:
                for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_percent', 'create_time']):
                    try:
                        info = p.info
                        if info['pid'] == 0:
                            continue
                        info['cpu_percent'] = (info['cpu_percent'] or 0.0) / self._ncpu
                        info['memory_percent'] = info['memory_percent'] or 0.0
                        procs.append(info)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                procs = sorted(procs, key=lambda x: x['cpu_percent'], reverse=True)[:self.top_n]
                self.processes_ready.emit(procs)
            except Exception as e:
                print(f"Process Sampler error: {e}")

    def stop(self):
        self._running = False
        self.wait()


# ==========================================
# 5. ALERT ENGINE & NOTIFIER
# ==========================================
class AlertRule:
    def __init__(self, metric, threshold, hold_s=3, cooldown_s=30):
        self.metric = metric  # 'cpu', 'mem', 'gpu', 'disk'
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
        if val is None:
            return
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
# 6. SETTINGS & HISTORY DIALOGS
# ==========================================
class SettingsDialog(QDialog):
    def __init__(self, rules, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Alert Threshold Settings")
        self.resize(300, 220)

        layout = QFormLayout(self)
        self.spinboxes = {}

        for rule in rules:
            spin = QDoubleSpinBox()
            spin.setRange(1.0, 100.0)
            spin.setValue(rule.threshold)
            spin.setSuffix(" %")
            layout.addRow(f"High {rule.metric.upper()} Threshold:", spin)
            self.spinboxes[rule.metric] = spin

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_thresholds(self):
        return {metric: spin.value() for metric, spin in self.spinboxes.items()}


class HistoryDialog(QDialog):
    """Offline SQLite Historical Telemetry Viewer with Performance Advisor / Caution Advice"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Historical Telemetry Peaks & Performance Advisor")
        self.resize(650, 500)

        layout = QVBoxLayout(self)

        # History Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Date", "Peak CPU %", "Peak RAM %", "Peak GPU %", "Peak Download (KB/s)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        db = TelemetryDatabase()
        rows = db.get_daily_peaks()
        self.table.setRowCount(len(rows))
        
        max_cpu, max_ram, max_gpu = 0.0, 0.0, 0.0
        for r_idx, row in enumerate(rows):
            day, cpu, ram, gpu, net = row
            if cpu and cpu > max_cpu: max_cpu = cpu
            if ram and ram > max_ram: max_ram = ram
            if gpu and gpu > max_gpu: max_gpu = gpu

            for c_idx, val in enumerate(row):
                text = str(val) if val is not None else "0"
                if c_idx > 0 and isinstance(val, (int, float)):
                    text = f"{val:.1f}"
                self.table.setItem(r_idx, c_idx, QTableWidgetItem(text))

        # Performance Advisor / Caution Notes Section
        advice_group = QVBoxLayout()
        advice_title = QLabel("<b>⚠️ Where to Be Careful for Overall Best Performance:</b>")
        advice_title.setStyleSheet("font-size: 13px; color: #FFC107; margin-top: 10px;")
        advice_group.addWidget(advice_title)

        self.advice_box = QTextEdit()
        self.advice_box.setReadOnly(True)
        self.advice_box.setStyleSheet("background-color: #2b2b2b; color: #FFFFFF; font-size: 12px; padding: 8px; border-radius: 4px;")
        
        # Generate dynamic recommendations based on stored history peaks
        recommendations = []
        if not rows:
            recommendations.append("• Not enough historical data collected yet. Keep the monitor running while you work to log peaks!")
        else:
            if max_cpu > 85.0:
                recommendations.append(f"• <b>CPU Bottleneck Warning (Peak: {max_cpu:.1f}%):</b> Your processor has been pushed near its limit. Avoid running heavy background compiling, code parsing, or multiple heavy IDE instances concurrently without closing unused applications.")
            else:
                recommendations.append(f"• <b>CPU Status (Peak: {max_cpu:.1f}%):</b> CPU headroom is generally healthy. Good for multitasking.")

            if max_ram > 85.0:
                recommendations.append(f"• <b>Memory Caution (Peak: {max_ram:.1f}%):</b> RAM usage has reached critical heights. Be careful having too many browser tabs open simultaneously alongside memory-intensive tools to prevent stuttering or swap-file lag.")
            else:
                recommendations.append(f"• <b>Memory Status (Peak: {max_ram:.1f}%):</b> RAM capacity is stable.")

            if max_gpu > 85.0:
                recommendations.append(f"• <b>GPU High Usage Peak ({max_gpu:.1f}%):</b> Intensive graphical rendering, local machine learning models, or heavy UI animations were triggered. Watch out for thermal throttling on laptops during extended sessions.")

            recommendations.append("• <b>General Tip:</b> Always inspect the <i>Running Processes</i> tab to quickly terminate runaway background scripts or unresponsive tasks before they degrade overall system responsiveness.")

        self.advice_box.setHtml("<br>".join(recommendations))
        advice_group.addWidget(self.advice_box)
        layout.addLayout(advice_group)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


# ==========================================
# 7. COMPACT WINDOW
# ==========================================
class CompactWindow(QWidget):
    def __init__(self, device_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Eye of Agamotto [Compact] - {device_name}")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.resize(270, 250)

        layout = QVBoxLayout()

        self.device_label = QLabel(device_name)
        self.device_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #FFFFFF;")
        layout.addWidget(self.device_label)

        self.label = QLabel("Waiting for metrics...")
        self.label.setStyleSheet("font-size: 13px; font-weight: bold; color: #00FF7F;")
        layout.addWidget(self.label)
        self.setLayout(layout)

    def update_metrics(self, sample, efficiency=None):
        if sample.battery_pct is None:
            battery_text = "N/A"
        else:
            state = "Charging" if sample.plugged else "Discharging"
            battery_text = f"{sample.battery_pct:.0f}% ({state})"

        disk_act = "N/A" if sample.disk_activity_pct is None else f"{sample.disk_activity_pct:.1f}%"
        gpu_text = "N/A" if sample.gpu_pct is None else f"{sample.gpu_pct:.1f}%"
        cpu_freq_text = "N/A" if sample.cpu_freq_ghz is None else f"{sample.cpu_freq_ghz:.2f} GHz"
        eff_text = "--" if efficiency is None else f"{efficiency[0]:.0f}/100 ({efficiency[1]})"

        text = (
            f"CPU: {sample.cpu_pct}% ({cpu_freq_text})\n"
            f"MEM (RAM): {sample.mem_pct}%\n"
            f"GPU: {gpu_text}\n"
            f"NET: ↓{sample.net_down_kbps:.1f} KB/s\n"
            f"DISK SPACE: {sample.disk_pct}%\n"
            f"Battery: {battery_text}\n"
            f"Efficiency: {eff_text}"
        )
        self.label.setText(text)


# ==========================================
# 8. MAIN APPLICATION WINDOW
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.device_name = get_device_name()
        self.os_info = get_os_info()
        self.has_battery = psutil.sensors_battery() is not None
        self.has_disk_io = disk_io_supported()
        _, _, self.gpu_name = get_gpu_info()
        self._eff_color = None

        self.setWindowTitle(f"Eye of Agamotto - Real-Time System Monitor [{self.device_name}]")
        self.resize(1050, 720)

        self.ring_buffer = RingBuffer(capacity=3600)
        self.rules = [
            AlertRule('cpu', threshold=85.0),
            AlertRule('mem', threshold=90.0),
            AlertRule('gpu', threshold=90.0),
            AlertRule('disk', threshold=95.0)
        ]

        self.load_settings()

        central = QWidget()
        central_layout = QVBoxLayout(central)

        header_row = QHBoxLayout()

        self.header_label = QLabel(f"Device: {self.device_name}    |    {self.os_info}")
        self.header_label.setAlignment(Qt.AlignCenter)
        self.header_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #FFFFFF;"
            "background-color: #2b2b2b; padding: 8px; border-radius: 6px;"
        )
        header_row.addWidget(self.header_label, 1)

        self.efficiency_label = QLabel("EFFICIENCY\n-- / 100")
        self.efficiency_label.setAlignment(Qt.AlignCenter)
        self.efficiency_label.setMinimumWidth(190)
        self.efficiency_label.setStyleSheet(
            "background-color: #555555; color: #FFFFFF; font-weight: bold;"
            "padding: 6px 14px; border-radius: 6px;"
        )
        self.efficiency_label.setToolTip("Collecting data...")
        header_row.addWidget(self.efficiency_label, 0, Qt.AlignRight)

        self.health_label = QLabel("BATTERY HEALTH\nReading...")
        self.health_label.setAlignment(Qt.AlignCenter)
        self.health_label.setMinimumWidth(190)
        self.health_label.setStyleSheet(badge_style("#555555", "#FFFFFF"))
        self.health_label.setToolTip("Reading battery capacity data...")
        header_row.addWidget(self.health_label, 0, Qt.AlignRight)
        self.health_label.setVisible(self.has_battery)

        central_layout.addLayout(header_row)

        self.tabs = QTabWidget()
        central_layout.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.init_dashboard_tab()
        self.init_processes_tab()
        self.init_specs_tab()

        self.compact_window = CompactWindow(self.device_name, self)

        self.system_sampler = SystemSampler(interval_s=1.0)
        self.system_sampler.sample_ready.connect(self.on_new_sample)
        self.system_sampler.start()

        self.process_sampler = ProcessSampler(interval_s=3.0, top_n=20)
        self.process_sampler.processes_ready.connect(self.on_new_processes)
        self.process_sampler.start()

        self.battery_health_sampler = None
        if self.has_battery:
            self.battery_health_sampler = BatteryHealthSampler()
            self.battery_health_sampler.health_ready.connect(self.on_battery_health)
            self.battery_health_sampler.start()

    def init_dashboard_tab(self):
        dash_widget = QWidget()
        layout = QVBoxLayout()

        control_layout = QHBoxLayout()
        self.compact_btn = QPushButton("Toggle Compact Mode")
        self.compact_btn.clicked.connect(self.toggle_compact_mode)
        control_layout.addWidget(self.compact_btn)

        self.settings_btn = QPushButton("Alert Settings")
        self.settings_btn.clicked.connect(self.open_settings_dialog)
        control_layout.addWidget(self.settings_btn)

        self.history_btn = QPushButton("View SQLite History & Advisor")
        self.history_btn.clicked.connect(self.open_history_dialog)
        control_layout.addWidget(self.history_btn)

        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export_logs_to_csv)
        control_layout.addWidget(self.export_btn)

        self.battery_label = QLabel("Battery: --")
        self.battery_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #FFC107; margin-left: 10px;")
        control_layout.addWidget(self.battery_label)

        control_layout.addStretch()

        self.follow_chk = QCheckBox("Follow live")
        self.follow_chk.setChecked(True)
        self.follow_chk.toggled.connect(self.on_follow_toggled)
        control_layout.addWidget(self.follow_chk)

        self.window_combo = QComboBox()
        for label, secs in VIEW_WINDOWS:
            self.window_combo.addItem(label, secs)
        self.window_combo.currentIndexChanged.connect(self.on_window_changed)
        control_layout.addWidget(self.window_combo)

        self.clock_label = QLabel("IST --:--:--")
        self.clock_label.setStyleSheet("font-size: 13px; font-weight: bold; margin-left: 10px;")
        control_layout.addWidget(self.clock_label)

        layout.addLayout(control_layout)

        self.time_axis = pg.DateAxisItem(orientation='bottom', utcOffset=0)
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': self.time_axis})
        self.plot_widget.setBackground('#1e1e1e')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setYRange(0, 100, padding=0.02)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.plot_widget.setTitle(
            f"{self.device_name} - System Resource Usage (%)", color="w", size="12pt"
        )
        self.plot_widget.setLabel('left', 'Usage / Metric (%)')
        self.plot_widget.setLabel('bottom', 'Time (IST)')

        self.legend = self.plot_widget.addLegend(
            offset=(10, 10),
            labelTextColor='w',
            brush=(30, 30, 30, 200)
        )

        self.cpu_curve = self.plot_widget.plot(
            pen=pg.mkPen(color='#FF5733', width=2), name='CPU Usage')
        self.mem_curve = self.plot_widget.plot(
            pen=pg.mkPen(color='#33FF57', width=2), name='Memory Usage (RAM)')
        
        self.gpu_curve = self.plot_widget.plot(
            pen=pg.mkPen(color='#00E5FF', width=2), name=f'GPU Usage ({self.gpu_name})')

        self.net_curve = self.plot_widget.plot(
            pen=pg.mkPen(color='#FFEA00', width=2, style=Qt.DashLine), name='Network Down (KB/s)')

        self.disk_curve = self.plot_widget.plot(
            pen=pg.mkPen(color='#3399FF', width=2, style=Qt.DotLine),
            name=f'Disk Space Used ({DISK_LABEL})')

        self.disk_io_curve = None
        if self.has_disk_io:
            self.disk_io_curve = self.plot_widget.plot(
                pen=pg.mkPen(color='#E040FB', width=2), name='Disk Activity')

        self.battery_curve = None
        if self.has_battery:
            self.battery_curve = self.plot_widget.plot(
                pen=pg.mkPen(color='#FFC107', width=2, style=Qt.DashLine),
                name='Battery Level')

        layout.addWidget(self.plot_widget)

        self.stats_label = QLabel("Collecting data...")
        self.stats_label.setStyleSheet("font-size: 12px;")
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)

        dash_widget.setLayout(layout)
        self.tabs.addTab(dash_widget, "Dashboard & Graphs")

        self.apply_follow_range()

    def init_processes_tab(self):
        proc_widget = QWidget()
        layout = QVBoxLayout()

        self.proc_table = QTableWidget()
        self.proc_table.setColumnCount(5)
        self.proc_table.setHorizontalHeaderLabels(["PID", "Name", "User", "CPU %", "Memory %"])
        self.proc_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.proc_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.proc_table)

        btn_layout = QHBoxLayout()
        self.term_btn = QPushButton("Terminate Selected Process")
        self.term_btn.setStyleSheet("background-color: #d9534f; color: white; font-weight: bold;")
        self.term_btn.clicked.connect(self.terminate_selected_process)
        btn_layout.addStretch()
        btn_layout.addWidget(self.term_btn)
        layout.addLayout(btn_layout)

        proc_widget.setLayout(layout)
        self.tabs.addTab(proc_widget, "Running Processes")

    def init_specs_tab(self):
        specs_widget = QWidget()
        layout = QVBoxLayout()

        _, cpu_min, cpu_max = get_cpu_freq_info()
        freq_str = f"Min: {cpu_min:.2f} GHz, Max: {cpu_max:.2f} GHz" if cpu_min else "Dynamic / Not available"

        info = [
            ("Node / Hostname", platform.node()),
            ("Operating System", f"{platform.system()} {platform.release()} ({platform.version()})"),
            ("Architecture", platform.machine()),
            ("Processor", platform.processor() or "Unknown"),
            ("CPU Frequency Specs", freq_str),
            ("Graphics Card (GPU)", self.gpu_name),
            ("Physical CPU Cores", str(psutil.cpu_count(logical=False))),
            ("Logical CPU Cores", str(psutil.cpu_count(logical=True))),
            ("Total Physical RAM", f"{psutil.virtual_memory().total / (1024**3):.2f} GB"),
            ("System Drive Path", DISK_PATH),
        ]

        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Property", "Specification"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setRowCount(len(info))

        for row, (prop, val) in enumerate(info):
            table.setItem(row, 0, QTableWidgetItem(prop))
            table.setItem(row, 1, QTableWidgetItem(val))

        layout.addWidget(table)
        specs_widget.setLayout(layout)
        self.tabs.addTab(specs_widget, "System Specs")

    def apply_follow_range(self, now_ist=None):
        if now_ist is None:
            now_ist = time.time() + IST_OFFSET_S
        window_s = self.window_combo.currentData()
        self.plot_widget.setXRange(now_ist - window_s, now_ist, padding=0)

    def on_follow_toggled(self, checked):
        self.plot_widget.setMouseEnabled(x=not checked, y=False)
        if checked:
            self.apply_follow_range()

    def on_window_changed(self, _index):
        if self.follow_chk.isChecked():
            self.apply_follow_range()

    @Slot(object)
    def on_battery_health(self, health):
        if health is None:
            self.health_label.setText("BATTERY HEALTH\nNot available")
            self.health_label.setStyleSheet(badge_style("#555555", "#FFFFFF"))
            return

        rating, color = rate_battery_health(health.health_pct)
        self.health_label.setText(f"BATTERY HEALTH\n{health.health_pct:.0f}%  -  {rating}")
        self.health_label.setStyleSheet(badge_style(color))

        lines = ["Battery health = full-charge capacity now / capacity when new"]
        if health.full_cap is not None and health.design_cap is not None:
            lines.append(f"Capacity now: {health.full_cap:.1f} {health.unit} of "
                         f"{health.design_cap:.1f} {health.unit} (when new)")
        if health.cycles:
            lines.append(f"Charge cycles: {health.cycles}")
        self.health_label.setToolTip("\n".join(lines))

    def update_efficiency_badge(self, eff):
        if eff is None:
            return
        score, rating, color, tooltip = eff
        self.efficiency_label.setText(f"EFFICIENCY\n{score:.0f} / 100  -  {rating}")
        self.efficiency_label.setToolTip(tooltip)
        if color != self._eff_color:
            self._eff_color = color
            self.efficiency_label.setStyleSheet(badge_style(color))

    @staticmethod
    def format_stats(sample):
        parts = []
        if sample.mem_used_gb is not None:
            parts.append(f"RAM: {sample.mem_used_gb:.1f} / {sample.mem_total_gb:.1f} GB ({sample.mem_pct:.0f}%)")
        if sample.cpu_freq_ghz is not None:
            parts.append(f"CPU: {sample.cpu_freq_ghz:.2f} GHz")
        if sample.gpu_pct is not None:
            parts.append(f"GPU: {sample.gpu_pct:.1f}%")
        parts.append(f"Network: ↑{sample.net_up_kbps:.1f} KB/s, ↓{sample.net_down_kbps:.1f} KB/s")
        if sample.disk_used_gb is not None:
            parts.append(f"Disk: {sample.disk_used_gb:.0f}/{sample.disk_total_gb:.0f} GB ({sample.disk_pct:.0f}%)")
        return "   |   ".join(parts)

    @staticmethod
    def compute_drain_rate(history, window_s=300):
        if not history:
            return None
        now = history[-1].timestamp
        recent = [s for s in history
                  if now - s.timestamp <= window_s and s.battery_pct is not None]
        if len(recent) < 2 or any(s.plugged for s in recent):
            return None
        elapsed_min = (recent[-1].timestamp - recent[0].timestamp) / 60.0
        if elapsed_min < 1.0:
            return None
        drop = recent[0].battery_pct - recent[-1].battery_pct
        return max(drop / elapsed_min, 0.0)

    def format_battery_status(self, sample, history):
        if sample.battery_pct is None:
            return "Battery: not detected"

        state = "Charging" if sample.plugged else "Discharging"
        text = f"Battery: {sample.battery_pct:.0f}% ({state})"

        if not sample.plugged:
            secs = sample.secsleft
            if (secs is not None
                    and secs not in (psutil.POWER_TIME_UNKNOWN, psutil.POWER_TIME_UNLIMITED)
                    and secs > 0):
                hours, mins = divmod(int(secs) // 60, 60)
                text += f"  |  ~{hours}h {mins}m left"

            rate = self.compute_drain_rate(history)
            if rate is not None:
                text += f"  |  Drain: {rate:.2f}%/min"
        return text

    @Slot(object)
    def on_new_sample(self, sample):
        self.ring_buffer.add(sample)

        history = self.ring_buffer.snapshot()
        x_data = [s.timestamp + IST_OFFSET_S for s in history]
        cpu_data = [s.cpu_pct for s in history]
        mem_data = [s.mem_pct for s in history]
        net_data = [s.net_down_kbps for s in history]
        disk_data = [s.disk_pct for s in history]

        self.cpu_curve.setData(x_data, cpu_data)
        self.mem_curve.setData(x_data, mem_data)
        self.net_curve.setData(x_data, net_data)
        self.disk_curve.setData(x_data, disk_data)

        if self.gpu_curve is not None:
            gpu_data = [s.gpu_pct if s.gpu_pct is not None else float('nan') for s in history]
            self.gpu_curve.setData(x_data, gpu_data, connect='finite')

        if self.disk_io_curve is not None:
            io_data = [s.disk_activity_pct if s.disk_activity_pct is not None else float('nan')
                       for s in history]
            self.disk_io_curve.setData(x_data, io_data, connect='finite')

        if self.battery_curve is not None:
            bat_data = [s.battery_pct if s.battery_pct is not None else float('nan')
                        for s in history]
            self.battery_curve.setData(x_data, bat_data, connect='finite')

        if self.follow_chk.isChecked():
            self.apply_follow_range(x_data[-1])

        self.clock_label.setText(datetime.now(IST).strftime("IST  %d %b %Y  %H:%M:%S"))
        self.battery_label.setText(self.format_battery_status(sample, history))
        self.stats_label.setText(self.format_stats(sample))

        efficiency = compute_efficiency(history)
        self.update_efficiency_badge(efficiency)

        for rule in self.rules:
            rule.evaluate(sample, self.show_desktop_notification)

        if self.compact_window.isVisible():
            self.compact_window.update_metrics(sample, efficiency)

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
        print(f"[ALERT] {title}: {message}")

    def terminate_selected_process(self):
        selected_rows = self.proc_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "Warning", "Please select a process to terminate.")
            return

        row = selected_rows[0].row()
        pid = int(self.proc_table.item(row, 0).text())
        name = self.proc_table.item(row, 1).text()

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

    def open_settings_dialog(self):
        dialog = SettingsDialog(self.rules, self)
        if dialog.exec() == QDialog.Accepted:
            new_thresholds = dialog.get_thresholds()
            for rule in self.rules:
                if rule.metric in new_thresholds:
                    rule.threshold = new_thresholds[rule.metric]
            self.save_settings()
            QMessageBox.information(self, "Settings Saved", "Alert thresholds successfully updated.")

    def open_history_dialog(self):
        dialog = HistoryDialog(self)
        dialog.exec()

    def export_logs_to_csv(self):
        history = self.ring_buffer.snapshot()
        if not history:
            QMessageBox.warning(self, "Export", "No metric history available to export yet.")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "Export Performance Logs", "system_metrics_log.csv", "CSV Files (*.csv)")
        if not file_path:
            return

        try:
            with open(file_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "CPU %", "CPU Freq (GHz)", "RAM %", "GPU %", "Net Up (KB/s)", "Net Down (KB/s)", "Disk Space %", "Disk Activity %", "Battery %", "Plugged"])
                for s in history:
                    writer.writerow([
                        s.timestamp, s.cpu_pct, s.cpu_freq_ghz, s.mem_pct, s.gpu_pct,
                        s.net_up_kbps, s.net_down_kbps, s.disk_pct,
                        s.disk_activity_pct, s.battery_pct, s.plugged
                    ])
            QMessageBox.information(self, "Success", f"Logs successfully exported to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to save CSV file:\n{e}")

    def load_settings(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    saved_rules = data.get("rules", [])
                    for sr in saved_rules:
                        for rule in self.rules:
                            if rule.metric == sr.get("metric"):
                                rule.threshold = sr.get("threshold", rule.threshold)
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
        if self.battery_health_sampler is not None:
            self.battery_health_sampler.stop()
        self.save_settings()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
