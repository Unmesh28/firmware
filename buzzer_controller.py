# buzzer_controller.py
import time
import logging
from gpiozero import Buzzer
from time import sleep

# Configure logging for buzzer debugging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('buzzer')

buzzer1 = Buzzer(12)
buzzer2 = Buzzer(13)

# Cooldown tracking to prevent rapid buzzing
_last_buzz_time = 0
BUZZ_COOLDOWN_SECONDS = 0  # Minimum time between buzzes

def buzz_for(duration):
    """Buzz for a duration (used for alerts)"""
    global _last_buzz_time
    current_time = time.time()
    time_since_last = current_time - _last_buzz_time
    
    logger.debug(f"buzz_for called: duration={duration}, time_since_last={time_since_last:.2f}s")
    
    if time_since_last < BUZZ_COOLDOWN_SECONDS:
        logger.debug(f"SKIPPED: cooldown active ({BUZZ_COOLDOWN_SECONDS - time_since_last:.2f}s remaining)")
        return
    
    logger.info(f"BUZZING for {duration}s (last buzz was {time_since_last:.2f}s ago)")
    _last_buzz_time = current_time
    
    buzzer1.on()
    buzzer2.on()
    sleep(duration)
    buzzer1.off()
    buzzer2.off()

def buzz_once(duration):
    """Buzz once with cooldown protection"""
    global _last_buzz_time
    current_time = time.time()
    time_since_last = current_time - _last_buzz_time
    
    logger.debug(f"buzz_once called: duration={duration}, time_since_last={time_since_last:.2f}s")
    
    if time_since_last < BUZZ_COOLDOWN_SECONDS:
        logger.debug(f"SKIPPED: cooldown active ({BUZZ_COOLDOWN_SECONDS - time_since_last:.2f}s remaining)")
        return
    
    logger.info(f"BUZZING once for {duration}s (last buzz was {time_since_last:.2f}s ago)")
    _last_buzz_time = current_time
    
    buzzer1.on()
    buzzer2.on()
    sleep(duration)
    buzzer1.off()
    buzzer2.off()
