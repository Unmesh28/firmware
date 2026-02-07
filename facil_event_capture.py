"""
Facial Tracking with Event-Based Frame Buffer & GIF Generation

This is an updated version of facil_updated_1.py that uses the new event capture system
for capturing pre-event, during-event, and post-event frames.
"""

import cv2
import os
import sys
import time
import fcntl
from datetime import datetime
import threading
import redis
import RPi.GPIO as GPIO
import base64
import requests
from store_locally import add_gps_data
from log import log_info, log_error
from get_device_id import get_device_id_from_db, get_auth_key_from_db
from get_user_info import get_user_info
from get_configure import get_configure
from facial_tracking.facialTracking import FacialTracker
import facial_tracking.conf as conf
from blnk_led import stop_blinking, start_blinking
from buzzer_controller import buzz_for
from event_capture import init_event_capture, get_event_buffer, shutdown_event_capture

# Default bundle version
bundle_version = "1.0.0"

# Lock file to prevent multiple instances
LOCK_FILE = '/tmp/facil_event_capture.lock'
lock_fd = None

def acquire_lock():
    """Ensure only one instance runs at a time"""
    global lock_fd
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        print(f"Lock acquired. PID: {os.getpid()}")
        return True
    except IOError:
        print("ERROR: Another instance of facil_event_capture.py is already running.")
        print("Kill existing process first: pkill -f facil_event_capture.py")
        sys.exit(1)

# Initialize Redis client
redis_client = redis.Redis(host='127.0.0.1', port=6379)

# Get device information from the database
device_id = get_device_id_from_db()
auth_key = get_auth_key_from_db()
phone_number = get_user_info('phone_number')
access_token = auth_key if auth_key else get_user_info('access_token')
speed_config = int(get_configure('speed')) if get_configure('speed') else 0

# Debug: Print token info at startup
print(f"DEBUG: device_id={device_id}")
print(f"DEBUG: auth_key={'set ('+auth_key[:8]+'...)' if auth_key else 'None'}")
print(f"DEBUG: access_token={'set ('+access_token[:8]+'...)' if access_token else 'None'}")

# Check if device is provisioned
if not device_id or not auth_key:
    print("WARNING: Device not provisioned. Run: python device_provisioning.py")
    print(f"device_id: {device_id}, auth_key: {'set' if auth_key else 'not set'}")

# Create folders
today_date = datetime.now().strftime("%Y-%m-%d")
base_path = "/home/pi/facial-tracker-firmware/images"
folder_path = os.path.join(base_path, today_date)
events_path = os.path.join(base_path, "events")
os.makedirs(folder_path, exist_ok=True)
os.makedirs(events_path, exist_ok=True)

# Initialize event capture system
event_buffer = init_event_capture(
    base_events_path=events_path,
    device_id=device_id,
    auth_token=access_token
)

# 15-minute interval verification settings
VERIFICATION_INTERVAL_SECONDS = 15 * 60  # 15 minutes
VERIFICATION_API_URL = "https://api.copilotai.click/api/driver-verification/capture"

# NoFace buzzer settings
NO_FACE_THRESHOLD = 2.0  # seconds before buzzing
BUZZER_DURATION = 1.7    # seconds

def capture_and_send_verification_image(frame, lat, long2, speed, acc):
    """
    Capture an instant image and send it to the backend for facial identification.
    This verifies that the registered driver is still driving the vehicle.
    """
    try:
        # Encode frame to JPEG
        encode_param = [cv2.IMWRITE_JPEG_QUALITY, 85]
        _, encoded = cv2.imencode('.jpg', frame, encode_param)
        image_base64 = base64.b64encode(encoded.tobytes()).decode('utf-8')

        # Prepare payload
        payload = {
            "device_id": device_id,
            "image": image_base64,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "lat": lat,
            "long": long2,
            "speed": speed,
            "acceleration": acc
        }

        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # Send to verification endpoint
        response = requests.post(
            VERIFICATION_API_URL,
            json=payload,
            headers=headers,
            timeout=30
        )

        if response.status_code == 201:
            result = response.json()
            log_info(f"Driver verification sent successfully: {result.get('message', 'OK')}")
            return True
        else:
            log_error(f"Driver verification failed: {response.status_code} - {response.text}")
            return False

    except requests.exceptions.Timeout:
        log_error("Driver verification request timed out")
        return False
    except Exception as e:
        log_error(f"Error sending driver verification: {str(e)}")
        return False

def map_driver_status(status):
    """Map device driver status to backend expected format"""
    status_map = {
        'Sleeping/Looking Down': 'Sleeping',
        'Yawning/Fatigued': 'Yawning',
        'No Face': 'NoFace',
        'Active': 'Active',
        'eye closed': 'Sleeping',
        'yawning': 'Yawning',
    }
    return status_map.get(status, status)

def decode_or_default(value, default='0'):
    return value.decode() if value else default

def send_status_and_stop_led(lat, long2, speed, status, acc):
    threadStopBlinkLed_1 = threading.Thread(target=stop_blinking)
    threadStopBlinkLed_1.start()
    api_status = map_driver_status(status)
    threadSendDataToApi = threading.Thread(
        target=add_gps_data,
        args=(lat, long2, speed, str(datetime.now()), api_status, acc)
    )
    threadSendDataToApi.start()

