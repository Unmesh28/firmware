"""Device configuration management — SQLite version."""
import db_helper

# Default configuration values
DEFAULT_CONFIG = {
    'speed': '20',  # Activation speed threshold (km/h)
    'gps_retention_days': '30',  # GPS data retention in active days
    'image_retention_days': '15',  # Image retention in days
    'led_blink_enabled': '1',  # LED blink on sleep/yawn (1=on, 0=off)
    'noface_enabled': '0',  # NoFace buzzer alert (1=on, 0=off)
    'noface_threshold': '2',  # Seconds before NoFace buzzer triggers
}


def get_configure(config_key):
    try:
        row = db_helper.fetchone(
            "SELECT config_value FROM configure WHERE config_key = ?",
            (config_key,))
        if row:
            print(f"Value for '{config_key}' is {row['config_value']}")
            return row['config_value']
        else:
            # Return default value if available
            default_val = DEFAULT_CONFIG.get(config_key)
            if default_val:
                print(f"No value found for '{config_key}', using default: {default_val}")
                return default_val
            print(f"No value found for config_key '{config_key}' in the configure table.")
            return None
    except Exception as e:
        print("Error:", e)
        return None


def set_configure(config_key, config_value):
    """Set or update a configuration value in the database"""
    try:
        row = db_helper.fetchone(
            "SELECT config_value FROM configure WHERE config_key = ?",
            (config_key,))

        if row:
            db_helper.execute_commit(
                "UPDATE configure SET config_value = ? WHERE config_key = ?",
                (str(config_value), config_key))
        else:
            db_helper.execute_commit(
                "INSERT INTO configure (config_key, config_value) VALUES (?, ?)",
                (config_key, str(config_value)))

        print(f"Set '{config_key}' = '{config_value}'")
        return True
    except Exception as e:
        print(f"Error setting config: {e}")
        return False


def ensure_configure_table():
    """Ensure configure table exists (handled by init_sqlite.py, kept for compatibility)"""
    try:
        db_helper.execute_commit("""
            CREATE TABLE IF NOT EXISTS configure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key TEXT UNIQUE NOT NULL,
                config_value TEXT
            )
        """)
        return True
    except Exception as e:
        print(f"Error ensuring configure table: {e}")
        return False
