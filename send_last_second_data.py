import mysql.connector
import json
import time
import requests
from get_device_id import *
from get_user_info import *
import threading

# Function to send data to the API
def send_data_to_api(url, bearer_token, device_id, json_data):
    try:
        # Prepare the request payload
        payload = {
            "device_id": device_id,
            "data": json_data
        }

        # Set the request headers
        headers = {
            'Authorization': f'Bearer {bearer_token}',
            'Content-Type': 'application/json'
        }

        # Send the POST request to the API
        response = requests.post(url, json=payload, headers=headers, timeout=10)

        # Check the response status code (200 or 201 are both success)
        if response.status_code in [200, 201]:
            print(f"Data sent successfully: {len(json_data)} items")
        else:
            print(f"API error {response.status_code}: {response.text}")

    except Exception as e:
        print(f"Send error: {str(e)}")

# API URL and bearer token
url = 'https://api.copilotai.click/api/store-dc-data'

device_id = get_device_id_from_db()
bearer_token = get_user_info('auth_key')

# Track the last sent ID to avoid duplicates
last_sent_id = 0

while True:
    try:
        # Connect to MySQL database
        with mysql.connector.connect(
            host="127.0.0.1",
            user="root",
            password="raspberry@123",
            database="car"
        ) as conn:
            cursor = conn.cursor(dictionary=True)

            # Execute SQL query to retrieve new rows since last sent
            query = """
                SELECT id, latitude, longitude, speed, timestamp, driver_status, acceleration
                FROM gps_data
                WHERE id > %s
                ORDER BY id ASC;
            """
            cursor.execute(query, (last_sent_id,))

            # Fetch all rows
            rows = cursor.fetchall()

            # Create an array of JSON objects (filter out invalid GPS coordinates)
            json_data = []
            max_id = last_sent_id
            for row in rows:
                lat = float(row["latitude"]) if row["latitude"] else 0.0
                lng = float(row["longitude"]) if row["longitude"] else 0.0
                
                # Track the max ID we've processed
                if row["id"] > max_id:
                    max_id = row["id"]
                
                # Skip rows with invalid GPS coordinates (0,0)
                if lat == 0.0 or lng == 0.0:
                    continue
                    
                json_row = {
                    "lat": lat,
                    "long": lng,
                    "speed": float(row["speed"]) if row["speed"] else 0.0,
                    "timestamp": str(row["timestamp"]),
                    "driver_status": row["driver_status"],
                    "acceleration": float(row["acceleration"]) if row["acceleration"] else 0.0
                }
                json_data.append(json_row)
            
            # Update last_sent_id to avoid re-sending
            if max_id > last_sent_id:
                last_sent_id = max_id
                print(f"Updated last_sent_id to {last_sent_id}")
                
            print(json_data)
            # Only send if there's valid data
            if json_data:
                api_thread = threading.Thread(target=send_data_to_api, args=(url, bearer_token, device_id, json_data))
                api_thread.start()

    except Exception as e:
        print(f"An error occurred: {str(e)}")
    finally:
        # Close cursor and connection
        cursor.close()
    # Wait for 1 second before the next iteration
    time.sleep(1)
