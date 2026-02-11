"""Local storage — SQLite version (no connection pool needed)."""
import os
import time
import db_helper

GPS_COORD_DECIMALS = int(os.getenv('GPS_COORD_DECIMALS', '7'))


def _format_coord(value):
    try:
        return f"{float(value):.{GPS_COORD_DECIMALS}f}"
    except Exception:
        return None


# Define a global variable to store the last insertion time
last_insertion_time = 0


def create_database():
    pass  # Handled by init_sqlite.py


def create_table():
    pass  # Handled by init_sqlite.py


def add_row(driver_status):
    try:
        db_helper.execute_commit(
            "INSERT INTO car_data (driver_status) VALUES (?)",
            (driver_status,))
    except Exception as e:
        print(f'Error adding row: {e}')


def add_row_new(driver_status):
    global last_insertion_time
    try:
        current_time = time.time()
        if (current_time - last_insertion_time) > 5:
            db_helper.execute_commit(
                "INSERT INTO car_data (driver_status) VALUES (?)",
                (driver_status,))
            last_insertion_time = current_time
    except Exception as e:
        print(f'Error adding row: {e}')


def add_gps_data(latitude, longitude, speed, timestamp, driver_status, acceleration):
    try:
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return
        db_helper.execute_commit(
            "INSERT INTO gps_data (latitude, longitude, speed, timestamp, driver_status, acceleration) VALUES (?,?,?,?,?,?)",
            (latitude, longitude, speed, timestamp, driver_status, acceleration))
    except Exception as e:
        print(f'Error adding row: {e}')


def get_last_gps_data():
    try:
        row = db_helper.fetchone(
            "SELECT latitude, longitude FROM gps_data ORDER BY id DESC LIMIT 1")
        if row:
            return (row['latitude'], row['longitude'])
        return None
    except Exception as e:
        print(f'Error getting last GPS data: {e}')
        return None


def add_gps_data_if_changed(latitude, longitude, speed):
    try:
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return

        formatted_latitude = _format_coord(latitude)
        formatted_longitude = _format_coord(longitude)

        last_gps_data = get_last_gps_data()
        last_latitude, last_longitude = last_gps_data if last_gps_data else (None, None)

        last_formatted_latitude = _format_coord(last_latitude) if last_latitude is not None else None
        last_formatted_longitude = _format_coord(last_longitude) if last_longitude is not None else None

        if (last_latitude is None or last_longitude is None or
                last_formatted_latitude != formatted_latitude or last_formatted_longitude != formatted_longitude):
            db_helper.execute_commit(
                "INSERT INTO gps_data (latitude, longitude, speed) VALUES (?,?,?)",
                (formatted_latitude, formatted_longitude, speed))
    except Exception as e:
        print(f'Error adding GPS data: {e}')


def update_count(date):
    try:
        row = db_helper.fetchone(
            "SELECT id, count FROM count_table WHERE date = ?", (date,))

        if row is None:
            db_helper.execute_commit(
                "INSERT INTO count_table (date, count) VALUES (?, 1)", (date,))
            return 'added'
        else:
            db_helper.execute_commit(
                "UPDATE count_table SET count = count + 1 WHERE id = ?",
                (row['id'],))
            return 'existed'
    except Exception as e:
        print(f'Error: {e}')
        return 'error'


def fetch_rows():
    try:
        rows = db_helper.fetchall(
            "SELECT * FROM car_data WHERE timestamp >= datetime('now', '-15 minutes')")
        return rows if rows else []
    except Exception as e:
        print(f'Error fetching rows: {e}')
        return []


def analyze_data(rows):
    status_counts = {}
    for row in rows:
        driver_status = row['driver_status']
        if driver_status in status_counts:
            status_counts[driver_status] += 1
        else:
            status_counts[driver_status] = 1
    return status_counts
