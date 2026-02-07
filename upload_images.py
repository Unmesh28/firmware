import os
import requests
import base64
import glob
import time
import shutil
import gc
from datetime import datetime
from get_device_id import *
from get_user_info import *
from log import log_info, log_error

# Batch size for processing frames (reduces peak RAM usage)
FRAME_BATCH_SIZE = 20  # Process 20 frames at a time

# API endpoints
URL = "https://api.copilotai.click/api/upload-file"
EVENT_UPLOAD_URL = "https://api.copilotai.click/api/events/upload"

# Directories
PENDING_DIR = "/home/pi/facial-tracker-firmware/images/pending"
EVENTS_DIR = "/home/pi/facial-tracker-firmware/images/events"
POLL_INTERVAL = 30  # seconds (batch uploads to reduce CPU contention with facial detection)

DEVICE_ID = get_device_id_from_db()

# Token caching with refresh
_token_cache = {"token": None, "last_refresh": 0}
TOKEN_REFRESH_INTERVAL = 60  # Refresh token every 60 seconds

def get_current_token():
    """Get current token, refreshing from DB if needed"""
    current_time = time.time()
    if (_token_cache["token"] is None or 
        current_time - _token_cache["last_refresh"] > TOKEN_REFRESH_INTERVAL):
        _token_cache["token"] = get_user_info('auth_key')
        _token_cache["last_refresh"] = current_time
        print(f"Token refreshed at {datetime.now()}")
    return _token_cache["token"]

def get_headers():
    """Get headers with current token"""
    return {
        "Authorization": f"Bearer {get_current_token()}",
        "Content-Type": "application/json"
    }

def force_token_refresh():
    """Force immediate token refresh"""
    _token_cache["token"] = None
    _token_cache["last_refresh"] = 0

def parse_filename_metadata(filename):
    """Parse metadata from filename format: {timestamp}_{status}_{lat}_{long}_{speed}.jpg"""
    try:
        # Remove extension and split by underscore
        name = os.path.splitext(os.path.basename(filename))[0]
        parts = name.split('_')
        
        # Expected format: 20251213_160530123456_Sleeping_12.3456_78.9012_50
        # Or: 20251213160530123456_Sleeping_12.3456_78.9012_50
        if len(parts) >= 5:
            # Last 4 parts are: status, lat, long, speed
            speed = float(parts[-1])
            longitude = float(parts[-2])
            latitude = float(parts[-3])
            driver_status = parts[-4]
            return driver_status, latitude, longitude, speed
    except Exception as e:
        print(f"Error parsing filename metadata: {e}")
    
    # Return defaults if parsing fails
    return "NoFace", 0.0, 0.0, 0.0

def upload_image(path):
    try:
        # Check if file exists and is not empty
        if not os.path.exists(path):
            print(f"File not found: {path}")
            return
        
        file_size = os.path.getsize(path)
        print(f"File size: {file_size} bytes for {path}")
        if file_size == 0:
            print(f"Skipping empty file: {path}")
            os.remove(path)  # Remove empty file
            return
        
        # Skip files that are too small (likely corrupted)
        if file_size < 1000:
            print(f"Skipping suspiciously small file ({file_size} bytes): {path}")
            os.remove(path)
            return
        
        # Parse metadata from filename
        driver_status, latitude, longitude, speed = parse_filename_metadata(path)
        
        # Ensure driver_status is never empty (backend defaults to NoFace anyway)
        if not driver_status or driver_status.strip() == "":
            driver_status = "NoFace"
        
        with open(path, "rb") as f:
            image_data = f.read()
        
        # Skip if image data is empty
        if not image_data or len(image_data) == 0:
            print(f"Skipping file with no data: {path}")
            os.remove(path)
            return
            
        encoded_image = base64.b64encode(image_data).decode()
        
        # Skip if encoded image is empty
        if not encoded_image:
            print(f"Skipping file with empty encoding: {path}")
            os.remove(path)
            return

        payload = {
            "device_id": DEVICE_ID,
            "file_name": encoded_image,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "driver_status": driver_status,
            "latitude": latitude,
            "longitude": longitude,
            "speed": speed
        }

        # Try upload with current token
        r = requests.post(URL, json=payload, headers=get_headers(), timeout=10)

        # Handle token expiration - retry once with refreshed token
        if r.status_code == 401 or "Invalid or expired token" in r.text:
            print(f"Token expired, refreshing and retrying...")
            force_token_refresh()
            r = requests.post(URL, json=payload, headers=get_headers(), timeout=10)

        if r.status_code == 201:
            os.remove(path)
            print(f"Uploaded: {path} (status={driver_status}, lat={latitude}, long={longitude}, speed={speed})")
        else:
            print(f"Upload failed for {path}: {r.text}")
            # If file is corrupted, remove it to avoid infinite retry
            if "FileName" in r.text and "required" in r.text:
                print(f"Removing corrupted file: {path}")
                os.remove(path)

    except Exception as e:
        print(f"Error uploading {path}: {e}")

def process_pending_images():
    """Scan pending folder and upload all images"""
    patterns = ['*.jpg', '*.jpeg', '*.png']
    
    for pattern in patterns:
        for filepath in glob.glob(os.path.join(PENDING_DIR, pattern)):
            upload_image(filepath)


