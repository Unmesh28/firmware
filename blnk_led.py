import RPi.GPIO as GPIO
import time
#from gpiozero import Buzzer
#from time import sleep

LED_PIN = 4
blinking = False

# Set up GPIO mode
GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PIN, GPIO.OUT)
#GPIO.setup(BUZZER_PIN, GPIO.OUT)


def start_blinking():
    global blinking
    blinking = True
    #print("LED blinking started")
    while blinking:
        GPIO.output(LED_PIN, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(LED_PIN, GPIO.LOW)
        time.sleep(0.1)

def stop_blinking():
    global blinking
    blinking = False
    GPIO.output(LED_PIN, GPIO.LOW)  # Ensure LED is off when stopped
    #print("LED blinking stopped")
    
#GPIO.cleanup()
