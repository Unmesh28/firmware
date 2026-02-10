# Blinksmart DMS — Pi Zero 2W Deployment Plan

## Hardware Assumptions
- **Board**: Raspberry Pi Zero 2W (BCM2710A1, 512MB RAM)
- **OS**: 32-bit Raspberry Pi OS Lite (Bookworm) — `armv7l`
- **Cooling**: 5V fan (active cooling — better than passive heatsink alone)
- **Camera**: CSI camera module (OV5647 or similar)
- **GPS**: UART GPS module via `/dev/ttyAMA0` or gpsd
- **Storage**: SD card (A1 or A2 rated recommended)

## Current Codebase State (main branch)
- Uses **MySQL** (`mysql-connector-python`) for persistent storage
- Uses **Redis** for GPS IPC between `get_gps_data.py` and `facil_event_capture.py`
- Has `pi_optimize.sh` setup script (needs `gpu_mem` fix — currently 16, needs 64 for camera)
- Systemd services already defined with priority-based resource isolation
- Dependencies include unused packages: `pygame`, `firebase_admin`

---

## Execution Plan — 12 Steps

### Step 1: Flash OS & First Boot
**Where**: Your laptop/desktop (Raspberry Pi Imager)

1. Download **Raspberry Pi OS Lite (32-bit, Bookworm)** — `raspios-bookworm-armhf-lite`
   - NOT 64-bit, NOT Desktop edition
2. Flash to SD card using Raspberry Pi Imager
3. In Imager settings (gear icon), pre-configure:
   - Hostname: `blinksmart`
   - Enable SSH: Yes (password-based)
   - Username/password: `pi` / `<your-password>`
   - WiFi: Your network SSID + password
   - Locale/timezone as appropriate
4. Insert SD card into Pi Zero 2W, power on
5. SSH in:
   ```bash
   ssh pi@blinksmart.local
   ```

**Verify**:
```bash
uname -m          # Must show: armv7l
cat /etc/os-release  # Should show Bookworm
free -h           # ~430-460MB total RAM
```

---

### Step 2: System Update & Essential Packages
**Where**: Pi (SSH)

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y git htop iotop vim python3-pip python3-venv \
    libatlas-base-dev libopenblas-dev libjpeg-dev libpng-dev \
    libhdf5-dev libharfbuzz-dev libwebp-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    v4l-utils gpsd gpsd-clients sqlite3
```

This installs build dependencies for OpenCV/MediaPipe and tools for GPS and monitoring.

---

### Step 3: Overclock & Boot Config
**Where**: Pi (SSH) — edit `/boot/firmware/config.txt`

Since you have a **5V fan** (active cooling), the 1.2GHz overclock with `over_voltage=2` is safe. The fan will keep temps well below throttle thresholds during sustained inference.

**Critical fix**: The existing `pi_optimize.sh` sets `gpu_mem=16`. This is **too low** for the camera — the V4L2/libcamera ISP needs at least 64MB. We must set `gpu_mem=64`.

```ini
# Overclock — safe with 5V fan active cooling
arm_freq=1200
core_freq=500
over_voltage=2

# GPU memory — camera ISP needs minimum 64MB
# Do NOT set lower than 64 or camera capture becomes unreliable
gpu_mem=64

# Thermal safety — with fan, 80C soft limit is conservative
temp_soft_limit=80
force_turbo=0
```

**Why these values with a fan**:
- `arm_freq=1200`: 20% overclock, stable with active cooling
- `over_voltage=2`: +50mV, required for stable 1.2GHz operation
- `core_freq=500`: Helps V4L2 camera pipeline throughput
- `gpu_mem=64`: Minimum for reliable camera, frees remaining RAM for CPU
- With a 5V fan, sustained temps should stay around **50-65C** under full load (vs 80C+ without cooling)

**NOT pushing higher** (e.g., 1.3GHz / over_voltage=4):
- Diminishing returns — extra 100MHz adds ~8% compute but significantly more heat/power draw
- Pi Zero 2W power regulator can become a bottleneck above 1.2GHz
- 1.2GHz with stable operation is better than 1.3GHz with occasional crashes

**Reboot and verify**:
```bash
sudo reboot
# After reboot:
vcgencmd measure_clock arm    # Should show ~1200000000
vcgencmd measure_clock core   # Should show ~500000000
vcgencmd measure_temp         # Should be 35-45C idle with fan
```

---

### Step 4: Swap & Memory Tuning
**Where**: Pi (SSH)

**Option A — ZRAM (recommended, already in pi_optimize.sh)**:
Uses compressed RAM swap with LZ4. Faster than SD card swap. Effective ~512MB from 256MB physical allocation due to ~2:1 compression.

```bash
# Run the optimization script (we'll fix gpu_mem first)
sudo bash setup/pi_optimize.sh
```

**Option B — SD card swap (simpler fallback)**:
If ZRAM causes issues, use traditional 512MB swap:
```bash
# Edit /etc/dphys-swapfile
CONF_SWAPSIZE=512
CONF_MAXSWAP=512
sudo systemctl restart dphys-swapfile
```

**Kernel tuning** (already in `pi_optimize.sh`):
- `vm.swappiness=100` when using ZRAM (swap aggressively to fast compressed RAM)
- `vm.swappiness=10` when using SD card swap (avoid slow SD I/O)
- `vm.dirty_background_ratio=1` — flush dirty pages early to prevent I/O stalls

---

### Step 5: Disable Unnecessary Services
**Where**: Pi (SSH)

Already handled by `pi_optimize.sh`, but verify these are disabled:

```bash
# Verify disabled
for svc in bluetooth hciuart triggerhappy; do
    systemctl is-enabled $svc 2>/dev/null && echo "$svc: STILL ENABLED" || echo "$svc: disabled (good)"
