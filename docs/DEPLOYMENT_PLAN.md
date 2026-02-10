# Blinksmart DMS — Pi Zero 2W Deployment Plan

## Hardware Assumptions
- **Board**: Raspberry Pi Zero 2W (BCM2710A1, 512MB RAM)
- **OS**: 32-bit Raspberry Pi OS Lite (Bookworm) — `armv7l`
- **Cooling**: 5V fan (active cooling)
- **Camera**: CSI camera module (OV5647 or similar)
- **GPS**: UART GPS module via gpsd
- **Storage**: SD card (A1 or A2 rated recommended)

---

## RAM Savings Built Into This Deployment

| Change | RAM Saved | Accuracy Risk |
|--------|-----------|---------------|
| MySQL -> SQLite | ~50-80MB | None |
| Remove firebase_admin | ~40-60MB | None |
| Redis -> shared memory | ~10MB | None |
| Lazy imports | ~10-15MB | None |
| Consolidate threads | ~2-3MB | None |
| systemd MemoryMax tuning | ~30-50MB freed for facial1 | None |
| **Total** | **~140-210MB** | **Zero** |

---

## Step 1: Flash OS & First Boot
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
4. Insert SD card, power on, SSH in:
   ```bash
   ssh pi@blinksmart.local
   ```

**Verify**:
```bash
uname -m             # Must show: armv7l
cat /etc/os-release  # Should show Bookworm
free -h              # ~430-460MB total RAM
```

---

## Step 2: System Update & Essential Packages

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y git htop iotop vim python3-pip python3-venv \
    libatlas-base-dev libopenblas-dev libjpeg-dev libpng-dev \
    libhdf5-dev libharfbuzz-dev libwebp-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    v4l-utils gpsd gpsd-clients sqlite3 stress-ng
```

**NOT installing**: `mariadb-server`, `redis-server` — replaced by SQLite + shared memory.

---

## Step 3: Overclock & Boot Config

Edit `/boot/firmware/config.txt` (or run `setup/pi_optimize.sh`):

```ini
# Overclock — safe with 5V fan
arm_freq=1200
core_freq=500
over_voltage=2

# GPU — camera ISP minimum is 64MB
gpu_mem=64

# Thermal safety
temp_soft_limit=80
force_turbo=0
```

**With 5V fan**: Sustained temps should stay 50-65C under full MediaPipe load. `over_voltage=2` (+50mV) is safe.

```bash
sudo reboot
# Verify:
vcgencmd measure_clock arm    # ~1200000000
vcgencmd measure_temp         # 35-45C idle
```

---

## Step 4: ZRAM Swap + Kernel Tuning

Run the optimization script which handles ZRAM, CPU governor, tmpfs, sysctl tuning, and disabling unnecessary services:

```bash
sudo bash setup/pi_optimize.sh
sudo reboot
```

This does:
- ZRAM swap: 256MB LZ4 compressed (~512MB effective)
- CPU governor: `performance` (no clock ramping delay)
- tmpfs: `/tmp` (32MB) + `/var/log` (16MB) in RAM
- `vm.swappiness=100` (prefer fast ZRAM)
- Disables: bluetooth, hciuart, triggerhappy

Additionally disable timer services:
```bash
sudo systemctl disable --now man-db.timer 2>/dev/null
sudo systemctl disable --now apt-daily.timer 2>/dev/null
sudo systemctl disable --now apt-daily-upgrade.timer 2>/dev/null
```

---

## Step 5: Deploy Code

```bash
cd /home/pi
git clone <your-repo-url> facial-tracker-firmware
cd facial-tracker-firmware
git checkout main

# Create directories
mkdir -p data images/pending images/events logs

# Create symlinks for systemd service paths
mkdir -p current
ln -sf /home/pi/facial-tracker-firmware current/apps
ln -sf /home/pi/facial-tracker-firmware current/libs
```

---

## Step 6: Initialize SQLite Database

```bash
cd /home/pi/facial-tracker-firmware
python3 init_sqlite.py
# Should print: "SQLite database initialized: /home/pi/facial-tracker-firmware/data/blinksmart.db"

# Verify:
sqlite3 data/blinksmart.db ".tables"
# Should show: car_data  configure  count_table  device  gps_data  user_info
```

**If migrating from an existing device with MySQL data**:
```bash
# Install mysql-connector temporarily for migration
pip3 install mysql-connector-python
python3 migrate_mysql_to_sqlite.py
# Then uninstall: pip3 uninstall mysql-connector-python
```

---

## Step 7: Python Virtual Environment & Dependencies

```bash
cd /home/pi/facial-tracker-firmware
python3 -m venv venv --system-site-packages
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Verify:
python3 -c "import mediapipe; print('MediaPipe:', mediapipe.__version__)"
python3 -c "import cv2; print('OpenCV:', cv2.__version__)"
python3 -c "import db_helper; print('db_helper: OK')"
```

---

## Step 8: Configure UART for GPS

```bash
sudo raspi-config
# -> Interface Options -> Serial Port
# -> Login shell over serial: NO
# -> Serial port hardware: YES

