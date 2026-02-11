import os
import serial
import pynmea2
import datetime
import math
import threading
import queue
import logging
from geopy.distance import geodesic
from gps3 import gps3
import time

from store_locally import add_gps_data_if_changed

# MQTT publishing for real-time live map updates
MQTT_ENABLED = os.getenv('MQTT_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
_mqtt_publisher = None

def _get_mqtt_publisher():
    """Lazy-load MQTT publisher to avoid import errors if not installed"""
    global _mqtt_publisher
    if _mqtt_publisher is None and MQTT_ENABLED:
        try:
            from mqtt_publisher import get_mqtt_publisher
            _mqtt_publisher = get_mqtt_publisher()
            logging.info("MQTT publisher initialized for live map updates")
        except ImportError as e:
            logging.warning(f"MQTT publisher not available: {e}")
        except Exception as e:
            logging.error(f"Failed to initialize MQTT publisher: {e}")
    return _mqtt_publisher

def _publish_gps_mqtt(lat, lng, speed, acc, driver_status="Active"):
    """Publish GPS data via MQTT for real-time updates (throttled)"""
    global _last_mqtt_publish
    now = time.time()
    if now - _last_mqtt_publish < MQTT_PUBLISH_INTERVAL:
        return
    _last_mqtt_publish = now
    publisher = _get_mqtt_publisher()
    if publisher:
        try:
            publisher.publish_gps(lat, lng, speed, acc, driver_status=driver_status)
        except Exception as e:
            logging.debug(f"MQTT publish failed: {e}")

last_gps_write = 0.0
_last_mqtt_publish = 0.0
MQTT_PUBLISH_INTERVAL = float(os.getenv('MQTT_PUBLISH_INTERVAL', '2.0'))

from gps_shm import GPSWriter
gps_writer = GPSWriter()

SERIAL_DEVICE_PATH = os.getenv('GPS_SERIAL_DEVICE', '/dev/ttyACM0')
SERIAL_BAUDRATE = int(os.getenv('GPS_SERIAL_BAUDRATE', '9600'))
GPS_WRITE_THROTTLE_SECONDS = float(os.getenv('GPS_WRITE_THROTTLE_SECONDS', '1.0'))

GPS_FORCE_GPSD = os.getenv('GPS_FORCE_GPSD', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
GPS_GPSD_USE_ECEF = os.getenv('GPS_GPSD_USE_ECEF', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

GPS_DEBUG_COMPARE = os.getenv('GPS_DEBUG_COMPARE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
GPS_DEBUG_LOG_RAW = os.getenv('GPS_DEBUG_LOG_RAW', '0').strip().lower() in {'1', 'true', 'yes', 'on'}

GPS_DEBUG_LATLON_TOL = float(os.getenv('GPS_DEBUG_LATLON_TOL', '1e-6'))
GPS_DEBUG_ACC_TOL = float(os.getenv('GPS_DEBUG_ACC_TOL', '1e-3'))

_last_debug_log = 0.0


def _debug_log_sample(source, raw_line=None, lat=None, lon=None, speed=None, acc=None):
    global _last_debug_log

    if not GPS_DEBUG_COMPARE and not GPS_DEBUG_LOG_RAW:
        return

    now = time.time()
    if now - _last_debug_log < 1.0:
        return
    _last_debug_log = now

    if GPS_DEBUG_LOG_RAW and raw_line:
        logging.debug('[%s] Raw GPS: %s', source, raw_line)

    if GPS_DEBUG_COMPARE:
        logging.debug(
            '[%s] Parsed -> lat=%s lon=%s speed_kmh=%s acc=%s',
            source,
            lat,
            lon,
            speed,
            acc,
        )


def _shm_get_float(key):
    """Read a GPS value from shared memory (for debug comparison)."""
    try:
        lat, lon, speed, acc, ts = gps_writer._shm.seek(0) or (None,)
        # Re-read properly
        from gps_shm import GPSReader
        reader = GPSReader()
        lat, lon, speed, acc, ts = reader.read()
        reader.close()
        key_map = {'lat': lat, 'long': lon, 'speed': speed, 'acc': acc}
        return key_map.get(key)
    except Exception:
        return None


def _shm_get_int(key):
    """Read speed from shared memory (for debug comparison)."""
    try:
        from gps_shm import GPSReader
        reader = GPSReader()
        lat, lon, speed, acc, ts = reader.read()
        reader.close()
        if key == 'speed':
            return speed
        return None
    except Exception:
        return None


def _debug_compare_after_write(lat, lon, speed, acc, source, raw_line=None):
    global _last_debug_log

    if not GPS_DEBUG_COMPARE:
        return

    _debug_log_sample(source=source, raw_line=raw_line, lat=lat, lon=lon, speed=speed, acc=acc)

    r_lat = _shm_get_float('lat')
    r_lon = _shm_get_float('long')
    r_speed = _shm_get_float('speed')
    r_acc = _shm_get_float('acc')

    mismatches = []
    if r_lat is None or abs(r_lat - float(lat)) > GPS_DEBUG_LATLON_TOL:
        mismatches.append(f'lat(parsed={lat}, shm={r_lat})')
    if r_lon is None or abs(r_lon - float(lon)) > GPS_DEBUG_LATLON_TOL:
        mismatches.append(f'long(parsed={lon}, shm={r_lon})')
    if r_speed is None or (speed is not None and abs(r_speed - float(speed)) > 0.1):
        mismatches.append(f'speed(parsed={speed}, shm={r_speed})')
    if r_acc is None or abs(r_acc - float(acc)) > GPS_DEBUG_ACC_TOL:
        mismatches.append(f'acc(parsed={acc}, shm={r_acc})')

    if mismatches:
        logging.warning('[%s] SHM mismatch: %s', source, '; '.join(mismatches))


def _ecef_to_latlon(x, y, z):
    a = 6378137.0
    e2 = 6.69437999014e-3
    b = a * math.sqrt(1.0 - e2)
    ep2 = (a * a - b * b) / (b * b)

    p = math.sqrt(x * x + y * y)
    if p == 0.0:
        return None, None

    lon = math.atan2(y, x)
    theta = math.atan2(z * a, p * b)
    sin_theta = math.sin(theta)
    cos_theta = math.cos(theta)
    lat = math.atan2(
        z + ep2 * b * sin_theta * sin_theta * sin_theta,
        p - e2 * a * cos_theta * cos_theta * cos_theta,
    )

    return math.degrees(lat), math.degrees(lon)


# ------------------------ PARSE GPS -------------------------
def parse_gps_with_pynmea(data):
    try:
        parsed_data = pynmea2.parse(data)
        current_datetime = datetime.datetime.now()

        speed = None  # None = sentence has no speed data (e.g. GGA)
        parsed_timestamp = None
        lat = None
        lon = None

        if hasattr(parsed_data, 'timestamp'):
            parsed_timestamp = datetime.datetime.combine(
                current_datetime.date(),
                parsed_data.timestamp
            ).replace(tzinfo=None)

        if hasattr(parsed_data, 'latitude'):
             lat = parsed_data.latitude

        if hasattr(parsed_data, 'longitude'):
             lon = parsed_data.longitude

        # Extract speed — only RMC and VTG sentences carry speed data
        if isinstance(parsed_data, pynmea2.types.talker.RMC):
            # RMC has speed in knots
            if parsed_data.spd_over_grnd is not None:
                try:
                   speed = float(parsed_data.spd_over_grnd) * 1.852 # Knots to km/h
                except (ValueError, TypeError):
                   speed = 0.0
            else:
                speed = 0.0

        elif isinstance(parsed_data, pynmea2.types.talker.VTG):
            # VTG has speed in km/h
             if parsed_data.spd_over_grnd_kmh is not None:
                try:
                    speed = float(parsed_data.spd_over_grnd_kmh)
                except (ValueError, TypeError):
                    speed = 0.0
             else:
                speed = 0.0

        return lat, lon, parsed_timestamp, speed

    except Exception as e:
        return None, None, None, None


# ------------------------ CALCULATIONS -------------------------
def calculate_speed_fallback(lat1, lon1, t1, lat2, lon2, t2):
    distance = geodesic((lat1, lon1), (lat2, lon2)).meters
    time_diff = (t2 - t1).total_seconds()
    if time_diff == 0:
        return 0
    return (distance / time_diff) * (60 * 60) / 1000  # km/h


def calculate_acceleration(speed1, t1, speed2, t2):
    time_diff = (t2 - t1).total_seconds()
    if time_diff == 0:
        return 0
    return (speed2 - speed1) / time_diff


def store_gps_data(lat, lon, speed):
    add_gps_data_if_changed(lat, lon, speed)


class _GpsStoreWorker:
    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._stop_event = threading.Event()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        try:
            self._queue.put_nowait((0.0, 0.0, 0.0))
        except queue.Full:
            pass

    def submit_latest(self, lat: float, lon: float, speed: float):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait((lat, lon, speed))
        except queue.Full:
            pass

    def _run(self):
        while not self._stop_event.is_set():
            try:
                lat, lon, speed = self._queue.get()
                if self._stop_event.is_set():
                    return
                store_gps_data(lat, lon, speed)
            except Exception:
                logging.exception('Failed to persist GPS data')


# ------------------------------------------------------------
#  READ FROM GPSD WHEN NO SERIAL DEVICE IS PRESENT
# ------------------------------------------------------------
def read_from_gpsd():
    if GPS_FORCE_GPSD:
        logging.info('Using gpsd (forced by GPS_FORCE_GPSD).')
    elif not os.path.exists(SERIAL_DEVICE_PATH):
        logging.warning('GPS serial device not found (%s). Falling back to gpsd.', SERIAL_DEVICE_PATH)
    else:
        logging.info('Using gpsd.')

    gps_socket = gps3.GPSDSocket()
    data_stream = gps3.DataStream()

    gps_socket.connect()
    gps_socket.watch()

    # lat1, lon1, t1 = 0, 0, datetime.datetime.now().replace(tzinfo=None)
    prev_speed = 0.0
    global last_gps_write
    THROTTLE_SECONDS = GPS_WRITE_THROTTLE_SECONDS

    store_worker = _GpsStoreWorker()
    store_worker.start()

    last_timestamp = datetime.datetime.now()

    try:
        while True:
            try:
                new_data = next(gps_socket)
            except StopIteration:
                store_worker.stop()
                return

            if not new_data:
                time.sleep(0.01)  # Prevent CPU spin when no data
                continue

            try:
                # Check for TPV first to avoid unnecessary processing
                if 'TPV' not in new_data:
                    continue

                data_stream.unpack(new_data)

                lat2 = data_stream.TPV.get('lat', None)
                lon2 = data_stream.TPV.get('lon', None)

                ecefx = data_stream.TPV.get('ecefx', None)
                ecefy = data_stream.TPV.get('ecefy', None)
                ecefz = data_stream.TPV.get('ecefz', None)

                speed_gps = data_stream.TPV.get('speed', None)  # Speed in m/s
                vel_n = data_stream.TPV.get('velN', None)
                vel_e = data_stream.TPV.get('velE', None)

                current_timestamp = datetime.datetime.now()

                if GPS_GPSD_USE_ECEF and ecefx not in (None, 'n/a') and ecefy not in (None, 'n/a') and ecefz not in (None, 'n/a'):
                    try:
                        lat_from_ecef, lon_from_ecef = _ecef_to_latlon(float(ecefx), float(ecefy), float(ecefz))
                        if lat_from_ecef is not None and lon_from_ecef is not None:
                            lat2 = lat_from_ecef
                            lon2 = lon_from_ecef
                    except Exception:
                        pass

                if lat2 == 'n/a' or lon2 == 'n/a' or lat2 is None or lon2 is None:
                    _debug_log_sample(source='gpsd', raw_line=new_data if GPS_DEBUG_LOG_RAW else None, lat=lat2, lon=lon2)
                    continue

                try:
                    lat2 = float(lat2)
                    lon2 = float(lon2)

                    if speed_gps not in (None, 'n/a'):
                        speed = float(speed_gps) * 3.6  # m/s to km/h
                    elif vel_n not in (None, 'n/a') and vel_e not in (None, 'n/a'):
                        speed = math.sqrt(float(vel_n) ** 2 + float(vel_e) ** 2) * 3.6
                    else:
                        speed = 0.0

                except ValueError:
                    continue

                acc = calculate_acceleration(prev_speed, last_timestamp, speed, current_timestamp)

                _debug_log_sample(source='gpsd', raw_line=new_data if GPS_DEBUG_LOG_RAW else None, lat=lat2, lon=lon2, speed=speed, acc=acc)

                gps_writer.write(lat2, lon2, speed if 0 <= speed < 300 else 0.0, acc, time.time())

                _debug_compare_after_write(lat2, lon2, speed, acc, source='gpsd', raw_line=new_data if GPS_DEBUG_LOG_RAW else None)

                _publish_gps_mqtt(lat2, lon2, speed, acc)

                if time.time() - last_gps_write >= THROTTLE_SECONDS:
                    store_worker.submit_latest(lat2, lon2, speed)
                    last_gps_write = time.time()

                last_timestamp = current_timestamp
                prev_speed = speed

            except Exception:
                logging.exception('GPSD error')
    except KeyboardInterrupt:
        store_worker.stop()
        return


# ------------------------------------------------------------
#  READ FROM SERIAL IF AVAILABLE
# ------------------------------------------------------------
def read_gps_data():
    if GPS_FORCE_GPSD:
        logging.info('GPS_FORCE_GPSD is enabled. Using gpsd instead of serial.')
        return read_from_gpsd()

    if not os.path.exists(SERIAL_DEVICE_PATH):
        return read_from_gpsd()

    logging.info('Reading GPS data from serial device: %s', SERIAL_DEVICE_PATH)

    prev_speed = 0.0
    last_known_speed = 0.0  # Retains last valid speed from RMC/VTG
    global last_gps_write
    THROTTLE_SECONDS = GPS_WRITE_THROTTLE_SECONDS

    store_worker = _GpsStoreWorker()
    store_worker.start()

    last_timestamp = datetime.datetime.now()

    ser = None
    while True:
        try:
            if ser is None or not ser.is_open:
                ser = serial.Serial(SERIAL_DEVICE_PATH, baudrate=SERIAL_BAUDRATE, timeout=1)

            line = ser.readline()
            if not line:
                time.sleep(0.01)  # Prevent CPU spin when no data
                continue

            if not line.startswith(b'$'):
                continue

            data = line.decode('utf-8', errors='ignore').strip()
            if not data or not data.startswith('$') or ',' not in data:
                continue

            lat2, lon2, t2_parsed, speed = parse_gps_with_pynmea(data)

            # Only update speed when sentence actually has speed (RMC/VTG).
            # GGA/GSA etc. return speed=None — keep last known speed.
            if speed is not None:
                last_known_speed = speed
            speed = last_known_speed

            # Use current time if parsed time is not available
            current_timestamp = datetime.datetime.now()

            acc = calculate_acceleration(prev_speed, last_timestamp, speed, current_timestamp)

            _debug_log_sample(source='serial', raw_line=data, lat=lat2, lon=lon2, speed=speed, acc=acc)

            if lat2 is None or lon2 is None:
                continue

            if lat2 != 0 and lon2 != 0:
                gps_writer.write(lat2, lon2, speed, acc, time.time())

                _debug_compare_after_write(lat2, lon2, speed, acc, source='serial', raw_line=data)

                _publish_gps_mqtt(lat2, lon2, speed, acc)
            else:
                gps_writer.write(0.0, 0.0, speed, acc, time.time())

                if time.time() - last_gps_write >= THROTTLE_SECONDS:
                    store_worker.submit_latest(lat2, lon2, speed)
                    last_gps_write = time.time()

            last_timestamp = current_timestamp
            prev_speed = speed

        except KeyboardInterrupt:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            store_worker.stop()
            return
        except serial.SerialException:
            logging.exception('Serial error')
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
            ser = None
            time.sleep(1)
        except Exception:
            logging.exception('Unexpected error')


if __name__ == '__main__':
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(message)s',
    )
    read_gps_data()