import RPi.GPIO as GPIO
import time

LED_PIN = 4
blinking = False

# Watchdog: LED auto-stops if not refreshed within timeout
_last_blink_refresh = 0
BLINK_WATCHDOG_TIMEOUT = 2.5

# Set up GPIO mode
GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PIN, GPIO.OUT)

def refresh_blinking():
    """Call every frame during sleeping/yawning to keep LED alive."""
    global _last_blink_refresh
    _last_blink_refresh = time.time()

def start_blinking():
    """Blink LED until stopped or watchdog timeout. Call refresh_blinking() to keep alive."""
    global blinking, _last_blink_refresh
    blinking = True
    _last_blink_refresh = time.time()
    while blinking:
        if time.time() - _last_blink_refresh > BLINK_WATCHDOG_TIMEOUT:
            blinking = False
            GPIO.output(LED_PIN, GPIO.LOW)
            break
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(LED_PIN, GPIO.LOW)
        time.sleep(0.1)

def stop_blinking():
    global blinking
    blinking = False
    GPIO.output(LED_PIN, GPIO.LOW)