sudo systemctl enable gpsd
sudo systemctl start gpsd
```

---

## Step 9: Device Provisioning

```bash
cd /home/pi/facial-tracker-firmware

# Create .env with your provisioning token
cp .env.example .env
nano .env  # Set PROVISIONING_TOKEN

# Run provisioning
source venv/bin/activate
python3 device_provisioning.py

# Verify:
python3 device_provisioning.py --check
# Should print: PROVISIONED: <device_id>
```

---

## Step 10: Install Systemd Services

```bash
sudo cp systemd/facial1.service /etc/systemd/system/
sudo cp systemd/get_gps_data1.service /etc/systemd/system/
sudo cp systemd/send-data-api.service /etc/systemd/system/
sudo cp systemd/upload_images.service /etc/systemd/system/

sudo systemctl daemon-reload

# Enable all
sudo systemctl enable facial1 get_gps_data1 send-data-api upload_images

# Start in priority order
sudo systemctl start facial1
sleep 3
sudo systemctl start get_gps_data1
sleep 2
sudo systemctl start send-data-api upload_images
```

---

## Step 11: Verify All Services Running

```bash
# Quick status check
for s in facial1 get_gps_data1 send-data-api upload_images; do
    echo -n "$s: "; systemctl is-active $s
done

# Check facial tracking logs
journalctl -u facial1 -f --no-pager -n 50
```

---

## Step 12: Performance Testing

### 12a — Thermal (continuous in background)
```bash
watch -n 2 'vcgencmd measure_temp && vcgencmd get_throttled'
# With fan: 50-65C under load, get_throttled = 0x0
```

### 12b — RAM Usage
```bash
free -h
# Expected (no MySQL, no Redis):
#   Total: ~430MB
#   Used:  ~200-250MB
#   Free:  ~180-230MB
#   Swap:  256MB ZRAM (near zero usage)

ps aux --sort=-%mem | head -10
```

### 12c — Facial Tracking FPS
```bash
journalctl -u facial1 -f | grep -i "performance\|fps\|frame"
# Target: <100ms/frame, 10+ FPS
```

### 12d — CPU Distribution
```bash
htop
# facil_event_capture.py: 60-80%
# get_gps_data.py: 2-5%
# Everything else: <1%
```

### 12e — Sustained Stress Test
```bash
# Let facial tracking run 10+ minutes, then check:
dmesg | grep -i oom         # No OOM kills
vcgencmd get_throttled      # Still 0x0
free -h                     # Stable RAM usage
```

### 12f — Full monitoring dashboard
```bash
bash monitor.sh
# Or:
watch -n 5 'echo "=== TEMP ===" && vcgencmd measure_temp && \
echo "=== THROTTLE ===" && vcgencmd get_throttled && \
echo "=== RAM ===" && free -m | head -3 && \
echo "=== TOP PROCS ===" && ps aux --sort=-%mem | head -8'
```

---

## Expected RAM Budget (Post-Optimization)

| Component | RAM Usage |
|-----------|-----------|
| OS (32-bit Lite, Bookworm) | ~45MB |
| GPU reserved | 64MB |
| **facial1.service** (MediaPipe + OpenCV) | ~160-200MB |
| get_gps_data1.service | ~15-20MB |
| upload_images.service | ~15-20MB |
| send-data-api.service | ~15-20MB |
| SQLite (embedded, no daemon) | ~2MB |
| Shared memory GPS IPC | <1MB |
| **Total Used** | **~320-370MB** |
| **Free RAM** | **~60-110MB** |
| **Swap Available** | **~512MB (ZRAM)** |

---

## Quick Reference

```bash
# Service management
sudo systemctl start|stop|restart|status facial1
journalctl -u facial1 -f

# Temperature / throttle
vcgencmd measure_temp
vcgencmd get_throttled    # 0x0 = good

# Database
sqlite3 /home/pi/facial-tracker-firmware/data/blinksmart.db ".tables"

# GPS shared memory check
ls -la /dev/shm/blinksmart_gps

# All services status
for s in facial1 get_gps_data1 send-data-api upload_images; do
    echo -n "$s: "; systemctl is-active $s
done
```
