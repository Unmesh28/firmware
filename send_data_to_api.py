import requests
import uuid
from log import log_info, log_error  # Assuming log_error is available


def generate_alert_id():
    """Generate a unique alert_id for matching GPS data with images"""
    return f"alert_{uuid.uuid4().hex[:12]}"


def send_data_to_api(device_id, timestamp, speed, lat, long2, driver_status, token, acc, alert_id=None):
    """
    Send GPS/telemetry data to the API.
    
    Args:
        device_id: Device identifier
        timestamp: Timestamp string
        speed: Speed value
        lat: Latitude
        long2: Longitude
        driver_status: Driver status (Active, Sleeping, Yawning, etc.)
        token: Auth token
        acc: Acceleration value (bytes or string)
        alert_id: Optional alert_id for matching with images. If None, one will be generated.
    
    Returns:
        alert_id: The alert_id used (for matching with send_image_to_api)
    """
    print("inside API")
    url = 'https://api.copilotai.click/api/store-dc-data'

    # Generate alert_id if not provided
    if alert_id is None:
        alert_id = generate_alert_id()

    # Handle acceleration - could be bytes or string
    if isinstance(acc, bytes):
        acc_str = acc.decode('utf-8')
    else:
        acc_str = str(acc)

    # NOTE: data must be an ARRAY, not an object
    data = {
        "device_id": device_id,
        "data": [{  # <-- Array with single item
            "timestamp": timestamp,
            "speed": speed,
            "lat": lat,
            "long": long2,
            "driver_status": driver_status,
            "acceleration": acc_str,
            "alert_id": alert_id  # Required for matching with images
        }]
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    try:
        # Debug: Log token status
        if not token:
            log_error(f"TOKEN IS NONE OR EMPTY!")
        else:
            log_info(f"Using token: {token[:8]}...")
        print(data)
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 201:
            log_info(f"Data sent successfully (alert_id: {alert_id})")
            return alert_id
        else:
            log_error(f'Error sending data: {response.status_code} - {response.text}')
            return None
    except requests.exceptions.RequestException as e:
        log_error(f'Error sending data: {str(e)}')
        return None


def send_image_to_api(device_id, alert_id, base64_image, token):
    """
    Send image data to the API. The image will be matched with GPS data by alert_id.
    
    Args:
        device_id: Device identifier
        alert_id: The alert_id from send_data_to_api (for matching)
        base64_image: Base64 encoded image string
        token: Auth token
    
    Returns:
        bool: True if successful, False otherwise
    """
    url = 'https://api.copilotai.click/api/store-image-data'

    data = {
        "device_id": device_id,
        "alert_id": alert_id,
        "image": base64_image
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    try:
        print(f"Sending image for alert_id: {alert_id}")
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 201:
            log_info(f"Image sent successfully (alert_id: {alert_id})")
            return True
        else:
            log_error(f'Error sending image: {response.status_code} - {response.text}')
            return False
    except requests.exceptions.RequestException as e:
        log_error(f'Error sending image: {str(e)}')
        return False

def upload_interval_file(token ,device_id, timestamp, base64_image):
    url = 'https://api.copilotai.click/api/upload-interval-file'

    data = {
        "device_id": device_id,
        "date": timestamp,
        "file_name": base64_image
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 201:
            log_info("Image capture interval have sent successfully")
            #print('Image capture interval have sent successfully')
        else:
            log_error(f'Error sending data: {response.status_code} - {response.text}')
            #print(f'Error sending data: {response.status_code}')
    except requests.exceptions.RequestException as e:
        log_error(f'Error sending data: {str(e)}')
        #print(f'Error sending data: {str(e)}')
