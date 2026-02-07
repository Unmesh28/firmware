import os
import logging
from datetime import datetime

def setup_logger(log_dir):
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_filename = os.path.join(log_dir, f"log_{datetime.now().strftime('%Y-%m-%d')}.log")

    logging.basicConfig(
        filename=log_filename,
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger('').addHandler(console_handler)

def log_info(message):
    logging.info(message)

def log_error(message):
    logging.error(message)

# Set the directory where you want to store the log files
log_directory = "./logs"
setup_logger(log_directory)

