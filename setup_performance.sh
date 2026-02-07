#!/bin/bash
# =============================================================================
# Raspberry Pi Zero 2W Performance Optimization Script
# For: Sapience Driver Monitoring System (facial tracking + GPS)
#
# Run as root: sudo bash setup_performance.sh
# Reboot required after running.
# =============================================================================

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root. Use: sudo bash setup_performance.sh"
    exit 1
fi

echo "============================================="
echo " Pi Zero 2W Performance Optimization"
echo "============================================="

# -----------------------------------------------
# 1. INCREASE SWAP SIZE (100MB -> 1024MB)
# -----------------------------------------------
echo ""
echo "[1/8] Configuring swap (1024MB)..."

if [ -f /etc/dphys-swapfile ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
    # Ensure swap file is on a fast location
    sed -i 's/^#\?CONF_SWAPFACTOR=.*/CONF_SWAPFACTOR=2/' /etc/dphys-swapfile
    echo "   Swap configured: 1024MB (was likely 100MB)"
    echo "   Will activate on reboot"
else
    echo "   dphys-swapfile not found, creating manual swap..."
    fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024
    chmod 600 /swapfile
    mkswap /swapfile
    if ! grep -q '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "   Manual 1GB swap created"
fi

# -----------------------------------------------
# 2. SETUP ZRAM (compressed RAM swap)
# -----------------------------------------------
echo ""
echo "[2/8] Setting up zram (compressed RAM swap)..."

cat > /etc/systemd/system/zram-swap.service << 'ZRAMEOF'
[Unit]
Description=Configure zram swap device
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c '\
    modprobe zram num_devices=1 && \
    echo lz4 > /sys/block/zram0/comp_algorithm 2>/dev/null || echo lzo > /sys/block/zram0/comp_algorithm && \
    echo 256M > /sys/block/zram0/disksize && \
    mkswap /dev/zram0 && \
    swapon -p 100 /dev/zram0'
ExecStop=/bin/bash -c 'swapoff /dev/zram0 && echo 1 > /sys/block/zram0/reset'

[Install]
WantedBy=multi-user.target
ZRAMEOF

systemctl daemon-reload
systemctl enable zram-swap.service
echo "   zram-swap.service enabled (256MB compressed, priority 100)"
echo "   Compressed RAM swap is 10-50x faster than SD card swap"

# -----------------------------------------------
# 3. OVERCLOCK CPU (1GHz -> 1.2GHz, safe)
# -----------------------------------------------
echo ""
echo "[3/8] Configuring overclock (1.2GHz, safe)..."

CONFIG_FILE="/boot/config.txt"
if [ -f /boot/firmware/config.txt ]; then
    CONFIG_FILE="/boot/firmware/config.txt"
fi

# Remove existing overclock settings to avoid duplicates
sed -i '/^arm_freq=/d' "$CONFIG_FILE"
sed -i '/^over_voltage=/d' "$CONFIG_FILE"
sed -i '/^gpu_mem=/d' "$CONFIG_FILE"
sed -i '/^dtparam=audio=/d' "$CONFIG_FILE"
sed -i '/^# Sapience performance/d' "$CONFIG_FILE"
sed -i '/^gpu_freq=/d' "$CONFIG_FILE"
sed -i '/^core_freq=/d' "$CONFIG_FILE"
sed -i '/^disable_splash=/d' "$CONFIG_FILE"
sed -i '/^boot_delay=/d' "$CONFIG_FILE"
sed -i '/^force_turbo=/d' "$CONFIG_FILE"

cat >> "$CONFIG_FILE" << 'OCEOF'

# Sapience performance optimizations for Pi Zero 2W
arm_freq=1200
over_voltage=4
gpu_freq=400
core_freq=400

# Reduce GPU memory (headless - no display needed)
gpu_mem=16

# Disable audio hardware (not used, saves resources)
dtparam=audio=off

# Faster boot
disable_splash=1
boot_delay=0
OCEOF

echo "   CPU: 1GHz -> 1.2GHz (+20%)"
echo "   Voltage: over_voltage=4 (safe for 1.2GHz)"
echo "   GPU memory: 64MB -> 16MB (freed 48MB for applications)"
echo "   Audio: disabled (not used)"

# -----------------------------------------------
# 4. CPU GOVERNOR -> performance (fixed max frequency)
# -----------------------------------------------
echo ""
echo "[4/8] Setting CPU governor to 'performance'..."

cat > /etc/systemd/system/cpu-performance.service << 'CPUEOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$cpu" 2>/dev/null; done'

[Install]
WantedBy=multi-user.target
CPUEOF

systemctl daemon-reload
systemctl enable cpu-performance.service
echo "   CPU governor set to 'performance' (no frequency scaling)"
echo "   Eliminates frame time jitter from CPU speed changes"

# -----------------------------------------------
# 5. KERNEL / SYSCTL TUNING
# -----------------------------------------------
echo ""
echo "[5/8] Tuning kernel parameters..."

cat > /etc/sysctl.d/99-sapience-performance.conf << 'SYSEOF'
# Reduce swappiness (prefer keeping app data in RAM over file cache)
vm.swappiness=30

# Allow overcommit to prevent OOM during brief memory spikes
vm.overcommit_memory=1

# Reduce dirty page writeback frequency (less SD card I/O)
vm.dirty_ratio=40
vm.dirty_background_ratio=10
vm.dirty_expire_centisecs=3000
vm.dirty_writeback_centisecs=1500

# Network buffer optimization (for MQTT/API uploads)
net.core.rmem_max=1048576
net.core.wmem_max=1048576
SYSEOF

echo "   vm.swappiness=30 (was 60)"
echo "   vm.overcommit_memory=1 (prevent OOM kills)"
echo "   Dirty page writeback optimized for SD card"

# -----------------------------------------------
# 6. DISABLE UNNECESSARY SERVICES
# -----------------------------------------------
echo ""
echo "[6/8] Disabling unnecessary services..."

SERVICES_TO_DISABLE=(
    "bluetooth.service"
    "hciuart.service"
    "triggerhappy.service"
    "avahi-daemon.service"
    "ModemManager.service"
    "wpa_supplicant@.service"
    "apt-daily.service"
    "apt-daily-upgrade.service"
    "apt-daily.timer"
    "apt-daily-upgrade.timer"
    "man-db.timer"
)

for svc in "${SERVICES_TO_DISABLE[@]}"; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        systemctl disable "$svc" 2>/dev/null && echo "   Disabled: $svc" || true
    fi
done

# Disable Bluetooth in config.txt
if ! grep -q 'dtoverlay=disable-bt' "$CONFIG_FILE"; then
    echo 'dtoverlay=disable-bt' >> "$CONFIG_FILE"
    echo "   Bluetooth hardware disabled"
fi

# Disable HDMI (saves ~25mA power, frees resources)
if ! grep -q 'hdmi_blanking' "$CONFIG_FILE"; then
    echo 'hdmi_blanking=2' >> "$CONFIG_FILE"
fi

echo "   Bluetooth, HDMI, apt timers, avahi disabled"

# -----------------------------------------------
# 7. TMPFS FOR TEMPORARY FILES
# -----------------------------------------------
echo ""
echo "[7/8] Setting up tmpfs (RAM-based temp storage)..."

# Add tmpfs mounts if not already present
if ! grep -q '/tmp tmpfs' /etc/fstab; then
    echo 'tmpfs /tmp tmpfs defaults,noatime,nosuid,size=64m 0 0' >> /etc/fstab
    echo "   /tmp mounted as tmpfs (64MB)"
fi
if ! grep -q '/var/log tmpfs' /etc/fstab; then
    echo 'tmpfs /var/log tmpfs defaults,noatime,nosuid,size=32m 0 0' >> /etc/fstab
    echo "   /var/log mounted as tmpfs (32MB)"
fi

echo "   Temp files and logs now use RAM (no SD card I/O)"
echo "   WARNING: Logs will be lost on reboot. Use journalctl for persistent logs."

# -----------------------------------------------
# 8. WIFI POWER MANAGEMENT OFF
# -----------------------------------------------
echo ""
echo "[8/8] Disabling WiFi power management..."

cat > /etc/NetworkManager/conf.d/no-wifi-powersave.conf 2>/dev/null << 'WIFIEOF' || true
[connection]
wifi.powersave = 2
WIFIEOF

# Also via systemd
cat > /etc/systemd/system/wifi-powersave-off.service << 'WPEOF'
[Unit]
Description=Disable WiFi Power Save
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/iw dev wlan0 set power_save off

[Install]
WantedBy=multi-user.target
WPEOF

systemctl daemon-reload
systemctl enable wifi-powersave-off.service
echo "   WiFi power management disabled (prevents latency spikes)"

# -----------------------------------------------
# SUMMARY
# -----------------------------------------------
echo ""
echo "============================================="
echo " OPTIMIZATION SUMMARY"
echo "============================================="
echo ""
echo " MEMORY:"
echo "   - Swap: 100MB -> 1024MB (10x more breathing room)"
echo "   - zram: 256MB compressed RAM swap (10-50x faster than SD)"
echo "   - GPU mem: 64MB -> 16MB (freed 48MB for apps)"
echo "   - tmpfs: /tmp and /var/log in RAM"
echo ""
echo " CPU:"
echo "   - Overclock: 1.0GHz -> 1.2GHz (+20%)"
echo "   - Governor: ondemand -> performance (no throttling)"
echo "   - over_voltage=4 (safe for sustained 1.2GHz)"
echo ""
echo " I/O:"
echo "   - Reduced dirty page writeback (less SD card thrashing)"
echo "   - tmpfs for temp files (no SD I/O for logs)"
echo ""
echo " DISABLED:"
echo "   - Bluetooth, HDMI, audio hardware"
echo "   - apt auto-updates, avahi, triggerhappy"
echo "   - WiFi power management"
echo ""
echo " *** REBOOT REQUIRED to apply all changes ***"
echo "   Run: sudo reboot"
echo ""
echo "============================================="
echo " After reboot, verify with:"
echo "   cat /proc/cpuinfo | grep MHz"
echo "   free -h"
echo "   swapon --show"
echo "   cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
echo "============================================="
