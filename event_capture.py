"""
Event-Based Frame Buffer & GIF Generation - Device Side

This module implements:
1. Circular frame buffer (deque) in RAM
2. Event state machine: IDLE → EVENT_ACTIVE → POST_EVENT → UPLOAD
3. Pre-event (2.5s) and post-event (1s) frame capture
4. Event folder structure for batch upload
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
from log import log_info, log_error


class EventState(Enum):
    """Event capture state machine states"""
    IDLE = "idle"
    EVENT_ACTIVE = "event_active"
    POST_EVENT = "post_event"
    UPLOAD = "upload"


@dataclass
class FrameData:
    """Container for frame with metadata - stores compressed JPEG bytes to save RAM"""
    frame_bytes: bytes  # JPEG compressed bytes (~30KB vs ~900KB raw)
    timestamp: float  # time.time()
    datetime_str: str  # formatted datetime string
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
    
    Features:
    - Circular buffer stores last N frames (pre-event buffer)
    - On event trigger: copies pre-event frames + captures during event + post-event frames
    - Saves event frames to folder structure
    - Triggers upload after event completion
    """
    
    # Configuration - optimized for Pi Zero 2W (512MB RAM)
    FPS = 5  # Expected frames per second
    PRE_EVENT_SECONDS = 2.0  # Reduced from 2.5s
    POST_EVENT_SECONDS = 1.0
    MAX_EVENT_SECONDS = 10.0  # Force complete after 10 seconds (50 frames max)
    BUFFER_SIZE = int(FPS * 1)  # 1 second buffer (5 frames) - minimal for Pi Zero 2W
    JPEG_QUALITY = 50  # Lower quality for smaller files (50 instead of 60)
    
    # NoFace: save only 1 frame per minute (not as full event)
    NOFACE_INTERVAL_SECONDS = 60
    
    def __init__(self, base_events_path: str, upload_callback=None):
        """
        Initialize the event frame buffer.
        
        Args:
            base_events_path: Base path for storing event folders
            upload_callback: Function to call when event is ready for upload
        """
        self.base_events_path = base_events_path
        self.upload_callback = upload_callback
        
        # Circular buffer for pre-event frames
        self.frame_buffer: Deque[FrameData] = deque(maxlen=self.BUFFER_SIZE)
        
        # Current state
        self.state = EventState.IDLE
        self.current_event: Optional[EventData] = None
        
        # Timing
        self.event_end_time: Optional[float] = None
        self.last_critical_status: Optional[str] = None
        
        # NoFace tracking - save only 1 frame per minute
        self.last_noface_save_time: float = 0
        
        # Thread safety
        self.lock = threading.Lock()
        
        # Create base events directory
        os.makedirs(base_events_path, exist_ok=True)
        
        log_info(f"EventFrameBuffer initialized: buffer_size={self.BUFFER_SIZE}, pre_event={self.PRE_EVENT_SECONDS}s, post_event={self.POST_EVENT_SECONDS}s")
    
    def add_frame(self, frame, speed: str, lat: str, long: str, acc: str, driver_status: str):
        """
        Add a frame to the buffer. Called for every captured frame.
        
        Args:
            frame: OpenCV frame
            speed: Current speed
            lat: Latitude
            long: Longitude
            acc: Acceleration
            driver_status: Current driver status (Active, Sleeping, Yawning, NoFace, etc.)
        """
        current_time = time.time()
        
        # Compress frame to JPEG bytes (~30KB vs ~900KB raw) - critical for Pi Zero 2W
        encode_param = [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]
        _, encoded = cv2.imencode('.jpg', frame, encode_param)
        frame_bytes = encoded.tobytes()
        
        frame_data = FrameData(
            frame_bytes=frame_bytes,  # Store compressed bytes to save RAM
            timestamp=current_time,
            datetime_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            speed=speed,
            lat=lat,
            long=long,
            acceleration=acc,
            driver_status=driver_status
        )
        
        with self.lock:
            # Always add to circular buffer (for pre-event capture)
            self.frame_buffer.append(frame_data)
            
            # Separate NoFace from critical events (Sleeping/Yawning)
            is_noface = driver_status in ['NoFace', 'No Face']
            is_critical = driver_status in ['Sleeping', 'Yawning', 
                                            'Sleeping/Looking Down', 'Yawning/Fatigued']
            
            # Handle NoFace separately - save only 1 frame per minute
            if is_noface:
                self._handle_noface(frame_data, current_time)
                return
            
            if self.state == EventState.IDLE:
                if is_critical:
                    self._start_event(driver_status, current_time)
                    
            elif self.state == EventState.EVENT_ACTIVE:
                if is_critical:
                    # Continue capturing during event
                    self.current_event.frames.append(frame_data)
                    self.last_critical_status = driver_status
                    
                    # Force complete if event exceeds max duration (prevent RAM overflow)
                    if current_time - self.current_event.start_time >= self.MAX_EVENT_SECONDS:
                        log_info(f"Event {self.current_event.event_id} exceeded max duration ({self.MAX_EVENT_SECONDS}s), force completing")
                        self._transition_to_post_event(current_time)
                else:
                    # Event ended, start post-event capture
                    self._transition_to_post_event(current_time)
                    
            elif self.state == EventState.POST_EVENT:
                # Capture post-event frames
                self.current_event.frames.append(frame_data)
                
                # Check if post-event period is complete
                if current_time - self.event_end_time >= self.POST_EVENT_SECONDS:
                    self._complete_event()
    
    def _start_event(self, driver_status: str, current_time: float):
        """Start a new event capture"""
        event_id = f"evt_{uuid.uuid4().hex[:8]}"
        
        # Map status to clean event type
        event_type = self._map_status_to_event_type(driver_status)
        
        self.current_event = EventData(
            event_id=event_id,
            event_type=event_type,
            start_time=current_time,
            frames=[]
        )
        
        # Copy pre-event frames from buffer
        pre_event_cutoff = current_time - self.PRE_EVENT_SECONDS
        for frame_data in self.frame_buffer:
            if frame_data.timestamp >= pre_event_cutoff:
                self.current_event.frames.append(frame_data)
        
        self.state = EventState.EVENT_ACTIVE
        self.last_critical_status = driver_status
        
        log_info(f"Event started: {event_id}, type={event_type}, pre_frames={len(self.current_event.frames)}")
    
    def _handle_noface(self, frame_data: FrameData, current_time: float):
        """Handle NoFace - save only 1 frame per minute (not as full event)"""
        time_since_last = current_time - self.last_noface_save_time
        
        if time_since_last < self.NOFACE_INTERVAL_SECONDS:
            # Skip - not enough time since last NoFace save
            return
        
        # Save single NoFace frame
        self.last_noface_save_time = current_time
        
        event_id = f"noface_{uuid.uuid4().hex[:8]}"
        noface_event = EventData(
            event_id=event_id,
            event_type='NoFace',
            start_time=current_time,
            end_time=current_time,
            frames=[frame_data]  # Only 1 frame
        )
        
        # Create folder and save immediately
        folder_name = noface_event.get_folder_name()
        noface_event.folder_path = os.path.join(self.base_events_path, folder_name)
        os.makedirs(noface_event.folder_path, exist_ok=True)
        
        log_info(f"NoFace captured: {event_id} (1 frame, next in {self.NOFACE_INTERVAL_SECONDS}s)")
        
        # Save in background thread
        save_thread = threading.Thread(
            target=self._save_event_frames,
            args=(noface_event,)
        )
        save_thread.start()
    
    def _transition_to_post_event(self, current_time: float):
        """Transition from active event to post-event capture"""
        self.event_end_time = current_time
        self.current_event.end_time = current_time
        self.state = EventState.POST_EVENT
        
        log_info(f"Event {self.current_event.event_id} ended, capturing post-event frames for {self.POST_EVENT_SECONDS}s")
    
    def _complete_event(self):
        """Complete the event and trigger save/upload"""
        if not self.current_event:
            return
        
        event = self.current_event
        
        # Create event folder
        folder_name = event.get_folder_name()
        event.folder_path = os.path.join(self.base_events_path, folder_name)
        os.makedirs(event.folder_path, exist_ok=True)
        
        log_info(f"Event {event.event_id} complete: {len(event.frames)} frames, saving to {event.folder_path}")
        
        # Save frames in background thread
        save_thread = threading.Thread(
            target=self._save_event_frames,
            args=(event,)
        )
        save_thread.start()
        
        # Reset state
        self.state = EventState.IDLE
        self.current_event = None
        self.event_end_time = None
        self.last_critical_status = None
    
    def _save_event_frames(self, event: EventData):
        """Save event frames to disk (lightweight: write raw JPEG bytes, skip annotation to save CPU)"""
        try:
            # Lower thread priority to avoid stealing CPU from MediaPipe inference
            try:
                os.nice(10)
            except OSError:
                pass

            frame_paths = []

            for i, frame_data in enumerate(event.frames):
                filename = f"frame_{i:04d}.jpg"
                filepath = os.path.join(event.folder_path, filename)

                # Write raw JPEG bytes directly — no decode/annotate/re-encode cycle
                # Saves ~5-10ms CPU per frame on Pi Zero 2W (vs decode+draw+encode)
                with open(filepath, 'wb') as f:
                    f.write(frame_data.frame_bytes)
                frame_paths.append(filepath)

            # Save event metadata (contains all info that was previously annotated on frames)
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

            # Clear frames from memory after saving to free RAM
            event.frames.clear()

            # Trigger upload callback
            if self.upload_callback:
                self.upload_callback(event)

        except Exception as e:
            log_error(f"Error saving event {event.event_id}: {str(e)}")
    
    def _annotate_frame(self, frame_data: FrameData):
        """Add overlay text to frame"""
        # Decode JPEG bytes back to frame
        nparr = np.frombuffer(frame_data.frame_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if frame is None:
            return None
        
        h, w = frame.shape[:2]
        
        # Font settings
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.4
        thickness = 1
        
        # Driver status overlay (top-left)
        status_text = f"Status: {frame_data.driver_status}"
        (tw, th), _ = cv2.getTextSize(status_text, font, font_scale, thickness)
        cv2.rectangle(frame, (10, 10), (20 + tw, 20 + th), (139, 104, 0), -1)
        cv2.putText(frame, status_text, (15, 15 + th), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
        
        # Footer overlay
        # Format lat/long to 4 decimal places to fit speed in frame
        try:
            lat_formatted = f"{float(frame_data.lat):.4f}"
            long_formatted = f"{float(frame_data.long):.4f}"
        except (ValueError, TypeError):
            lat_formatted = frame_data.lat
            long_formatted = frame_data.long
        footer_texts = [
            "Sapience Automata 2025",
            f"Time:{frame_data.datetime_str}",
            f"Lat,Long:{lat_formatted},{long_formatted}",
            f"Speed:{frame_data.speed} Km/h"
        ]
        cv2.rectangle(frame, (0, h - 30), (w, h), (255, 0, 0), -1)
        x = 5
        for text in footer_texts:
            cv2.putText(frame, text, (x, h - 10), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
            (tw, _), _ = cv2.getTextSize(text, font, font_scale, thickness)
            x += tw + 10
        
        return frame
    
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
                self._complete_event()


class EventUploader:
    """
    Handles uploading event folders to the server.
    
    Features:
    - Uploads entire event folder after completion
    - Sends frames as base64 array
    - Cleans up after successful upload
    """
    
    def __init__(self, device_id: str, auth_token: str, api_base_url: str = "https://api.copilotai.click"):
        self.device_id = device_id
        self.auth_token = auth_token
        self.api_base_url = api_base_url
        self.upload_queue = []
        self.lock = threading.Lock()
    
    def upload_event(self, event: EventData):
        """
        Upload an event to the server.
        
        Args:
            event: EventData with saved frames
        """
        if not event.folder_path or not os.path.exists(event.folder_path):
            log_error(f"Event folder not found: {event.folder_path}")
            return
        
        try:
            import requests
            
            # Collect all frame files
            frame_files = sorted([
                f for f in os.listdir(event.folder_path) 
                if f.startswith('frame_') and f.endswith('.jpg')
            ])
            
            if not frame_files:
                log_error(f"No frames found in event folder: {event.folder_path}")
                return
            
            # Encode frames to base64
            frames_base64 = []
            for frame_file in frame_files:
                frame_path = os.path.join(event.folder_path, frame_file)
                with open(frame_path, 'rb') as f:
                    frames_base64.append(base64.b64encode(f.read()).decode('utf-8'))
            
            # Read metadata
            metadata = {}
            meta_path = os.path.join(event.folder_path, "event_meta.txt")
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    for line in f:
                        if '=' in line:
                            key, value = line.strip().split('=', 1)
                            metadata[key] = value
            
            # Prepare payload
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
            
            # Upload to server
            url = f"{self.api_base_url}/api/upload-event"
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            
            if response.status_code == 201:
                log_info(f"Event {event.event_id} uploaded successfully ({len(frames_base64)} frames)")
                
                # Clean up local folder after successful upload
                shutil.rmtree(event.folder_path)
                log_info(f"Cleaned up event folder: {event.folder_path}")
            else:
                log_error(f"Event upload failed: {response.status_code} - {response.text}")
                
        except Exception as e:
            log_error(f"Error uploading event {event.event_id}: {str(e)}")


# Global instance for use in main script
_event_buffer: Optional[EventFrameBuffer] = None
_event_uploader: Optional[EventUploader] = None


def init_event_capture(base_events_path: str, device_id: str, auth_token: str):
    """
    Initialize the event capture system.
    
    Args:
        base_events_path: Path to store event folders
        device_id: Device ID for API calls
        auth_token: Auth token for API calls
    """
    global _event_buffer, _event_uploader
    
    # upload_images.py service handles uploads via /api/events/upload
    # Don't pass upload_callback here to avoid double-uploading events
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