done

# Additionally disable if present:
sudo systemctl disable --now man-db.timer 2>/dev/null
sudo systemctl disable --now apt-daily.timer 2>/dev/null
sudo systemctl disable --now apt-daily-upgrade.timer 2>/dev/null
```

**CPU governor** (already in `pi_optimize.sh`):
```bash
# Verify performance governor is active
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# Should show: performance
```

---

### Step 6: Deploy Code from Main Branch
**Where**: Pi (SSH)

```bash
# Clone the repository
cd /home/pi
git clone <your-repo-url> facial-tracker-firmware
cd facial-tracker-firmware
git checkout main

# Create directory structure expected by services
mkdir -p /home/pi/facial-tracker-firmware/data
mkdir -p /home/pi/facial-tracker-firmware/images/pending
mkdir -p /home/pi/facial-tracker-firmware/images/events
mkdir -p /home/pi/facial-tracker-firmware/logs

# Create the 'current/apps' symlink structure used by systemd services
# If using OTA bundle system:
mkdir -p /home/pi/facial-tracker-firmware/current
ln -sf /home/pi/facial-tracker-firmware /home/pi/facial-tracker-firmware/current/apps
ln -sf /home/pi/facial-tracker-firmware /home/pi/facial-tracker-firmware/current/libs
```

---

### Step 7: Python Virtual Environment & Dependencies
**Where**: Pi (SSH)

```bash
cd /home/pi/facial-tracker-firmware
python3 -m venv venv --system-site-packages
source venv/bin/activate

pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt

# Verify key packages
python3 -c "import mediapipe; print('MediaPipe:', mediapipe.__version__)"
python3 -c "import cv2; print('OpenCV:', cv2.__version__)"
python3 -c "import numpy; print('NumPy:', numpy.__version__)"
```

**Note**: The current `requirements.txt` includes `pygame` and `firebase_admin` which are unused. They waste install time and disk space but won't hurt runtime. We can clean them up later.

---

### Step 8: Database Setup (MySQL for Now)
**Where**: Pi (SSH)

The current code uses MySQL. For the initial deployment and performance testing, we'll use MySQL as-is. The SQLite migration (Phase 4 of the guide) can be done after confirming baseline performance.

```bash
# Install MariaDB
sudo apt install -y mariadb-server

# Secure and configure
sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'raspberry@123';"
sudo mysql -e "FLUSH PRIVILEGES;"

# Initialize the database schema
mysql -u root -praspberry@123 < /home/pi/facial-tracker-firmware/init_database.sql

# Verify
mysql -u root -praspberry@123 -e "USE car; SHOW TABLES;"
```

---

### Step 9: Redis Setup (for GPS IPC)
**Where**: Pi (SSH)

The current code uses Redis for real-time GPS data sharing between services.

```bash
sudo apt install -y redis-server

# Start Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Verify
redis-cli ping  # Should return: PONG
```

---

### Step 10: Configure UART for GPS (if applicable)
**Where**: Pi (SSH)

```bash
sudo raspi-config
# → Interface Options → Serial Port
# → Login shell over serial: NO
# → Serial port hardware: YES

# If using gpsd:
sudo systemctl enable gpsd
sudo systemctl start gpsd

# Verify GPS device
ls -la /dev/ttyAMA0 /dev/serial0
```

---

### Step 11: Install & Enable Systemd Services
**Where**: Pi (SSH)

```bash
# Copy service files
sudo cp /home/pi/facial-tracker-firmware/systemd/facial1.service /etc/systemd/system/
sudo cp /home/pi/facial-tracker-firmware/systemd/get_gps_data1.service /etc/systemd/system/
sudo cp /home/pi/facial-tracker-firmware/systemd/send-data-api.service /etc/systemd/system/
sudo cp /home/pi/facial-tracker-firmware/systemd/upload_images.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable services (they will start on boot)
sudo systemctl enable facial1.service
sudo systemctl enable get_gps_data1.service
sudo systemctl enable send-data-api.service
sudo systemctl enable upload_images.service

