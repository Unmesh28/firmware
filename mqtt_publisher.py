"""
MQTT Publisher for GPS and Driver Status data
Publishes real-time data to backend via MQTT for smooth live map updates
"""

import os
import time
import logging
import threading
from typing import Optional

import paho.mqtt.client as mqtt
import msgpack

logger = logging.getLogger(__name__)

# MQTT Configuration from environment
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'api.copilotai.click')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', '1883'))
MQTT_BROKER_PORT_TLS = int(os.getenv('MQTT_BROKER_PORT_TLS', '8883'))
MQTT_USE_TLS = os.getenv('MQTT_USE_TLS', '0').strip().lower() in {'1', 'true', 'yes'}
MQTT_USERNAME = os.getenv('MQTT_USERNAME', '')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', '')
MQTT_QOS = int(os.getenv('MQTT_QOS', '1'))

# Get device ID from database
try:
    from get_device_id import get_device_id_from_db
    DEVICE_ID = get_device_id_from_db()
except Exception:
    DEVICE_ID = os.getenv('DEVICE_ID', 'unknown')


class MQTTPublisher:
    """
    Simple MQTT publisher for GPS and status data
    
    Features:
    - Auto-reconnect with exponential backoff
    - Non-blocking publish
    - Thread-safe
    """
    
    def __init__(self, device_id: str = None):
        self.device_id = device_id or DEVICE_ID
        self._client_id = f"pi_gps_{self.device_id}"
        
        # MQTT client
        self._client = mqtt.Client(client_id=self._client_id, protocol=mqtt.MQTTv311)
        
        # Connection state
        self._connected = False
        self._connecting = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        
        # Topics
        self._gps_topic = f"device/gps/{self.device_id}"
        self._status_topic = f"device/status/{self.device_id}"
        self._alert_topic = f"device/alert/{self.device_id}"
        
        # Statistics
        self._messages_sent = 0
        self._messages_failed = 0
        
        # Thread lock
        self._lock = threading.Lock()
        
        # Setup callbacks
        self._setup_callbacks()
    
    def _setup_callbacks(self):
        """Setup MQTT client callbacks"""
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        
        # Authentication
        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        
        # TLS/SSL
        if MQTT_USE_TLS:
            import ssl
            self._client.tls_set(tls_version=ssl.PROTOCOL_TLS)
            self._client.tls_insecure_set(False)
    
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when connected to broker"""
        if rc == 0:
            print(f"MQTT connected to {MQTT_BROKER_HOST}")
            logger.info(f"MQTT connected to {MQTT_BROKER_HOST}")
            self._connected = True
            self._connecting = False
            self._reconnect_delay = 1
        else:
            print(f"MQTT connection failed with code: {rc}")
            logger.error(f"MQTT connection failed with code: {rc}")
            self._connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback when disconnected from broker"""
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT unexpected disconnect (code: {rc})")
            # Schedule reconnect
            self._schedule_reconnect()
    
    def _on_publish(self, client, userdata, mid):
        """Callback when message is published"""
        self._messages_sent += 1
    
    def _schedule_reconnect(self):
        """Schedule a reconnection attempt"""
        if self._connecting:
            return
        
        def reconnect():
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            self.connect()
        
        thread = threading.Thread(target=reconnect, daemon=True)
        thread.start()
    
    def connect(self) -> bool:
        """Connect to MQTT broker"""
        if self._connected:
            return True
        
        if self._connecting:
            return False
        
        with self._lock:
            self._connecting = True
        
        try:
            port = MQTT_BROKER_PORT_TLS if MQTT_USE_TLS else MQTT_BROKER_PORT
            
            print(f"Connecting to MQTT broker: {MQTT_BROKER_HOST}:{port}")
            logger.info(f"Connecting to MQTT broker: {MQTT_BROKER_HOST}:{port}")
            
            self._client.connect_async(MQTT_BROKER_HOST, port, keepalive=30)
            self._client.loop_start()
            print("MQTT loop started, waiting for connection...")
            
            # Wait for connection (max 5 seconds)
            for _ in range(50):
                if self._connected:
                    return True
                time.sleep(0.1)
            
            logger.error("MQTT connection timeout")
            self._connecting = False
            return False
            
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            self._connecting = False
            return False
    
    def disconnect(self):
        """Disconnect from MQTT broker"""
        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False
        logger.info("MQTT disconnected")
    
    def publish_gps(self, lat: float, lng: float, speed: float, acceleration: float = 0.0, 
                    heading: float = 0.0, driver_status: str = "Active") -> bool:
        """
        Publish GPS data
        
        Args:
            lat: Latitude
            lng: Longitude  
            speed: Speed in km/h
            acceleration: Acceleration in m/s²
            heading: Heading in degrees
            driver_status: Driver status string
            
        Returns:
            True if published successfully
        """
        if not self._connected:
            if not self.connect():
                self._messages_failed += 1
                return False
        
        try:
            # Create compact message using msgpack
            payload = msgpack.packb({
                "d": self.device_id,
                "t": int(time.time()),
                "la": int(lat * 1_000_000),      # 6 decimal precision
                "lo": int(lng * 1_000_000),
                "al": 0,                          # altitude
                "sp": int(speed * 10),            # 1 decimal precision
                "ac": int(acceleration * 100),    # 2 decimal precision
                "hd": int(heading),
                "sa": 0,                          # satellites
                "fq": 1,                          # fix quality
                "ds": driver_status,              # driver status
            }, use_bin_type=True)
            
            result = self._client.publish(
                self._gps_topic,
                payload,
                qos=MQTT_QOS,
                retain=True  # Retain last known position
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"GPS published: lat={lat:.6f}, lng={lng:.6f}, speed={speed:.1f}")
                return True
            else:
                logger.warning(f"GPS publish failed: {result.rc}")
                self._messages_failed += 1
                return False
                
        except Exception as e:
            logger.error(f"GPS publish error: {e}")
            self._messages_failed += 1
            return False
    
    def publish_status(self, status: str, lat: float = 0.0, lng: float = 0.0, 
                       speed: float = 0.0) -> bool:
        """
        Publish driver status
        
        Args:
            status: Status string (Active, Sleeping, Yawning, NoFace, etc.)
            lat: Latitude
            lng: Longitude
            speed: Speed in km/h
            
        Returns:
            True if published successfully
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            # Map status string to code
            status_map = {
                "Active": 1,
                "Sleeping": 2,
                "Yawning": 3,
                "NoFace": 4,
                "LookingDown": 5,
                "OverSpeeding": 6,
                "RashDriving": 7,
            }
            status_code = status_map.get(status, 1)
            
            payload = msgpack.packb({
                "d": self.device_id,
                "t": int(time.time()),
                "st": status_code,
                "la": int(lat * 1_000_000),
                "lo": int(lng * 1_000_000),
                "sp": int(speed * 10),
            }, use_bin_type=True)
            
            result = self._client.publish(
                self._status_topic,
                payload,
                qos=MQTT_QOS
            )
            
            return result.rc == mqtt.MQTT_ERR_SUCCESS
            
        except Exception as e:
            logger.error(f"Status publish error: {e}")
            return False
    
    def publish_alert(self, alert_type: str, message: str, lat: float, lng: float, 
                      speed: float) -> bool:
        """
        Publish alert
        
        Args:
            alert_type: Type of alert (Sleeping, Yawning, OverSpeeding, etc.)
            message: Alert message
            lat: Latitude
            lng: Longitude
            speed: Speed in km/h
            
        Returns:
            True if published successfully
        """
        if not self._connected:
            if not self.connect():
                return False
        
        try:
            payload = msgpack.packb({
                "d": self.device_id,
                "t": int(time.time()),
                "at": alert_type,
                "m": message,
                "la": int(lat * 1_000_000),
                "lo": int(lng * 1_000_000),
                "sp": int(speed * 10),
            }, use_bin_type=True)
            
            result = self._client.publish(
                self._alert_topic,
                payload,
                qos=2  # Exactly once for alerts
            )
            
            return result.rc == mqtt.MQTT_ERR_SUCCESS
            
        except Exception as e:
            logger.error(f"Alert publish error: {e}")
            return False
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to broker"""
        return self._connected
    
    @property
    def stats(self) -> dict:
        """Get publisher statistics"""
        return {
            "connected": self._connected,
            "messages_sent": self._messages_sent,
            "messages_failed": self._messages_failed,
        }


