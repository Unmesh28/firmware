"""Periodic GPS telemetry batch sender — SQLite version."""
import os
import time
import requests
from get_device_id import get_device_id_from_db
from get_user_info import get_user_info
import threading
import db_helper

POLL_INTERVAL = int(os.getenv('SEND_DATA_POLL_INTERVAL', '10'))


def send_data_to_api(url, bearer_token, device_id, json_data):
    try:
        payload = {
            "device_id": device_id,
            "data": json_data
        }
        headers = {
            'Authorization': f'Bearer {bearer_token}',
            'Content-Type': 'application/json'
        }
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            print(f"Data sent successfully: {len(json_data)} items")
        else:
            print(f"API error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"Send error: {str(e)}")


url = 'https://api.copilotai.click/api/store-dc-data'

device_id = get_device_id_from_db()
bearer_token = get_user_info('auth_key')

# Track the last sent ID to avoid duplicates
last_sent_id = 0

while True:
    try:
        rows = db_helper.fetchall(
            "SELECT id, latitude, longitude, speed, timestamp, driver_status, acceleration "
            "FROM gps_data WHERE id > ? ORDER BY id ASC",
            (last_sent_id,))

        json_data = []
        max_id = last_sent_id
        for row in rows:
            lat = float(row['latitude']) if row['latitude'] else 0.0
            lng = float(row['longitude']) if row['longitude'] else 0.0

            row_id = row['id']
            if row_id > max_id:
                max_id = row_id

            if lat == 0.0 or lng == 0.0:
                continue

            json_row = {
                "lat": lat,
                "long": lng,
                "speed": float(row['speed']) if row['speed'] else 0.0,
                "timestamp": str(row['timestamp']),
                "driver_status": row['driver_status'],
                "acceleration": float(row['acceleration']) if row['acceleration'] else 0.0
            }
            json_data.append(json_row)

        if max_id > last_sent_id:
            last_sent_id = max_id

        if json_data:
            api_thread = threading.Thread(target=send_data_to_api, args=(url, bearer_token, device_id, json_data))
            api_thread.start()

    except Exception as e:
        print(f"An error occurred: {str(e)}")

    time.sleep(POLL_INTERVAL)
