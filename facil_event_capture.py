"""
Facial Tracking with Event-Based Frame Buffer & GIF Generation

This is an updated version of facil_updated_1.py that uses the new event capture system
for capturing pre-event, during-event, and post-event frames.
"""

import cv2
import os
import sys
import time
import gc
import fcntl
import socket
from datetime import datetime
from queue import Queue
import threading


# --- systemd watchdog: notify systemd we're alive ---
_sd_notify_sock = None

def _sd_notify(msg):
    """Send sd_notify message to systemd (READY=1, WATCHDOG=1, etc.)."""
    global _sd_notify_sock
    addr = os.getenv('NOTIFY_SOCKET')
    if not addr:
        return
    try:
        if _sd_notify_sock is None:
            _sd_notify_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            if addr[0] == '@':
                addr = '\0' + addr[1:]
            _sd_notify_sock.connect(addr)
        _sd_notify_sock.sendall(msg.encode())
    except Exception:
        _sd_notify_sock = None
import RPi.GPIO as GPIO
from store_locally import add_gps_data
from log import log_info, log_error
from get_device_id import get_device_id_from_db, get_auth_key_from_db
from get_user_info import get_user_info
from get_configure import get_configure
from facial_tracking.facialTracking import FacialTracker
import facial_tracking.conf as conf
from blnk_led import stop_blinking, start_blinking, refresh_blinking, update_led
from buzzer_controller import buzz_for, start_continuous_buzz, stop_continuous_buzz
from event_capture import init_event_capture, get_event_buffer, shutdown_event_capture


class ThreadedCamera:
    """
    Threaded camera capture to separate I/O from processing.
    Camera reads happen in a background thread so the main loop
    always has a fresh frame ready without blocking on I/O.
    Reference: https://pyimagesearch.com/2015/12/28/increasing-raspberry-pi-fps-with-python-and-opencv/
    """

    def __init__(self, src=0, width=640, height=480):
        self.cap = cv2.VideoCapture(src)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.grabbed, self.frame = self.cap.read()
        self.stopped = False
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._update, daemon=True).start()
        return self

    def _update(self):
        while not self.stopped:
            grabbed, frame = self.cap.read()
            with self.lock:
                self.grabbed = grabbed
                self.frame = frame

    def read(self):
        with self.lock:
            return self.grabbed, self.frame

    def isOpened(self):
        return self.cap.isOpened()

    def release(self):
        self.stopped = True
        self.cap.release()

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

# Initialize shared memory GPS reader (replaces Redis)
from gps_shm import GPSReader
gps_reader = GPSReader()

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

# NoFace buzzer settings (defaults — overridden by DB config)
NO_FACE_THRESHOLD = 2.0  # seconds before buzzing
BUZZER_DURATION = 1.7    # seconds

# --- Runtime settings from DB (re-read periodically) ---
_settings_last_read = 0
_settings_read_interval = 30  # re-read every 30 seconds
led_blink_enabled = True
noface_enabled = False
noface_threshold = 2.0

def _reload_settings():
    """Re-read settings from DB so changes take effect without restart."""
    global led_blink_enabled, noface_enabled, noface_threshold, _settings_last_read
    now = time.time()
    if now - _settings_last_read < _settings_read_interval:
        return
    _settings_last_read = now
    try:
        val = get_configure('led_blink_enabled')
        led_blink_enabled = val != '0' if val is not None else True
        val = get_configure('noface_enabled')
        noface_enabled = val == '1' if val is not None else False
        val = get_configure('noface_threshold')
        noface_threshold = float(val) if val else 2.0
    except Exception:
        pass

def capture_and_send_verification_image(frame, lat, long2, speed, acc):
    """
    Capture an instant image and send it to the backend for facial identification.
    This verifies that the registered driver is still driving the vehicle.
    """
    try:
        import base64
        import requests
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

        if response.status_code in (200, 201):
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

# Image annotation settings
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.4
FONT_THICKNESS = 1
_padding_x = 4
_padding_y = 3
_top_left = (15, 15)

