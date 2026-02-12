"""
Event-Based Frame Buffer & GIF Generation - Device Side

This module implements:
1. Circular frame buffer (deque) in RAM
2. Event state machine: IDLE → EVENT_ACTIVE → POST_EVENT → SAVING
3. Pre-event (2s) and post-event (1s) frame capture
4. Single save worker thread with queue (prevents SD card I/O storms)
5. Event cooldown to prevent rapid start/stop cycling
"""

import os
import cv2
import time
import uuid
import shutil
import threading
import base64
import numpy as np
from collections import deque
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Deque
from queue import Queue
from log import log_info, log_error


class EventState(Enum):
    """Event capture state machine states"""
    IDLE = "idle"
    EVENT_ACTIVE = "event_active"
    POST_EVENT = "post_event"


@dataclass
class FrameData:
    """Container for frame with metadata.
    Stores raw numpy bytes — JPEG encoding happens in the save worker thread,
    NOT on the main detection thread. At 320x240, each frame is ~225KB raw."""
    frame_raw: bytes       # Raw numpy frame bytes (tobytes)
    frame_shape: tuple     # (h, w, channels) for reconstruction
    timestamp: float
    datetime_str: str
    speed: str
    lat: str
    long: str
    acceleration: str
    driver_status: str


@dataclass
class EventData:
    """Container for a complete event"""
    event_id: str
    event_type: str  # Sleeping, Yawning, NoFace, etc.
    start_time: float
    end_time: Optional[float] = None
    frames: List[FrameData] = field(default_factory=list)
    folder_path: Optional[str] = None

    def get_folder_name(self) -> str:
        """Generate folder name: timestamp_event_id"""
        dt = datetime.fromtimestamp(self.start_time)
        return f"{dt.strftime('%Y%m%d_%H%M%S')}_{self.event_id}"


