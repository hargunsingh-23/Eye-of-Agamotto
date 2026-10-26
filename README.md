# Eye of Agamotto 👁️‍🗨️
**Real-time, offline desktop system monitor built with Python, PySide6, and pyqtgraph.**

## Features
- **Live System Graphs**: Real-time plotting of CPU, Memory, and Disk usage via `pyqtgraph`.
- **Background Sampler Threads**: Keeps the UI responsive even under 100% load.
- **Process Manager & SafetyGuard**: Lists running tasks sorted by resource consumption and blocks accidental termination of critical system processes.
- **Compact Mode**: Always-on-top micro window for continuous background viewing.
- **Customizable Alerts**: Hysteresis and cooldown-based notifications to prevent notification spam.

## Installation & Running
1. Clone the repository:
   ```bash
   git clone [https://github.com/your-username/eye-of-agamotto.git](https://github.com/your-username/eye-of-agamotto.git)
   cd eye-of-agamotto