def save_image(frame, folder_path, speed, lat2, long2, driver_status):
    """Save annotated image with driver status overlay and footer.
    Filename includes metadata for upload_images.py parsing."""
    lat2 = round(float(lat2), 4)
    long2 = round(float(long2), 4)
    timestamp_obj = datetime.now()
    timestamp = timestamp_obj.strftime("%Y-%m-%d %H:%M:%S")
    filename_time = timestamp_obj.strftime("%Y%m%d_%H%M%S%f")
    image_file = os.path.join(folder_path, f"{filename_time}_{driver_status}_{lat2}_{long2}_{speed}.jpg")

    # Draw driver status box at top
    driver_text = f"Driver Status: {driver_status}"
    (text_width, text_height), _ = cv2.getTextSize(driver_text, FONT, FONT_SCALE, FONT_THICKNESS)
    bottom_right = (_top_left[0] + text_width + 2 * _padding_x, _top_left[1] + text_height + 2 * _padding_y)
    cv2.rectangle(frame, _top_left, bottom_right, (139, 104, 0), -1)
    cv2.rectangle(frame, _top_left, bottom_right, (0, 0, 0), 1)
    text_position = (_top_left[0] + _padding_x, _top_left[1] + text_height + _padding_y - 2)
    cv2.putText(frame, driver_text, text_position, FONT, FONT_SCALE, (255, 255, 255), FONT_THICKNESS, cv2.LINE_AA)

    # Draw footer with metadata
    h, w = frame.shape[:2]
    footer_text = [
        "Sapience Automata 2025",
        f"Time:{timestamp}",
        f"Lat,Long:{lat2},{long2}",
        f"Speed:{speed} Km/h"
    ]
    cv2.rectangle(frame, (0, h - 30), (w, h), (255, 0, 0), -1)
    x = 5
    for text in footer_text:
        cv2.putText(frame, text, (x, h - 10), FONT, FONT_SCALE, (255, 255, 255), FONT_THICKNESS, cv2.LINE_AA)
        (tw, _), _ = cv2.getTextSize(text, FONT, FONT_SCALE, FONT_THICKNESS)
        x += tw + 10

    cv2.imwrite(image_file, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

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
    """Legacy helper kept for compatibility."""
    return value.decode() if isinstance(value, bytes) and value else str(value) if value else default

_last_idle_send_time = 0

# Single background worker for GPS sends — prevents unbounded thread spawning
_gps_queue = None
_save_image_queue = None

def _gps_worker():
    """Persistent worker for GPS data sends. One at a time, never piles up."""
    while True:
        try:
            args = _gps_queue.get()
            add_gps_data(*args)
        except Exception:
            pass

def _save_image_worker():
    """Persistent worker for save_image. Serializes SD card writes."""
    try:
        os.nice(5)  # Lower priority than detection
    except OSError:
        pass
    while True:
        try:
            args = _save_image_queue.get()
            save_image(*args)
        except Exception:
            pass

def _init_workers():
    """Start persistent background worker threads."""
    global _gps_queue, _save_image_queue
    _gps_queue = Queue(maxsize=2)
    _save_image_queue = Queue(maxsize=2)
    threading.Thread(target=_gps_worker, daemon=True).start()
    threading.Thread(target=_save_image_worker, daemon=True).start()

def _enqueue_gps(lat, long2, speed, timestamp, status, acc):
    """Queue GPS data for background send. Drops if queue full (backpressure)."""
    try:
        _gps_queue.put_nowait((lat, long2, speed, timestamp, status, acc))
    except Exception:
        pass  # Drop if worker is busy — next one will update

def _enqueue_save_image(frame, folder_path, speed, lat, long2, driver_status):
    """Queue image for background save. Drops if queue full."""
    try:
        _save_image_queue.put_nowait((frame, folder_path, speed, lat, long2, driver_status))
    except Exception:
        pass  # Drop if worker is busy

def send_status_and_stop_led(lat, long2, speed, status, acc):
    global _last_idle_send_time
    now = time.time()
    # Throttle: only send GPS + stop LED once per 2 seconds when idle
    if now - _last_idle_send_time < 2.0:
        return
    _last_idle_send_time = now

    stop_blinking()  # Direct call, no thread needed (just sets flag + GPIO off)
    stop_continuous_buzz()  # Stop buzzer when vehicle idle/stopped
    api_status = map_driver_status(status)
    _enqueue_gps(lat, long2, speed, str(datetime.now()), api_status, acc)

def main():
    # Start persistent background workers (GPS + save_image)
    _init_workers()

    # Disable automatic garbage collection — run manually every N frames
    # Prevents GC pauses (10-50ms) during real-time inference
    gc.disable()
    gc_interval = 300  # Run GC every 300 frames (~30 sec at 10 FPS) to minimize pauses

    # Threaded camera: I/O runs in background thread, main loop never blocks on read
    cap = ThreadedCamera(src=conf.CAM_ID, width=conf.FRAME_W, height=conf.FRAME_H)
    cap.start()

    facial_tracker = FacialTracker()

    # Frame timing for facial detection (from conf, default 10 FPS for Pi Zero 2W)
    target_detection_fps = conf.TARGET_DETECTION_FPS
    detection_frame_interval = 1.0 / target_detection_fps
    last_detection_time = 0

    # Frame timing for event buffer (2 FPS — raw numpy copy only, JPEG encoding in save worker)
    target_buffer_fps = 2
    buffer_frame_interval = 1.0 / target_buffer_fps
    last_buffer_frame_time = 0

    # GPS data sending cooldown (for Active status)
    last_gps_send_time = 0
    gps_send_interval = 2  # Send GPS every 2 seconds when Active

    # Track NoFace detection for buzzer trigger
    no_face_start_time = None
    no_face_buzzer_triggered = False

    # 15-minute interval driver verification
    last_verification_time = 0

    # GPS cache — read from shared memory at most 2x/sec
    last_gps_read_time = 0
    gps_read_interval = 0.5
    cached_speed = '0'
    cached_lat = '0.0'
    cached_long = '0.0'
    cached_acc = '0'

    # Throttle save_image: max 1 per second, offloaded to background thread
    # JPEG encode + SD card write takes 30-100ms — must NOT block detection loop
    last_save_time = 0
    SAVE_IMAGE_INTERVAL = 1.0

    # Detection resolution: half of capture to reduce preprocessing overhead
    # MediaPipe resizes to fixed model input internally, so savings are in
    # cvtColor + internal resize (~5-10ms). Main benefit: less memory pressure.
    # Eye/lip ratios are scale-invariant, so accuracy is preserved.
    DETECT_W = conf.FRAME_W // 2  # 320
    DETECT_H = conf.FRAME_H // 2  # 240

    log_info(f"Starting facial tracking with event capture (detection FPS: {target_detection_fps}, buffer FPS: {target_buffer_fps})")
    log_info(f"Capture: {conf.FRAME_W}x{conf.FRAME_H}, Detection: {DETECT_W}x{DETECT_H}, HEADLESS: {conf.HEADLESS}, refine_landmarks: {conf.REFINE_LANDMARKS}")
    log_info(f"Driver verification interval: {VERIFICATION_INTERVAL_SECONDS // 60} minutes")

    # Load settings from DB on startup
    _reload_settings()
    log_info(f"Settings: LED blink={led_blink_enabled}, NoFace alert={noface_enabled}, NoFace threshold={noface_threshold}s")

    # Tell systemd we're ready (for Type=notify)
    _sd_notify('READY=1')

    # Watchdog ping interval (ping every 10s, systemd timeout is 30s)
    _last_watchdog_ping = 0
    _watchdog_interval = 10

    try:
        while cap.isOpened():
            current_time = time.time()

            # Ping systemd watchdog — proves main loop is alive
            if current_time - _last_watchdog_ping >= _watchdog_interval:
                _last_watchdog_ping = current_time
                _sd_notify('WATCHDOG=1')

            # Re-read settings from DB periodically (every 30s)
            _reload_settings()

            # Read GPS from shared memory (single read, no network round-trip)
            if current_time - last_gps_read_time >= gps_read_interval:
                last_gps_read_time = current_time
                lat_f, lon_f, speed_i, acc_f, _ts = gps_reader.read()
                cached_speed = str(speed_i)
                cached_lat = str(lat_f) if lat_f != 0.0 else '0.0'
                cached_long = str(lon_f) if lon_f != 0.0 else '0.0'
                cached_acc = str(acc_f)

            speed = cached_speed
            lat = cached_lat
            long2 = cached_long
            acc = cached_acc

            # Check speed threshold
            if speed is None or int(speed) < speed_config:
                # Vehicle not moving or below speed threshold
                if float(lat) != 0.0 and float(long2) != 0.0:
                    send_status_and_stop_led(lat, long2, speed or "0", "", acc)
                time.sleep(0.1)  # Slow down when not processing
                continue

            # Frame timing: throttle detection to target FPS
            if current_time - last_detection_time < detection_frame_interval:
                time.sleep(0.001)  # Yield CPU briefly
                continue

            # Read latest frame from threaded camera (non-blocking, always fresh)
            success, frame = cap.read()
            if not success or frame is None:
                time.sleep(0.01)
                continue

            # Downscale for detection: reduces cvtColor + internal resize overhead
            # Eye/lip ratios are scale-invariant — identical detection accuracy
            detect_frame = cv2.resize(frame, (DETECT_W, DETECT_H), interpolation=cv2.INTER_AREA)

            last_detection_time = current_time
            t_start = time.time()
            facial_tracker.process_frame(detect_frame)
            t_process = time.time() - t_start

            # Log processing time every 30 frames to track actual FPS
            if not hasattr(main, '_frame_count'):
                main._frame_count = 0
                main._total_process_time = 0
            main._frame_count += 1
            main._total_process_time += t_process
            if main._frame_count % 30 == 0:
                avg_ms = (main._total_process_time / 30) * 1000
                actual_fps = 30 / main._total_process_time if main._total_process_time > 0 else 0
                log_info(f"Performance: avg {avg_ms:.0f}ms/frame, max possible FPS: {actual_fps:.1f}, detected: {facial_tracker.detected}")
                main._total_process_time = 0

            # Manual GC at controlled intervals (avoids random GC pauses during inference)
            if main._frame_count % gc_interval == 0:
                gc.collect()

            # Determine driver status
            # Watchdog pattern: buzzer & LED auto-stop if not refreshed.
            # During Sleeping/Yawning: call start_continuous_buzz() + refresh_blinking() every frame.
            # When condition clears: explicit stop for instant response, watchdog as safety net.
            if facial_tracker.detected:
                # Reset NoFace timer when face is detected
                no_face_start_time = None
                no_face_buzzer_triggered = False

                if facial_tracker.eyes_status == 'eye closed':
                    driver_status = 'Sleeping'
                    start_continuous_buzz()   # Refreshes watchdog each frame
                    if led_blink_enabled:
                        start_blinking()          # Activate LED (idempotent)
                        refresh_blinking()        # Refreshes LED watchdog each frame
                        update_led()              # Actually toggle the GPIO pin
                    # Save annotated image (throttled 1/sec, queued to worker)
                    if current_time - last_save_time >= SAVE_IMAGE_INTERVAL:
                        last_save_time = current_time
                        _enqueue_save_image(frame.copy(), folder_path, speed, lat, long2, driver_status)

                elif facial_tracker.yawn_status == 'yawning':
                    driver_status = 'Yawning'
                    start_continuous_buzz()   # Refreshes watchdog each frame
                    if led_blink_enabled:
                        start_blinking()          # Activate LED (idempotent)
                        refresh_blinking()        # Refreshes LED watchdog each frame
                        update_led()              # Actually toggle the GPIO pin
                    if current_time - last_save_time >= SAVE_IMAGE_INTERVAL:
                        last_save_time = current_time
                        _enqueue_save_image(frame.copy(), folder_path, speed, lat, long2, driver_status)

                else:
                    driver_status = 'Active'
                    # Immediate stop — no throttle. Just flag+GPIO ops, very cheap.
                    stop_blinking()
                    stop_continuous_buzz()
            else:
                driver_status = 'NoFace'

                # Immediate stop of continuous alerts (driver not visible)
                stop_blinking()
                stop_continuous_buzz()

                # Start tracking NoFace duration
                if no_face_start_time is None:
                    no_face_start_time = current_time
                    no_face_buzzer_triggered = False  # Reset for new NoFace period

                # Calculate how long NoFace has persisted
                no_face_duration = current_time - no_face_start_time

                # Buzz when NoFace persists beyond threshold (configurable via Settings)
                if noface_enabled and no_face_duration >= noface_threshold:
                    buzz_for(BUZZER_DURATION)
                    no_face_buzzer_triggered = True
                    log_info(f"NoFace detected for {no_face_duration:.1f}s - buzzer activated")
                    no_face_start_time = current_time  # Reset timer for next interval

            # Add full-res frame to event buffer at throttled rate (2 FPS)
            # Uses 640x480 for image quality; raw bytes stored, JPEG encoding in save worker
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
                    _enqueue_gps(lat, long2, speed, str(datetime.now()), driver_status, acc)
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
        stop_continuous_buzz()
        shutdown_event_capture()
        cap.release()
        GPIO.cleanup()
        cv2.destroyAllWindows()
        log_info("Facial tracking stopped")

if __name__ == '__main__':
    acquire_lock()
    main()