class EventFrameBuffer:
    """
    Manages circular frame buffer and event capture logic.

    Event flow: 2 pre-event frames + during-event frames + 2 post-event frames.
    All stored as one event folder on disk, then uploaded by upload_images.py.

    Key design for Pi Zero 2W performance:
    - add_frame() does ZERO JPEG encoding — just stores raw numpy bytes (~0.1ms)
    - JPEG encoding happens in a single persistent save worker thread
    - Single save worker serializes SD card writes (no concurrent I/O)
    """

    # Configuration - optimized for Pi Zero 2W (512MB RAM)
    PRE_EVENT_FRAMES = 2            # Frames to keep before event starts
    POST_EVENT_FRAMES = 2           # Frames to capture after event ends
    MAX_EVENT_SECONDS = 10.0        # Force complete after 10 seconds
    BUFFER_SIZE = PRE_EVENT_FRAMES + 2  # Circular buffer (slightly larger than needed)
    JPEG_QUALITY = 40               # Used in save worker, not main thread
    EVENT_COOLDOWN = 3.0            # Seconds between events
    POST_EVENT_TIMEOUT = 5.0        # Force complete POST_EVENT if frames stop arriving

    # NoFace: save only 1 frame per minute
    NOFACE_INTERVAL_SECONDS = 60

    # Font settings for footer overlay
    FONT = cv2.FONT_HERSHEY_SIMPLEX
    FONT_SCALE = 0.35
    FONT_THICKNESS = 1
    SPEED_FONT_SCALE = 0.8
    SPEED_FONT_THICKNESS = 2

    def __init__(self, base_events_path: str, upload_callback=None):
        self.base_events_path = base_events_path
        self.upload_callback = upload_callback

        # Circular buffer for pre-event frames
        self.frame_buffer: Deque[FrameData] = deque(maxlen=self.BUFFER_SIZE)

        # Current state
        self.state = EventState.IDLE
        self.current_event: Optional[EventData] = None

        # Timing / counters
        self.event_end_time: Optional[float] = None
        self.post_event_count: int = 0
        self.last_critical_status: Optional[str] = None
        self.last_event_complete_time: float = 0  # For cooldown

        # NoFace tracking
        self.last_noface_save_time: float = 0

        # Thread safety for state machine
        self.lock = threading.Lock()

        # Single persistent save worker — serializes all SD card I/O
        self._save_queue: Queue = Queue(maxsize=10)
        self._save_worker = threading.Thread(target=self._save_worker_loop, daemon=True)
        self._save_worker.start()

        os.makedirs(base_events_path, exist_ok=True)
        log_info(f"EventFrameBuffer initialized: pre={self.PRE_EVENT_FRAMES}, post={self.POST_EVENT_FRAMES}, cooldown={self.EVENT_COOLDOWN}s")

    def add_frame(self, frame, speed: str, lat: str, long: str, acc: str, driver_status: str):
        """Add a frame to the buffer. Called from main detection loop at ~2 FPS."""
        current_time = time.time()

        frame_data = FrameData(
            frame_raw=frame.tobytes(),
            frame_shape=frame.shape,
            timestamp=current_time,
            datetime_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            speed=speed,
            lat=lat,
            long=long,
            acceleration=acc,
            driver_status=driver_status
        )

        with self.lock:
            # Always add to circular buffer (for pre-event frames)
            self.frame_buffer.append(frame_data)

            is_noface = driver_status in ['NoFace', 'No Face']
            is_critical = driver_status in ['Sleeping', 'Yawning',
                                            'Sleeping/Looking Down', 'Yawning/Fatigued']

            # --- IDLE ---
            if self.state == EventState.IDLE:
                if is_noface:
                    self._handle_noface(frame_data, current_time)
                elif is_critical:
                    if current_time - self.last_event_complete_time < self.EVENT_COOLDOWN:
                        return
                    self._start_event(driver_status, current_time)

            # --- EVENT_ACTIVE ---
            elif self.state == EventState.EVENT_ACTIVE:
                if is_critical:
                    self.current_event.frames.append(frame_data)
                    self.last_critical_status = driver_status
                    # Force complete if too long
                    if current_time - self.current_event.start_time >= self.MAX_EVENT_SECONDS:
                        log_info(f"Event {self.current_event.event_id} max duration reached, completing")
                        self._transition_to_post_event(current_time)
                else:
                    # Not critical anymore (Active or NoFace) — start post-event capture
                    self._transition_to_post_event(current_time)

            # --- POST_EVENT ---
            elif self.state == EventState.POST_EVENT:
                self.current_event.frames.append(frame_data)
                self.post_event_count += 1
                # Complete after collecting enough post-event frames
                if self.post_event_count >= self.POST_EVENT_FRAMES:
                    self._complete_event(current_time)
                # Safety: force complete if post-event takes too long (frames stopped arriving)
                elif current_time - self.event_end_time >= self.POST_EVENT_TIMEOUT:
                    log_info(f"Event {self.current_event.event_id} post-event timeout, completing with {self.post_event_count} post frames")
                    self._complete_event(current_time)

    def _start_event(self, driver_status: str, current_time: float):
        """Start a new event — grab last N pre-event frames from buffer"""
        event_id = f"evt_{uuid.uuid4().hex[:8]}"
        event_type = self._map_status_to_event_type(driver_status)

        self.current_event = EventData(
            event_id=event_id,
            event_type=event_type,
            start_time=current_time,
            frames=[]
        )

        # Copy last PRE_EVENT_FRAMES from circular buffer as pre-event context
        buffer_list = list(self.frame_buffer)
        pre_frames = buffer_list[-self.PRE_EVENT_FRAMES:] if len(buffer_list) >= self.PRE_EVENT_FRAMES else buffer_list
        self.current_event.frames.extend(pre_frames)

        self.state = EventState.EVENT_ACTIVE
        self.last_critical_status = driver_status
        log_info(f"Event started: {event_id}, type={event_type}, pre_frames={len(pre_frames)}")

    def _handle_noface(self, frame_data: FrameData, current_time: float):
        """Handle NoFace when IDLE — save 1 frame per minute"""
        if current_time - self.last_noface_save_time < self.NOFACE_INTERVAL_SECONDS:
            return

        self.last_noface_save_time = current_time
        event_id = f"noface_{uuid.uuid4().hex[:8]}"
        noface_event = EventData(
            event_id=event_id,
            event_type='NoFace',
            start_time=current_time,
            end_time=current_time,
            frames=[frame_data]
        )

        folder_name = noface_event.get_folder_name()
        noface_event.folder_path = os.path.join(self.base_events_path, folder_name)
        log_info(f"NoFace captured: {event_id} (1 frame, next in {self.NOFACE_INTERVAL_SECONDS}s)")
        self._enqueue_save(noface_event)

    def _transition_to_post_event(self, current_time: float):
        """Driver no longer critical — start collecting post-event frames"""
        self.event_end_time = current_time
        self.current_event.end_time = current_time
        self.post_event_count = 0
        self.state = EventState.POST_EVENT
        log_info(f"Event {self.current_event.event_id} ended, capturing {self.POST_EVENT_FRAMES} post-event frames")

    def _complete_event(self, current_time: float):
        """Event done — queue for save worker"""
        if not self.current_event:
            return

        event = self.current_event
        folder_name = event.get_folder_name()
        event.folder_path = os.path.join(self.base_events_path, folder_name)

        log_info(f"Event {event.event_id} complete: {len(event.frames)} frames (pre={self.PRE_EVENT_FRAMES}+during+post={self.post_event_count}), saving to {event.folder_path}")

        self._enqueue_save(event)

        # Reset state
        self.state = EventState.IDLE
        self.current_event = None
        self.event_end_time = None
        self.post_event_count = 0
        self.last_critical_status = None
        self.last_event_complete_time = current_time

    def _enqueue_save(self, event: EventData):
        """Queue an event for the save worker. Drop if queue full (backpressure)."""
        try:
            self._save_queue.put_nowait(event)
        except Exception:
            log_error(f"Save queue full, dropping event {event.event_id}")
            event.frames.clear()

    def _save_worker_loop(self):
        """Persistent save worker thread. Serializes all SD card I/O.
        Runs at lowest priority to avoid stealing CPU from detection."""
        try:
            os.nice(10)
        except OSError:
            pass

        while True:
            try:
                event = self._save_queue.get()
                self._save_event_frames(event)
            except Exception as e:
                log_error(f"Save worker error: {e}")

    def _save_event_frames(self, event: EventData):
        """Save event frames to disk. JPEG encoding happens HERE, not main thread."""
        try:
            os.makedirs(event.folder_path, exist_ok=True)
            frame_paths = []
            encode_param = [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]

            for i, frame_data in enumerate(event.frames):
                filename = f"frame_{i:04d}.jpg"
                filepath = os.path.join(event.folder_path, filename)

                # Reconstruct numpy array from raw bytes (copy needed for cv2 drawing)
                frame = np.frombuffer(frame_data.frame_raw, dtype=np.uint8).reshape(frame_data.frame_shape).copy()

                # Draw speed on upper right corner (big font)
                h, w = frame.shape[:2]
                lat_r = round(float(frame_data.lat), 4) if frame_data.lat else 0.0
                lon_r = round(float(frame_data.long), 4) if frame_data.long else 0.0
                speed_text = f"{frame_data.speed} Km/h"
                (sw, sh), _ = cv2.getTextSize(speed_text, self.FONT, self.SPEED_FONT_SCALE, self.SPEED_FONT_THICKNESS)
                cv2.rectangle(frame, (w - sw - 16, 0), (w, sh + 16), (0, 0, 0), -1)
                cv2.putText(frame, speed_text, (w - sw - 8, sh + 8), self.FONT, self.SPEED_FONT_SCALE, (255, 255, 255), self.SPEED_FONT_THICKNESS, cv2.LINE_AA)

                # Draw footer overlay (no speed — shown above)
                footer_text = [
                    "Sapience Automata 2025",
                    f"Time:{frame_data.datetime_str}",
                    f"Lat,Long:{lat_r},{lon_r}"
                ]
                cv2.rectangle(frame, (0, h - 24), (w, h), (255, 0, 0), -1)
                x = 10
                for text in footer_text:
                    cv2.putText(frame, text, (x, h - 8), self.FONT, self.FONT_SCALE, (255, 255, 255), self.FONT_THICKNESS, cv2.LINE_AA)
                    (tw, _), _ = cv2.getTextSize(text, self.FONT, self.FONT_SCALE, self.FONT_THICKNESS)
                    x += tw + 10

                _, encoded = cv2.imencode('.jpg', frame, encode_param)
                with open(filepath, 'wb') as f:
                    f.write(encoded.tobytes())
                frame_paths.append(filepath)

                # Yield between frames to avoid hogging I/O
                time.sleep(0.01)

            # Save metadata
            metadata_path = os.path.join(event.folder_path, "event_meta.txt")
            with open(metadata_path, 'w') as f:
                f.write(f"event_id={event.event_id}\n")
                f.write(f"event_type={event.event_type}\n")
                f.write(f"start_time={event.start_time}\n")
                f.write(f"end_time={event.end_time}\n")
                f.write(f"frame_count={len(event.frames)}\n")
                if event.frames:
                    f.write(f"lat={event.frames[0].lat}\n")
                    f.write(f"long={event.frames[0].long}\n")
                    f.write(f"speed={event.frames[0].speed}\n")

            log_info(f"Event {event.event_id} saved: {len(frame_paths)} frames")

            # Free RAM
            event.frames.clear()

            if self.upload_callback:
                self.upload_callback(event)

        except Exception as e:
            log_error(f"Error saving event {event.event_id}: {e}")

    def _map_status_to_event_type(self, status: str) -> str:
        """Map driver status to clean event type"""
        status_map = {
            'Sleeping/Looking Down': 'Sleeping',
            'Yawning/Fatigued': 'Yawning',
            'No Face': 'NoFace',
            'Sleeping': 'Sleeping',
            'Yawning': 'Yawning',
            'NoFace': 'NoFace',
        }
        return status_map.get(status, status)

    def force_complete_event(self):
        """Force complete current event (e.g., on shutdown)"""
        with self.lock:
            if self.current_event and self.state in [EventState.EVENT_ACTIVE, EventState.POST_EVENT]:
                log_info(f"Force completing event {self.current_event.event_id}")
                self._complete_event(time.time())


