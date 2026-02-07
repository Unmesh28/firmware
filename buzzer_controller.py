# buzzer_controller.py
import time
import logging
import threading
from gpiozero import Buzzer
from time import sleep

# Configure logging for buzzer debugging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('buzzer')

buzzer1 = Buzzer(12)
buzzer2 = Buzzer(13)

# Cooldown tracking to prevent rapid buzzing
_last_buzz_time = 0
BUZZ_COOLDOWN_SECONDS = 0.5  # Minimum time between buzzes (max 2 buzzes/sec)
_buzz_lock = threading.Lock()
_buzz_active = False  # Prevent overlapping buzz threads

def _do_buzz(duration):
    """Internal: run buzzer for duration, then turn off."""
    global _buzz_active
    try:
        buzzer1.on()
        buzzer2.on()
        sleep(duration)
        buzzer1.off()
        buzzer2.off()
    finally:
        _buzz_active = False

def buzz_for(duration):
    """Buzz for a duration (used for alerts) - non-blocking"""
    global _last_buzz_time, _buzz_active
    current_time = time.time()

    with _buzz_lock:
        time_since_last = current_time - _last_buzz_time

        if time_since_last < BUZZ_COOLDOWN_SECONDS:
            return

        if _buzz_active:
            return  # Already buzzing, skip

        logger.info(f"BUZZING for {duration}s (last buzz was {time_since_last:.2f}s ago)")
        _last_buzz_time = current_time
        _buzz_active = True

    threading.Thread(target=_do_buzz, args=(duration,), daemon=True).start()

def buzz_once(duration):
    """Buzz once with cooldown protection - non-blocking"""
    global _last_buzz_time, _buzz_active
    current_time = time.time()

    with _buzz_lock:
        time_since_last = current_time - _last_buzz_time

        if time_since_last < BUZZ_COOLDOWN_SECONDS:
            return

        if _buzz_active:
            return  # Already buzzing, skip

        logger.info(f"BUZZING once for {duration}s (last buzz was {time_since_last:.2f}s ago)")
        _last_buzz_time = current_time
        _buzz_active = True

    threading.Thread(target=_do_buzz, args=(duration,), daemon=True).start()
