#!/usr/bin/env python3
"""
Device Provisioning Script - Runs on first boot to register device with backend.

This script:
1. Checks if device is already provisioned (has device_id and auth_key)
2. If not, calls the create-device API to get credentials
3. Stores device_id and auth_key in local MySQL database

This should run ONCE during manufacturing when the device is first powered on.
"""

import json
import logging
import os
import sys
import urllib.request
import urllib.error
import mysql.connector
from mysql.connector import Error

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
LOG_DIR = os.path.expanduser("~/Desktop/Updated_codes/logs")
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
    # Backend API
    API_BASE_URL = os.getenv("API_BASE_URL", "https://api.copilotai.click")

    # Provisioning token (secure method - recommended)
    PROVISIONING_TOKEN = os.getenv("PROVISIONING_TOKEN", "")  # Set this for secure provisioning

    # Legacy: Admin credentials (fallback if no provisioning token)
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

    # Device type
    DEVICE_TYPE = os.getenv("DEVICE_TYPE", "DM")  # DM = Driver Monitoring

    # MySQL connection
    DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
    DB_NAME = os.getenv("DB_NAME", "car")
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "raspberry@123")


def get_db_connection():
    """Get MySQL database connection"""
    return mysql.connector.connect(
        host=Config.DB_HOST,
        database=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD
    )


def ensure_device_table():
    """Ensure device table exists with auth_key column"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Create device table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS device (
                id INT AUTO_INCREMENT PRIMARY KEY,
                device_id VARCHAR(100) UNIQUE NOT NULL,
                auth_key VARCHAR(64),
                device_type VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Check if auth_key column exists, add if not
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'device' AND column_name = 'auth_key'
        """, (Config.DB_NAME,))

        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE device ADD COLUMN auth_key VARCHAR(64)")
            logger.info("Added auth_key column to device table")

        conn.commit()
        cursor.close()
        conn.close()
        return True

    except Error as e:
        logger.error(f"Database error: {e}")
        return False


def is_device_provisioned():
    """Check if device already has device_id and auth_key"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT device_id, auth_key FROM device LIMIT 1")
        row = cursor.fetchone()

        cursor.close()
        conn.close()

        if row and row[0] and row[1]:
            logger.info(f"Device already provisioned: {row[0]}")
            return True, row[0], row[1]

        return False, None, None

    except Error as e:
        logger.error(f"Error checking provisioning status: {e}")
        return False, None, None


def call_create_device_api():
    """Call backend API to create device and get credentials"""
    try:
        url = f"{Config.API_BASE_URL}/api/auth/create-device"

        # Use provisioning token (secure) or fall back to admin credentials (legacy)
        if Config.PROVISIONING_TOKEN:
            # Secure method: use provisioning token
            request_data = {
                "device_type": Config.DEVICE_TYPE,
                "provisioning_token": Config.PROVISIONING_TOKEN
            }
            logger.info("Using provisioning token for authentication")
        else:
            # Legacy method: use admin credentials
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
    """Save device credentials to MySQL database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Clear existing records and insert new one
        cursor.execute("DELETE FROM device")

        # Try to insert with device_type column, fall back to without if column doesn't exist
        try:
            cursor.execute(
                "INSERT INTO device (device_id, auth_key, device_type) VALUES (%s, %s, %s)",
                (device_id, auth_key, Config.DEVICE_TYPE)
            )
        except Error as col_error:
            if "Unknown column" in str(col_error):
                # Table doesn't have device_type column, insert without it
                logger.info("device_type column not found, inserting without it")
                cursor.execute(
                    "INSERT INTO device (device_id, auth_key) VALUES (%s, %s)",
                    (device_id, auth_key)
                )
            else:
                raise col_error

        conn.commit()
        cursor.close()
        conn.close()

        logger.info(f"Saved device credentials: device_id={device_id}")
        return True

    except Error as e:
        logger.error(f"Error saving credentials: {e}")
        return False


def provision_device():
    """Main provisioning function"""
    logger.info("=" * 50)
    logger.info("Device Provisioning - Starting")
    logger.info("=" * 50)

    # Ensure database table exists
    if not ensure_device_table():
        logger.error("Failed to setup database")
        return False

    # Check if already provisioned
    provisioned, device_id, auth_key = is_device_provisioned()
    if provisioned:
        logger.info("Device already provisioned, skipping")
        return True

    # Validate credentials are set
    if not Config.PROVISIONING_TOKEN and not Config.ADMIN_PASSWORD:
        logger.error("No authentication configured")
        logger.error("Please set PROVISIONING_TOKEN (recommended) or ADMIN_EMAIL/ADMIN_PASSWORD")
        logger.error("Example: export PROVISIONING_TOKEN='your_secure_token'")
        return False

    # Call API to create device
    logger.info("Provisioning new device...")
    device_id, auth_key = call_create_device_api()

    if not device_id or not auth_key:
        logger.error("Failed to get device credentials from API")
        return False

    # Save credentials locally
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
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT device_id, auth_key FROM device LIMIT 1")
        row = cursor.fetchone()

        cursor.close()
        conn.close()

        if row:
            return {"device_id": row[0], "auth_key": row[1]}
        return None

    except Error as e:
        logger.error(f"Error getting credentials: {e}")
        return None


def delete_device_credentials():
    """Delete device credentials from database (for re-provisioning)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Truncate device table to reset auto-increment
        cursor.execute("TRUNCATE TABLE device")
        logger.info("Truncated device table")
        
        # Also truncate user_info table
        try:
            cursor.execute("TRUNCATE TABLE user_info")
            logger.info("Truncated user_info table")
        except Error as e:
            # user_info table might not exist, that's ok
            logger.warning(f"Could not truncate user_info table: {e}")

        conn.commit()
        cursor.close()
        conn.close()

        # Cleanup images folder
        import shutil
        images_path = "/home/pi/facial-tracker-firmware/images"
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

        logger.info("Device credentials deleted successfully")
        return True

    except Error as e:
        logger.error(f"Error deleting credentials: {e}")
        return False


if __name__ == "__main__":
    # Check for command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--check":
            # Just check if provisioned
            provisioned, device_id, auth_key = is_device_provisioned()
            if provisioned:
                print(f"PROVISIONED: {device_id}")
                sys.exit(0)
            else:
                print("NOT_PROVISIONED")
                sys.exit(1)
        elif sys.argv[1] == "--get":
            # Get credentials as JSON
            creds = get_device_credentials()
            if creds:
                print(json.dumps(creds))
                sys.exit(0)
            else:
                print("{}")
                sys.exit(1)

    # Run provisioning
    success = provision_device()
    sys.exit(0 if success else 1)