# Start facial tracking first (the critical service)
sudo systemctl start facial1.service

# Check it's running
sudo systemctl status facial1.service
journalctl -u facial1.service -f --no-pager -n 50
```

**Start order**:
1. `facial1.service` — start first, verify it runs
2. `get_gps_data1.service` — start second
3. `send-data-api.service` — start third
4. `upload_images.service` — start last

---

### Step 12: Performance Testing & Monitoring
**Where**: Pi (SSH)

This is the key step — measuring actual facial tracking performance.

#### 12a. Thermal Monitoring (continuous)
```bash
# Terminal 1 — monitor temps
watch -n 2 'vcgencmd measure_temp && vcgencmd get_throttled'
```

**Expected with 5V fan**:
- Idle: 35-45C
- Under facial tracking load: 50-65C
- `get_throttled` should always show `0x0` (no throttling)

#### 12b. RAM Usage
```bash
free -h
# Expected with MySQL + Redis running:
#   Total: ~430MB (64MB reserved for GPU)
#   Used:  ~300-350MB
#   Free:  ~80-130MB
#   Swap:  256MB ZRAM (should show low usage)

# Per-process breakdown
ps aux --sort=-%mem | head -15
```

#### 12c. Facial Tracking FPS
```bash
# Watch detection logs
journalctl -u facial1.service -f | grep -i "performance\|fps\|frame\|latency"

# Expected targets:
#   - Average inference: <100ms/frame at 1.2GHz
#   - Effective FPS: 10+ (target is 10)
#   - Detection confidence: stable at 0.5 threshold
```

#### 12d. CPU Load Distribution
```bash
htop
# Expected:
#   - facil_event_capture.py: 60-80% across cores (this is normal)
#   - get_gps_data.py: 2-5%
#   - send_last_second_data.py: <1% (wakes every 30s)
#   - upload_images.py: <1% (I/O bound, idle class)
#   - mysqld: 1-3%
#   - redis-server: <1%
```

#### 12e. Stress Test Facial Tracking
```bash
# Let it run for 10+ minutes with camera pointing at a face
# Monitor for:
# 1. Memory leaks (RSS growing over time)
# 2. Thermal throttling (vcgencmd get_throttled != 0x0)
# 3. FPS drops below target
# 4. OOM kills (dmesg | grep -i oom)

# Full monitoring command
watch -n 5 'echo "=== TEMP ===" && vcgencmd measure_temp && \
echo "=== THROTTLE ===" && vcgencmd get_throttled && \
echo "=== RAM ===" && free -m | head -3 && \
echo "=== TOP PROCS ===" && ps aux --sort=-%mem | head -8'
```

---

## Post-Baseline Optimization Path

Once the baseline is working and you have performance numbers, proceed with these optimizations **one at a time**, measuring impact after each:

| Priority | Optimization | Expected Impact | Risk |
|----------|-------------|-----------------|------|
| 1 | Fix `gpu_mem=64` in pi_optimize.sh | Reliable camera capture | Low |
| 2 | Clean requirements.txt (remove pygame, firebase) | Faster installs, less disk | None |
| 3 | SQLite migration (replace MySQL) | Free ~50-80MB RAM | Medium |
| 4 | Shared memory GPS IPC (replace Redis) | Free ~10-15MB RAM | Medium |
| 5 | Camera MJPEG mode | Less CPU for frame decode | Low |
| 6 | LED controller rewrite (non-blocking) | Fewer threads | Low |
| 7 | Lazy imports | Faster startup, less peak RAM | Low |

---

## Quick Reference: Key Paths on the Pi

| Path | Purpose |
|------|---------|
| `/home/pi/facial-tracker-firmware/` | Main code directory |
| `/home/pi/facial-tracker-firmware/venv/` | Python virtual environment |
| `/home/pi/facial-tracker-firmware/data/` | SQLite DB (future) |
| `/home/pi/facial-tracker-firmware/images/` | Event captures |
| `/home/pi/facial-tracker-firmware/current/apps` | Symlink used by services |
| `/boot/firmware/config.txt` | Boot config (overclock, GPU mem) |
| `/etc/systemd/system/` | Service files |
| `/dev/shm/` | Shared memory (future GPS IPC) |

## Quick Reference: Key Commands

```bash
# Service management
sudo systemctl start|stop|restart|status facial1.service
journalctl -u facial1.service -f

# Temperature / throttle
vcgencmd measure_temp
vcgencmd get_throttled    # 0x0 = good

# RAM
free -h

# CPU frequency
vcgencmd measure_clock arm

# All services status
for s in facial1 get_gps_data1 send-data-api upload_images; do
    echo -n "$s: "; systemctl is-active $s
done
```