def main():
    cap = cv2.VideoCapture(conf.CAM_ID)
    cap.set(3, conf.FRAME_W)
    cap.set(4, conf.FRAME_H)
    facial_tracker = FacialTracker()

    # Frame timing for facial detection (15 FPS to reduce CPU usage)
    target_detection_fps = 15
    detection_frame_interval = 1.0 / target_detection_fps
    last_detection_time = 0

    # Frame timing for event buffer (5 FPS for GIF)
    target_buffer_fps = 5
    buffer_frame_interval = 1.0 / target_buffer_fps
    last_buffer_frame_time = 0

    # GPS data sending cooldown (for Active status)
    last_gps_send_time = 0
    gps_send_interval = 2  # Send GPS every 2 seconds when Active

    # Track last LED stop time to avoid spawning too many threads
    last_led_stop_time = 0
    led_stop_interval = 1.0  # Only call stop_blinking once per second max

    # Track NoFace detection for buzzer trigger
    no_face_start_time = None
    no_face_buzzer_triggered = False

    # 15-minute interval driver verification
    last_verification_time = 0

    log_info(f"Starting facial tracking with event capture (buffer FPS: {target_buffer_fps})")
    log_info(f"Driver verification interval: {VERIFICATION_INTERVAL_SECONDS // 60} minutes")

    try:
        while cap.isOpened():
            current_time = time.time()

            # Read GPS data from Redis
            speed = decode_or_default(redis_client.get('speed'))
            lat = decode_or_default(redis_client.get('lat'), '0.0')
            long2 = decode_or_default(redis_client.get('long'), '0.0')
            acc = decode_or_default(redis_client.get('acc'), '0')

            # Check speed threshold
            if speed is None or int(speed) < speed_config:
                # Vehicle not moving or below speed threshold
                if float(lat) != 0.0 and float(long2) != 0.0:
                    send_status_and_stop_led(lat, long2, speed or "0", "", acc)
                time.sleep(0.1)  # Slow down when not processing
                continue

            # Capture frame
            success, frame = cap.read()
            if not success:
                log_error("Failed to capture frame")
                continue

            frame = cv2.flip(frame, 1)

            # Process frame for facial detection at limited FPS (reduces CPU usage)
            if current_time - last_detection_time >= detection_frame_interval:
                last_detection_time = current_time
                facial_tracker.process_frame(frame)

                       # Determine driver status
            if facial_tracker.detected:
                # Reset NoFace timer when face is detected
                no_face_start_time = None
                no_face_buzzer_triggered = False

                if facial_tracker.eyes_status == 'eye closed':
                    driver_status = 'Sleeping'
                    threading.Thread(target=start_blinking).start()
                elif facial_tracker.yawn_status == 'yawning':
                    driver_status = 'Yawning'
                    threading.Thread(target=start_blinking).start()
                else:
                    driver_status = 'Active'
                    # Stop LED blinking for active status (throttled to reduce thread spawning)
                    if current_time - last_led_stop_time >= led_stop_interval:
                        threading.Thread(target=stop_blinking).start()
                        last_led_stop_time = current_time
            else:
                driver_status = 'NoFace'

                # Start tracking NoFace duration
                if no_face_start_time is None:
                    no_face_start_time = current_time
                    no_face_buzzer_triggered = False  # Reset for new NoFace period

                # Calculate how long NoFace has persisted
                no_face_duration = current_time - no_face_start_time

                # Buzz every 2 seconds continuously while NoFace persists
                if no_face_duration >= NO_FACE_THRESHOLD:
                    # Calculate how many 2-second intervals have passed
                    intervals_passed = int(no_face_duration // NO_FACE_THRESHOLD)

                    # Check if we should buzz again (new interval reached)
                    if not no_face_buzzer_triggered or no_face_duration >= (intervals_passed * NO_FACE_THRESHOLD):
                        threading.Thread(target=buzz_for, args=(BUZZER_DURATION,)).start()
                        no_face_buzzer_triggered = True
                        log_info(f"NoFace detected for {no_face_duration:.1f}s - buzzer activated (interval {intervals_passed})")
                        # Update the threshold for next buzz
                        no_face_start_time = current_time  # Reset timer for next 2-second interval

                # Throttle LED stop calls to reduce thread spawning
                if current_time - last_led_stop_time >= led_stop_interval:
                    threading.Thread(target=stop_blinking).start()
                    last_led_stop_time = current_time

            # Add frame to event buffer at throttled rate (5 FPS for GIF)
            # This doesn't affect buzzer - that already triggered in process_frame()
            if current_time - last_buffer_frame_time >= buffer_frame_interval:
                last_buffer_frame_time = current_time
                event_buffer.add_frame(
                    frame=frame,
                    speed=speed,
                    lat=lat,
                    long=long2,
                    acc=acc,
                    driver_status=driver_status
                )

            # Send GPS data periodically for Active status (for live tracking)
            if driver_status == 'Active' and float(lat) != 0.0 and float(long2) != 0.0:
                if current_time - last_gps_send_time >= gps_send_interval:
                    threading.Thread(
                        target=add_gps_data,
                        args=(lat, long2, speed, str(datetime.now()), driver_status, acc)
                    ).start()
                    last_gps_send_time = current_time

            # 15-minute interval driver verification capture
            # Only capture when face is detected to ensure quality verification image
            if facial_tracker.detected and current_time - last_verification_time >= VERIFICATION_INTERVAL_SECONDS:
                last_verification_time = current_time
                log_info("Capturing 15-minute interval driver verification image")
                # Send verification in background thread to avoid blocking
                threading.Thread(
                    target=capture_and_send_verification_image,
                    args=(frame.copy(), lat, long2, speed, acc)
                ).start()

    except KeyboardInterrupt:
        log_info("Shutting down...")
    finally:
        # Graceful shutdown
        shutdown_event_capture()
        cap.release()
        GPIO.cleanup()
        cv2.destroyAllWindows()
        log_info("Facial tracking stopped")

if __name__ == '__main__':
    acquire_lock()
    main()