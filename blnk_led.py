"""Non-blocking LED controller — called from main loop, no threads."""
import RPi.GPIO as GPIO
import time

LED_PIN = 4
_active = False
_last_toggle = 0.0
_last_refresh = 0.0
BLINK_WATCHDOG_TIMEOUT = 2.5

GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PIN, GPIO.OUT)


def refresh_blinking():
    """Call every frame during sleeping/yawning to keep LED alive."""
    global _last_refresh
    _last_refresh = time.time()


def start_blinking():
    """Start LED blinking. Call update_led() from main loop."""
    global _active, _last_refresh
    _active = True
    _last_refresh = time.time()


def stop_blinking():
    global _active
    _active = False
    GPIO.output(LED_PIN, GPIO.LOW)


def update_led():
    """Call from main loop every frame. Handles blinking + watchdog."""
    global _active, _last_toggle
    if not _active:
        return
    now = time.time()
    # Watchdog: auto-stop if not refreshed
    if now - _last_refresh > BLINK_WATCHDOG_TIMEOUT:
        stop_blinking()
        return
    # Toggle at ~5Hz (100ms on/off)
    if now - _last_toggle >= 0.1:
        _last_toggle = now
        GPIO.output(LED_PIN, not GPIO.input(LED_PIN))