def parse_event_metadata(event_folder):
    """
    Parse event metadata from event_meta.txt file.
    
    Returns:
        dict with event_id, event_type, lat, long, speed, start_time, end_time
    """
    metadata = {
        'event_id': os.path.basename(event_folder),
        'event_type': 'Unknown',
        'lat': '0',
        'long': '0',
        'speed': '0',
        'start_time': '',
        'end_time': '',
        'frame_count': 0
    }
    
    meta_file = os.path.join(event_folder, 'event_meta.txt')
    if os.path.exists(meta_file):
        try:
            with open(meta_file, 'r') as f:
                for line in f:
                    if '=' in line:
                        key, value = line.strip().split('=', 1)
                        if key in metadata:
                            metadata[key] = value
        except Exception as e:
            print(f"Error reading event metadata: {e}")
    
    return metadata


def upload_event_folder(event_folder):
    """
    Upload an event folder containing frames for GIF generation.
    Optimized for Pi Zero 2W: processes frames in batches to reduce RAM usage.
    
    Args:
        event_folder: Path to the event folder containing frame_*.jpg files
    """
    try:
        # Check if folder exists
        if not os.path.isdir(event_folder):
            print(f"Event folder not found: {event_folder}")
            return False
        
        # Get all frame files sorted
        frame_files = sorted(glob.glob(os.path.join(event_folder, 'frame_*.jpg')))
        
        if not frame_files:
            print(f"No frames found in event folder: {event_folder}")
            # Remove empty event folder
            shutil.rmtree(event_folder)
            return False
        
        # Limit max frames to prevent RAM overflow on Pi Zero 2W
        MAX_FRAMES = 60  # ~12 seconds at 5 FPS
        if len(frame_files) > MAX_FRAMES:
            print(f"Limiting frames from {len(frame_files)} to {MAX_FRAMES}")
            frame_files = frame_files[:MAX_FRAMES]
        
        # Parse event metadata
        metadata = parse_event_metadata(event_folder)
        
        # Encode frames in batches to reduce peak RAM usage
        frames_base64 = []
        for i in range(0, len(frame_files), FRAME_BATCH_SIZE):
            batch = frame_files[i:i + FRAME_BATCH_SIZE]
            for frame_file in batch:
                try:
                    with open(frame_file, 'rb') as f:
                        frame_data = f.read()
                        if frame_data and len(frame_data) > 1000:  # Skip corrupted frames
                            frames_base64.append(base64.b64encode(frame_data).decode('utf-8'))
                        # Clear frame_data immediately
                        del frame_data
                except Exception as e:
                    print(f"Error reading frame {frame_file}: {e}")
            # Force garbage collection after each batch
            gc.collect()
        
        if not frames_base64:
            print(f"No valid frames in event folder: {event_folder}")
            shutil.rmtree(event_folder)
            return False
        
        # Prepare payload
        payload = {
            "device_id": DEVICE_ID,
            "event_id": metadata['event_id'],
            "event_type": metadata['event_type'],
            "frames": frames_base64,
            "frame_count": len(frames_base64),
            "start_time": metadata.get('start_time', ''),
            "end_time": metadata.get('end_time', ''),
            "lat": metadata.get('lat', '0'),
            "long": metadata.get('long', '0'),
            "speed": metadata.get('speed', '0'),
        }
        
        log_info(f"Uploading event {metadata['event_id']}: {len(frames_base64)} frames, type={metadata['event_type']}")
        
        # Upload to server
        r = requests.post(EVENT_UPLOAD_URL, json=payload, headers=get_headers(), timeout=60)
        
        # Handle token expiration
        if r.status_code == 401 or "Invalid or expired token" in r.text:
            print(f"Token expired, refreshing and retrying...")
            force_token_refresh()
            r = requests.post(EVENT_UPLOAD_URL, json=payload, headers=get_headers(), timeout=60)
        
        # Clear payload from memory before checking result
        upload_success = r.status_code == 201
        response_text = r.text
        del payload
        del frames_base64
        gc.collect()
        
        if upload_success:
            # Success - remove event folder
            shutil.rmtree(event_folder)
            log_info(f"Event uploaded successfully: {metadata['event_id']}")
            return True
        else:
            log_error(f"Event upload failed: {r.status_code} - {response_text}")
            return False
            
    except Exception as e:
        log_error(f"Error uploading event {event_folder}: {e}")
        return False
    finally:
        # Always run garbage collection after processing
        gc.collect()


def process_event_folders():
    """
    Scan events folder and upload completed event folders.
    Event folders are created by event_capture.py after event completion.
    """
    if not os.path.exists(EVENTS_DIR):
        return
    
    # Get all event folders (directories in events folder)
    for item in os.listdir(EVENTS_DIR):
        event_folder = os.path.join(EVENTS_DIR, item)
        
        # Skip if not a directory
        if not os.path.isdir(event_folder):
            continue
        
        # Check if event_meta.txt exists (indicates event is complete)
        meta_file = os.path.join(event_folder, 'event_meta.txt')
        if not os.path.exists(meta_file):
            # Event still in progress, skip
            continue
        
        # Upload the event folder
        upload_event_folder(event_folder)


if __name__ == "__main__":
    # Ensure directories exist
    os.makedirs(PENDING_DIR, exist_ok=True)
    os.makedirs(EVENTS_DIR, exist_ok=True)
    
    print(f"Watching for uploads (polling every {POLL_INTERVAL}s)...")
    print(f"  - Single images: {PENDING_DIR}")
    print(f"  - Event folders: {EVENTS_DIR}")
    
    while True:
        try:
            # Process single images (legacy mode)
            # process_pending_images()
            
            # Process event folders (GIF generation mode)
            process_event_folders()
            
        except Exception as e:
            print(f"Error processing uploads: {e}")
        
        time.sleep(POLL_INTERVAL)