class EventUploader:
    """
    Handles uploading event folders to the server.
    Uploads entire event folder after completion.
    """

    def __init__(self, device_id: str, auth_token: str, api_base_url: str = "https://api.copilotai.click"):
        self.device_id = device_id
        self.auth_token = auth_token
        self.api_base_url = api_base_url
        self.upload_queue = []
        self.lock = threading.Lock()

    def upload_event(self, event: EventData):
        """Upload an event to the server."""
        if not event.folder_path or not os.path.exists(event.folder_path):
            log_error(f"Event folder not found: {event.folder_path}")
            return

        try:
            import requests

            frame_files = sorted([
                f for f in os.listdir(event.folder_path)
                if f.startswith('frame_') and f.endswith('.jpg')
            ])

            if not frame_files:
                log_error(f"No frames found in event folder: {event.folder_path}")
                return

            frames_base64 = []
            for frame_file in frame_files:
                frame_path = os.path.join(event.folder_path, frame_file)
                with open(frame_path, 'rb') as f:
                    frames_base64.append(base64.b64encode(f.read()).decode('utf-8'))

            metadata = {}
            meta_path = os.path.join(event.folder_path, "event_meta.txt")
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    for line in f:
                        if '=' in line:
                            key, value = line.strip().split('=', 1)
                            metadata[key] = value

            payload = {
                "device_id": self.device_id,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "frames": frames_base64,
                "frame_count": len(frames_base64),
                "start_time": datetime.fromtimestamp(event.start_time).isoformat(),
                "end_time": datetime.fromtimestamp(event.end_time).isoformat() if event.end_time else None,
                "lat": metadata.get('lat', '0'),
                "long": metadata.get('long', '0'),
                "speed": metadata.get('speed', '0'),
            }

            headers = {
                'Authorization': f'Bearer {self.auth_token}',
                'Content-Type': 'application/json'
            }

            url = f"{self.api_base_url}/api/upload-event"
            response = requests.post(url, json=payload, headers=headers, timeout=60)

            if response.status_code == 201:
                log_info(f"Event {event.event_id} uploaded successfully ({len(frames_base64)} frames)")
                shutil.rmtree(event.folder_path)
                log_info(f"Cleaned up event folder: {event.folder_path}")
            else:
                log_error(f"Event upload failed: {response.status_code} - {response.text}")

        except Exception as e:
            log_error(f"Error uploading event {event.event_id}: {e}")


# Global instance
_event_buffer: Optional[EventFrameBuffer] = None
_event_uploader: Optional[EventUploader] = None


def init_event_capture(base_events_path: str, device_id: str, auth_token: str):
    """Initialize the event capture system."""
    global _event_buffer, _event_uploader

    _event_uploader = EventUploader(device_id, auth_token)
    _event_buffer = EventFrameBuffer(
        base_events_path=base_events_path,
        upload_callback=None
    )

    log_info("Event capture system initialized")
    return _event_buffer


def get_event_buffer() -> Optional[EventFrameBuffer]:
    """Get the global event buffer instance"""
    return _event_buffer


def shutdown_event_capture():
    """Shutdown the event capture system gracefully"""
    global _event_buffer
    if _event_buffer:
        _event_buffer.force_complete_event()
        log_info("Event capture system shutdown")
