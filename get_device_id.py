"""Device ID and Auth Key retrieval module.

This module provides functions to get device credentials from the local MySQL database.
If device is not provisioned, it will trigger the provisioning process.
"""

import mysql.connector
from mysql.connector import Error
import os
import logging

logger = logging.getLogger(__name__)

# Database configuration
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_NAME = os.getenv("DB_NAME", "car")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "raspberry@123")


def get_db_connection():
    """Get MySQL database connection"""
    return mysql.connector.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )


def get_device_id_from_db():
    """Get device_id from local MySQL database"""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT device_id FROM device LIMIT 1")
        result = cursor.fetchone()
        if result:
            return result[0]
        else:
            logger.warning("No device_id found in database")
            return None
    except Error as e:
        logger.error(f"Database error: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def get_auth_key_from_db():
    """Get auth_key from local MySQL database"""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT auth_key FROM device LIMIT 1")
        result = cursor.fetchone()
        if result:
            return result[0]
        else:
            logger.warning("No auth_key found in database")
            return None
    except Error as e:
        logger.error(f"Database error: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def get_device_credentials():
    """Get both device_id and auth_key from database
    
    Returns:
        dict: {"device_id": str, "auth_key": str} or None if not found
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT device_id, auth_key FROM device LIMIT 1")
        result = cursor.fetchone()
        if result and result[0] and result[1]:
            return {"device_id": result[0], "auth_key": result[1]}
        else:
            logger.warning("Device not provisioned (missing device_id or auth_key)")
            return None
    except Error as e:
        logger.error(f"Database error: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def is_device_provisioned():
    """Check if device has been provisioned with device_id and auth_key"""
    creds = get_device_credentials()
    return creds is not None


# For backward compatibility and direct execution
if __name__ == "__main__":
    device_id = get_device_id_from_db()
    if device_id:
        print(f"Device ID: {device_id}")
    
    auth_key = get_auth_key_from_db()
    if auth_key:
        print(f"Auth Key: {auth_key[:8]}...{auth_key[-8:]}")
    
    if not device_id or not auth_key:
        print("\nDevice not provisioned. Run: python device_provisioning.py")

