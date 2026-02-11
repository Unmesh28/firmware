#!/usr/bin/env python3
"""Initialize SQLite database — replaces MySQL init_database.sql"""
import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "blinksmart.db"))


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Device table (provisioning)
    c.execute("""
        CREATE TABLE IF NOT EXISTS device (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            auth_key TEXT,
            device_type TEXT DEFAULT 'DM',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # User info (legacy compatibility)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT,
            access_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # GPS data
    c.execute("""
        CREATE TABLE IF NOT EXISTS gps_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL,
            longitude REAL,
            speed REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            driver_status TEXT,
            acceleration REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_gps_timestamp ON gps_data(timestamp)")

    # Car data (driver status log)
    c.execute("""
        CREATE TABLE IF NOT EXISTS car_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            driver_status TEXT
        )
    """)

    # Configure (key-value settings)
    c.execute("""
        CREATE TABLE IF NOT EXISTS configure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_key TEXT UNIQUE NOT NULL,
            config_value TEXT
        )
    """)

    # Count table (daily event counts)
    c.execute("""
        CREATE TABLE IF NOT EXISTS count_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            count INTEGER DEFAULT 0
        )
    """)

    # Insert defaults
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('speed', '0')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('alert_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('upload_interval', '30')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('gps_retention_days', '30')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('image_retention_days', '15')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('led_blink_enabled', '1')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('noface_enabled', '0')")
    c.execute("INSERT OR IGNORE INTO configure (config_key, config_value) VALUES ('noface_threshold', '2')")

    conn.commit()
    conn.close()
    print(f"SQLite database initialized: {DB_PATH}")


if __name__ == "__main__":
    init_db()
