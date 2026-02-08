#!/bin/bash
#
# Install all dependencies for Driver Monitoring System on fresh Pi Zero 2W
# Run: sudo bash setup/install_deps.sh
#
# Prerequisites:
#   - Bookworm Legacy Lite (32-bit armhf)
#   - pi_optimize.sh already run + rebooted
#

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run as root: sudo bash $0"
    exit 1
fi

FIRMWARE_DIR="/home/pi/facial-tracker-firmware"
SRC_DIR="$FIRMWARE_DIR/src"
VENV_DIR="$FIRMWARE_DIR/venv"

echo "=== Installing Dependencies for Driver Monitoring ==="
echo ""

# ─────────────────────────────────────────────
# 1. APT PACKAGES
# ─────────────────────────────────────────────
echo "[1/5] Installing system packages..."

apt-get update -qq

# Core: Python, camera, I/O
apt-get install -y --no-install-recommends \
    python3-venv python3-dev python3-pip \
    python3-numpy python3-opencv \
    python3-rpi.gpio python3-gpiozero \
    libcamera-apps-lite v4l-utils

# Services: Redis, MariaDB, GPSD, networking
apt-get install -y --no-install-recommends \
    redis-server mariadb-server \
    gpsd gpsd-clients \
    git hostapd dnsmasq

echo "  System packages installed."

# ─────────────────────────────────────────────
# 2. PYTHON VENV (with system-site-packages)
# ─────────────────────────────────────────────
echo ""
echo "[2/5] Setting up Python virtual environment..."

if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
    echo "  Created venv at $VENV_DIR"
else
    echo "  Venv already exists at $VENV_DIR"
fi

# Install pip packages not available via apt
"$VENV_DIR/bin/pip" install --no-cache-dir \
    tflite-runtime \
    redis \
    mysql-connector-python \
    requests \
    pyserial \
    pynmea2 \
    geopy \
    gps3 \
    paho-mqtt \
    msgpack \
    flask

echo "  Python packages installed."

# ─────────────────────────────────────────────
# 3. MARIADB DATABASE SETUP
# ─────────────────────────────────────────────
echo ""
echo "[3/5] Setting up MariaDB database..."

# Ensure MariaDB is running
systemctl start mariadb
systemctl enable mariadb

# Create database and set root password
mysql -u root -e "CREATE DATABASE IF NOT EXISTS car;" 2>/dev/null || \
mysql -u root -praspberry@123 -e "CREATE DATABASE IF NOT EXISTS car;" 2>/dev/null

# Set root password if not already set
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'raspberry@123';" 2>/dev/null || true

# Initialize schema if SQL file exists
if [ -f "$SRC_DIR/init_database.sql" ]; then
    mysql -u root -praspberry@123 car < "$SRC_DIR/init_database.sql" 2>/dev/null || true
    echo "  Database schema initialized."
fi

echo "  MariaDB configured (database: car)"

# ─────────────────────────────────────────────
# 4. REDIS CONFIGURATION
# ─────────────────────────────────────────────
echo ""
echo "[4/5] Configuring Redis..."

systemctl enable redis-server
systemctl start redis-server

echo "  Redis enabled and started."

# ─────────────────────────────────────────────
# 5. DIRECTORY STRUCTURE
# ─────────────────────────────────────────────
echo ""
echo "[5/5] Creating directory structure..."

# Models directory (symlinked for TFLite)
mkdir -p "$FIRMWARE_DIR/models"
[ ! -L "$SRC_DIR/models" ] && ln -sf "$FIRMWARE_DIR/models" "$SRC_DIR/models" || true

# Images directories
mkdir -p "$FIRMWARE_DIR/images/pending"
mkdir -p "$FIRMWARE_DIR/images/events"

# Set ownership
chown -R pi:pi "$FIRMWARE_DIR"

echo "  Directory structure created."

# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────
echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Download TFLite models to $FIRMWARE_DIR/models/"
echo "     - face_detection_short.tflite (BlazeFace)"
echo "     - face_landmark_192.tflite (468 landmarks)"
echo "  2. Provision the device:"
echo "     cd $SRC_DIR && $VENV_DIR/bin/python provisioning_ui.py"
echo "  3. Deploy services (one at a time, test performance between each):"
echo "     sudo cp $SRC_DIR/systemd/facial1.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload && sudo systemctl enable --now facial1"
echo "     journalctl -u facial1 -f"
echo ""
