# buzzer_controller.py
import time
import logging
import threading
from gpiozero import Buzzer
from time import sleep

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('buzzer')

buzzer1 = Buzzer(12)
buzzer2 = Buzzer(13)

# --- One-shot buzz (for NoFace alerts) ---
_buzz_lock = threading.Lock()
_buzz_active = False

def _do_buzz(duration):
    """Internal: run buzzer for duration, then turn off."""
    global _buzz_active
    try:
        buzzer1.on()
        buzzer2.on()
        sleep(duration)
        # Don't turn off if continuous buzzing took over
        if not _continuous_buzz_active:
            buzzer1.off()
            buzzer2.off()
    finally:
        _buzz_active = False

def buzz_for(duration):
    """Buzz for a fixed duration (NoFace alerts) - non-blocking, self-limiting."""
    global _buzz_active
    with _buzz_lock:
        if _buzz_active:
            return
        _buzz_active = True
    threading.Thread(target=_do_buzz, args=(duration,), daemon=True).start()

# --- Continuous buzz with WATCHDOG (sleeping/yawning alerts) ---
# Dead man's switch: buzzer auto-stops if not refreshed within WATCHDOG_TIMEOUT.
# Call start_continuous_buzz() every frame to keep it alive.
# This makes it IMPOSSIBLE for the buzzer to get stuck.

_continuous_buzz_active = False
_continuous_buzz_thread = None
_continuous_lock = threading.Lock()
_last_refresh_time = 0
WATCHDOG_TIMEOUT = 2.5  # Auto-stop after 2.5s without refresh

def _continuous_buzz_loop():
    """Internal: beep on/off until stopped OR watchdog timeout."""
    global _continuous_buzz_active
    while _continuous_buzz_active:
        # Watchdog check: auto-stop if no refresh
        if time.time() - _last_refresh_time > WATCHDOG_TIMEOUT:
            _continuous_buzz_active = False
            buzzer1.off()
            buzzer2.off()
            logger.info("Continuous buzzer WATCHDOG TIMEOUT - auto-stopped")
            return
        buzzer1.on()
        buzzer2.on()
        sleep(0.15)
        buzzer1.off()
        buzzer2.off()
        if _continuous_buzz_active:
            sleep(0.1)

def start_continuous_buzz():
    """Start or refresh continuous beeping. Call every frame to keep alive.
    Watchdog auto-stops if this isn't called for 2.5 seconds."""
    global _continuous_buzz_active, _continuous_buzz_thread, _last_refresh_time
    _last_refresh_time = time.time()  # Refresh watchdog
    with _continuous_lock:
        if _continuous_buzz_active and _continuous_buzz_thread and _continuous_buzz_thread.is_alive():
            return  # Already running, just refreshed timestamp
        _continuous_buzz_active = True
        _continuous_buzz_thread = threading.Thread(target=_continuous_buzz_loop, daemon=True)
        _continuous_buzz_thread.start()
        logger.info("Continuous buzzer STARTED")

def stop_continuous_buzz():
    """Immediately stop continuous beeping. Also called by watchdog."""
    global _continuous_buzz_active
    with _continuous_lock:
        if not _continuous_buzz_active:
            return
        _continuous_buzz_active = False
        logger.info("Continuous buzzer STOPPED")
    buzzer1.off()
    buzzer2.off()
