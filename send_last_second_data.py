"""Periodic GPS telemetry batch sender — SQLite version."""
import os
import time
import requests
from get_device_id import get_device_id_from_db
from get_user_info import get_user_info
import db_helper

POLL_INTERVAL = int(os.getenv('SEND_DATA_POLL_INTERVAL', '5'))
BATCH_SIZE = 50  # Max rows per API call
API_URL = 'https://api.copilotai.click/api/store-dc-data'

device_id = get_device_id_from_db()

# Token with auto-refresh
_token_cache = {"token": None, "last_refresh": 0}
TOKEN_REFRESH_INTERVAL = 300  # Refresh from DB every 5 minutes


def get_token():
    now = time.time()
    if _token_cache["token"] is None or now - _token_cache["last_refresh"] > TOKEN_REFRESH_INTERVAL:
        _token_cache["token"] = get_user_info('auth_key')
        _token_cache["last_refresh"] = now
    return _token_cache["token"]


def force_token_refresh():
    _token_cache["token"] = None
    _token_cache["last_refresh"] = 0


def send_batch(json_data):
    """Send a batch of GPS data. Returns True on success."""
    token = get_token()
    if not token:
        print("No auth token available")
        return False

    payload = {
        "device_id": device_id,
        "data": json_data
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=10)

        # Retry once on token expiry
        if response.status_code == 401 or "Invalid or expired token" in response.text:
            print("Token expired, refreshing...")
            force_token_refresh()
            headers['Authorization'] = f'Bearer {get_token()}'
            response = requests.post(API_URL, json=payload, headers=headers, timeout=10)

        if response.status_code in [200, 201]:
            print(f"Data sent successfully: {len(json_data)} items")
            return True
        else:
            print(f"API error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"Send error: {str(e)}")
        return False


# Start from latest ID — don't re-send old data on restart
row = db_helper.fetchone("SELECT MAX(id) as max_id FROM gps_data")
last_sent_id = row['max_id'] if row and row['max_id'] else 0
print(f"Starting from gps_data id={last_sent_id}, polling every {POLL_INTERVAL}s")

while True:
    try:
        rows = db_helper.fetchall(
            "SELECT id, latitude, longitude, speed, timestamp, driver_status, acceleration "
            "FROM gps_data WHERE id > ? ORDER BY id ASC LIMIT ?",
            (last_sent_id, BATCH_SIZE))

        if not rows:
            time.sleep(POLL_INTERVAL)
            continue

        json_data = []
        max_id = last_sent_id
        for row in rows:
            row_id = row['id']
            if row_id > max_id:
                max_id = row_id

            lat = float(row['latitude']) if row['latitude'] else 0.0
            lng = float(row['longitude']) if row['longitude'] else 0.0
            if lat == 0.0 or lng == 0.0:
                continue

            json_data.append({
                "lat": lat,
                "long": lng,
                "speed": float(row['speed']) if row['speed'] else 0.0,
                "timestamp": str(row['timestamp']),
                "driver_status": row['driver_status'] or 'Active',
                "acceleration": float(row['acceleration']) if row['acceleration'] else 0.0
            })

        if json_data:
            if send_batch(json_data):
                last_sent_id = max_id  # Only advance on success
        else:
            # All rows had lat/lon=0, advance past them
            last_sent_id = max_id

    except Exception as e:
        print(f"An error occurred: {str(e)}")

    time.sleep(POLL_INTERVAL)