# Global singleton instance
_mqtt_publisher: Optional[MQTTPublisher] = None
_mqtt_lock = threading.Lock()


def get_mqtt_publisher() -> MQTTPublisher:
    """Get or create the global MQTT publisher instance"""
    global _mqtt_publisher
    
    if _mqtt_publisher is None:
        with _mqtt_lock:
            if _mqtt_publisher is None:
                _mqtt_publisher = MQTTPublisher()
                # Auto-connect on first access
                _mqtt_publisher.connect()
    
    return _mqtt_publisher


def publish_gps_mqtt(lat: float, lng: float, speed: float, acceleration: float = 0.0,
                     driver_status: str = "Active") -> bool:
    """
    Convenience function to publish GPS data via MQTT
    
    Args:
        lat: Latitude
        lng: Longitude
        speed: Speed in km/h
        acceleration: Acceleration in m/s²
        driver_status: Driver status string
        
    Returns:
        True if published successfully
    """
    publisher = get_mqtt_publisher()
    return publisher.publish_gps(lat, lng, speed, acceleration, driver_status=driver_status)


def publish_status_mqtt(status: str, lat: float = 0.0, lng: float = 0.0, 
                        speed: float = 0.0) -> bool:
    """
    Convenience function to publish driver status via MQTT
    
    Args:
        status: Status string
        lat: Latitude
        lng: Longitude
        speed: Speed in km/h
        
    Returns:
        True if published successfully
    """
    publisher = get_mqtt_publisher()
    return publisher.publish_status(status, lat, lng, speed)
