# Blinksmart DMS — Pi Zero 2W Deployment Plan

## Hardware Assumptions
- **Board**: Raspberry Pi Zero 2W (BCM2710A1, 512MB RAM)
- **OS**: **64-bit** Raspberry Pi OS Lite (Bookworm) — `aarch64`
- **Cooling**: 5V fan (active cooling)
- **Camera**: CSI camera module (OV5647 or similar)
- **GPS**: UART serial GPS module (no gpsd — direct serial read via pyserial)
- **Storage**: SD card (A1 or A2 rated recommended)

> **Why 64-bit?** MediaPipe has no pre-built wheels for 32-bit ARM (armv7l).
> Only aarch64 is supported. 64-bit also gives 10-30% faster ML inference
> due to better NEON optimization. The ~20-30MB extra RAM overhead is
> offset by the performance gain and is manageable with ZRAM swap.

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

1. Download **Raspberry Pi OS Lite (64-bit, Bookworm)** — `raspios-bookworm-arm64-lite`
   - Must be **64-bit** (mediapipe requires aarch64, no armv7l wheels exist)
   - Must be **Lite** edition (no Desktop — saves ~200MB RAM)
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
uname -m             # Must show: aarch64
cat /etc/os-release  # Should show Bookworm
free -h              # ~400-440MB total RAM
```

---

## Step 2: System Update & Essential Packages

```bash
sudo apt update && sudo apt full-upgrade -y

sudo apt install -y git htop iotop vim python3-pip python3-venv \
    libatlas-base-dev libopenblas-dev libjpeg-dev libpng-dev \
    libhdf5-dev libharfbuzz-dev libwebp-dev libtiff-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    v4l-utils sqlite3 stress-ng
```

**NOT installing**:
- `mariadb-server`, `redis-server` — replaced by SQLite + shared memory
- `gpsd`, `gpsd-clients` — GPS is read directly via serial (pyserial)

---

## Step 3: Clone Code & Run Optimization

```bash
cd /home/pi
mkdir -p facial-tracker-firmware
cd facial-tracker-firmware
git clone <your-repo-url> .
git checkout main

# Create directories
mkdir -p data images/pending images/events logs

# Create OTA-safe symlinks for systemd service paths
ln -sfn /home/pi/facial-tracker-firmware current
```

Then run the optimization script (handles overclock, ZRAM, CPU governor, tmpfs, sysctl, services):

```bash
sudo bash setup/pi_optimize.sh
sudo reboot
```

This does:
- Overclock: `arm_freq=1200`, `over_voltage=2`, `core_freq=500`
- GPU memory: `gpu_mem=64` (minimum for camera ISP)
- ZRAM swap: 256MB LZ4 compressed (~512MB effective)
- CPU governor: `performance` (no clock ramping delay)
- tmpfs: `/tmp` (64MB) + `/var/log` (16MB) in RAM
- `vm.swappiness=100` (prefer fast ZRAM)
- Disables: bluetooth, hciuart, triggerhappy, apt timers

**Verify after reboot**:
```bash
vcgencmd measure_clock arm    # ~1200000000
vcgencmd measure_temp         # 35-45C idle with fan
vcgencmd get_throttled        # 0x0 (no throttling)
free -m                       # Check ZRAM in swap
```

---

## Step 4: Initialize SQLite Database

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

## Step 5: Python Virtual Environment & Dependencies

```bash
cd /home/pi/facial-tracker-firmware
python3 -m venv venv --system-site-packages
source venv/bin/activate

pip install --upgrade pip

# /tmp is on tmpfs (64MB) — large packages like opencv need disk space.
# Temporarily unmount /tmp for the pip install, then remount after.
sudo umount /tmp
TMPDIR=/home/pi/.pip-tmp pip install -r requirements.txt \
    --cache-dir /home/pi/.pip-cache
sudo mount -a   # Remount /tmp tmpfs

# Clean up pip temp/cache
rm -rf /home/pi/.pip-tmp /home/pi/.pip-cache

# Verify:
python3 -c "import mediapipe; print('MediaPipe:', mediapipe.__version__)"
python3 -c "import cv2; print('OpenCV:', cv2.__version__)"
python3 -c "import db_helper; print('db_helper: OK')"
```

> **Note**: `pip install mediapipe` works on aarch64 (64-bit OS). It does NOT
> work on armv7l (32-bit). This is why Step 1 requires 64-bit Bookworm Lite.

---

## Step 6: Configure UART for Serial GPS

```bash
sudo raspi-config
# -> Interface Options -> Serial Port
# -> Login shell over serial: NO
# -> Serial port hardware: YES

sudo reboot
```

**Verify** (after connecting GPS module to UART pins):
```bash
# Check that serial port exists
ls -la /dev/serial0   # Should link to /dev/ttyS0 or /dev/ttyAMA0

# Test raw NMEA output from GPS
cat /dev/serial0      # Should show $GPRMC, $GPGGA sentences
# Press Ctrl+C to exit
```

> **No gpsd needed.** The `get_gps_data.py` script reads NMEA sentences
> directly from the serial port using pyserial + pynmea2.

---

## Step 7: Device Provisioning

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

## Step 8: Install Systemd Services

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

## Step 9: Verify All Services Running

```bash
# Quick status check
for s in facial1 get_gps_data1 send-data-api upload_images; do
    echo -n "$s: "; systemctl is-active $s
done

# Check facial tracking logs
journalctl -u facial1 -f --no-pager -n 50
```

---

## Step 10: Performance Testing

### 10a — Thermal (continuous in background)
```bash
watch -n 2 'vcgencmd measure_temp && vcgencmd get_throttled'
# With fan: 50-65C under load, get_throttled = 0x0
```

### 10b — RAM Usage
```bash
free -h
# Expected (64-bit, no MySQL, no Redis):
#   Total: ~400-440MB
#   Used:  ~220-280MB
#   Free:  ~120-180MB
#   Swap:  256MB ZRAM (near zero usage)

ps aux --sort=-%mem | head -10
```

### 10c — Facial Tracking FPS
```bash
journalctl -u facial1 -f | grep -i "performance\|fps\|frame"
# Target: <100ms/frame, 10+ FPS
```

### 10d — CPU Distribution
```bash
htop
# facil_event_capture.py: 60-80%
# get_gps_data.py: 2-5%
# Everything else: <1%
```

### 10e — Sustained Stress Test
```bash
# Let facial tracking run 10+ minutes, then check:
dmesg | grep -i oom         # No OOM kills
vcgencmd get_throttled      # Still 0x0
free -h                     # Stable RAM usage
```

### 10f — Full monitoring dashboard
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
| OS (64-bit Lite, Bookworm) | ~55-65MB |
| GPU reserved | 64MB |
| **facial1.service** (MediaPipe + OpenCV) | ~170-220MB |
| get_gps_data1.service | ~15-25MB |
| upload_images.service | ~15-25MB |
| send-data-api.service | ~15-25MB |
| SQLite (embedded, no daemon) | ~2MB |
| Shared memory GPS IPC | <1MB |
| **Total Used** | **~340-400MB** |
| **Free RAM** | **~40-100MB** |
| **Swap Available** | **~512MB (ZRAM)** |

> 64-bit uses ~20-30MB more RAM than 32-bit, but gives 10-30% faster
> ML inference. ZRAM swap provides ample overflow capacity.

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
