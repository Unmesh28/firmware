#!/usr/bin/env python3
"""
Device Provisioning Script — SQLite version.

This script:
1. Checks if device is already provisioned (has device_id and auth_key)
2. If not, calls the create-device API to get credentials
3. Stores device_id and auth_key in local SQLite database
"""

import json
import logging
import os
import sys
import urllib.request
import urllib.error
import db_helper

# Load .env file if exists
def load_env_file():
    """Load environment variables from .env file"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_env_file()

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "device_provisioning.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("device_provisioning")

# Configuration
class Config:
    API_BASE_URL = os.getenv("API_BASE_URL", "https://api.copilotai.click")
    PROVISIONING_TOKEN = os.getenv("PROVISIONING_TOKEN", "")
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    DEVICE_TYPE = os.getenv("DEVICE_TYPE", "DM")


def ensure_device_table():
    """Ensure device table exists (handled by init_sqlite.py, kept for safety)"""
    try:
        db_helper.execute_commit("""
            CREATE TABLE IF NOT EXISTS device (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT UNIQUE NOT NULL,
                auth_key TEXT,
                device_type TEXT DEFAULT 'DM',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        return True
    except Exception as e:
        logger.error(f"Database error: {e}")
        return False


def is_device_provisioned():
    """Check if device already has device_id and auth_key"""
    try:
        row = db_helper.fetchone("SELECT device_id, auth_key FROM device LIMIT 1")
        if row and row['device_id'] and row['auth_key']:
            logger.info(f"Device already provisioned: {row['device_id']}")
            return True, row['device_id'], row['auth_key']
        return False, None, None
    except Exception as e:
        logger.error(f"Error checking provisioning status: {e}")
        return False, None, None


def call_create_device_api():
    """Call backend API to create device and get credentials"""
    try:
        url = f"{Config.API_BASE_URL}/api/auth/create-device"

        if Config.PROVISIONING_TOKEN:
            request_data = {
                "device_type": Config.DEVICE_TYPE,
                "provisioning_token": Config.PROVISIONING_TOKEN
            }
            logger.info("Using provisioning token for authentication")
        else:
            request_data = {
                "device_type": Config.DEVICE_TYPE,
                "admin_id": Config.ADMIN_EMAIL,
                "admin_pass": Config.ADMIN_PASSWORD
            }
            logger.info("Using admin credentials for authentication (legacy)")

        data = json.dumps(request_data).encode('utf-8')

        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'application/json')

        logger.info(f"Calling create-device API: {url}")

        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())

            if result.get("success"):
                device_data = result.get("data", {})
                device_id = device_data.get("device_id")
                auth_key = device_data.get("auth_key")

                if device_id and auth_key:
                    logger.info(f"Device created successfully: {device_id}")
                    return device_id, auth_key
                else:
                    logger.error("API response missing device_id or auth_key")
                    return None, None
            else:
                logger.error(f"API error: {result.get('message')}")
                return None, None

    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"HTTP error {e.code}: {e.reason} - {error_body}")
        return None, None
    except urllib.error.URLError as e:
        logger.error(f"URL error: {e.reason}")
        return None, None
    except Exception as e:
        logger.error(f"Error calling API: {e}")
        return None, None


def save_device_credentials(device_id, auth_key):
    """Save device credentials to SQLite database"""
    try:
        db_helper.execute_commit("DELETE FROM device")
        db_helper.execute_commit(
            "INSERT INTO device (device_id, auth_key, device_type) VALUES (?, ?, ?)",
            (device_id, auth_key, Config.DEVICE_TYPE))

        logger.info(f"Saved device credentials: device_id={device_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving credentials: {e}")
        return False


def provision_device():
    """Main provisioning function"""
    logger.info("=" * 50)
    logger.info("Device Provisioning - Starting")
    logger.info("=" * 50)

    if not ensure_device_table():
        logger.error("Failed to setup database")
        return False

    provisioned, device_id, auth_key = is_device_provisioned()
    if provisioned:
        logger.info("Device already provisioned, skipping")
        return True

    if not Config.PROVISIONING_TOKEN and not Config.ADMIN_PASSWORD:
        logger.error("No authentication configured")
        logger.error("Please set PROVISIONING_TOKEN (recommended) or ADMIN_EMAIL/ADMIN_PASSWORD")
        return False

    logger.info("Provisioning new device...")
    device_id, auth_key = call_create_device_api()

    if not device_id or not auth_key:
        logger.error("Failed to get device credentials from API")
        return False

    if not save_device_credentials(device_id, auth_key):
        logger.error("Failed to save device credentials")
        return False

    logger.info("=" * 50)
    logger.info("Device Provisioning - Complete")
    logger.info(f"Device ID: {device_id}")
    logger.info("=" * 50)

    return True


def get_device_credentials():
    """Get device credentials (for use by other scripts)"""
    try:
        row = db_helper.fetchone("SELECT device_id, auth_key FROM device LIMIT 1")
        if row:
            return {"device_id": row['device_id'], "auth_key": row['auth_key']}
        return None
    except Exception as e:
        logger.error(f"Error getting credentials: {e}")
        return None


def delete_device_credentials():
    """Delete device credentials and all data from database (full reset for re-provisioning)"""
    try:
        db_helper.execute_commit("DELETE FROM device")
        logger.info("Deleted device records")

        try:
            db_helper.execute_commit("DELETE FROM user_info")
            logger.info("Deleted user_info records")
        except Exception as e:
            logger.warning(f"Could not delete user_info records: {e}")

        try:
            db_helper.execute_commit("DELETE FROM gps_data")
            logger.info("Deleted gps_data records")
        except Exception as e:
            logger.warning(f"Could not delete gps_data records: {e}")

        try:
            db_helper.execute_commit("DELETE FROM car_data")
            logger.info("Deleted car_data records")
        except Exception as e:
            logger.warning(f"Could not delete car_data records: {e}")

        # Cleanup images folder
        import shutil
        images_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
        if os.path.exists(images_path):
            try:
                for item in os.listdir(images_path):
                    item_path = os.path.join(images_path, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)
                logger.info(f"Cleaned up images folder: {images_path}")
            except Exception as e:
                logger.warning(f"Could not cleanup images folder: {e}")

        logger.info("Device credentials and all data deleted successfully")
        return True
    except Exception as e:
        logger.error(f"Error deleting credentials: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--check":
            provisioned, device_id, auth_key = is_device_provisioned()
            if provisioned:
                print(f"PROVISIONED: {device_id}")
                sys.exit(0)
            else:
                print("NOT_PROVISIONED")
                sys.exit(1)
        elif sys.argv[1] == "--get":
            creds = get_device_credentials()
            if creds:
                print(json.dumps(creds))
                sys.exit(0)
            else:
                print("{}")
                sys.exit(1)

    success = provision_device()
    sys.exit(0 if success else 1)
