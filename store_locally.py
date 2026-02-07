import os
import mysql.connector
from mysql.connector import Error, pooling
import time

GPS_COORD_DECIMALS = int(os.getenv('GPS_COORD_DECIMALS', '7'))


def _format_coord(value):
    try:
        return f"{float(value):.{GPS_COORD_DECIMALS}f}"
    except Exception:
        return None

# Define a global variable to store the last insertion time
last_insertion_time = 0

# Set up connection pool
connection_pool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=5,
    pool_reset_session=True,
    host='127.0.0.1',
    database='car',
    user='root',
    password='raspberry@123'
)

def get_connection():
    try:
        return connection_pool.get_connection()
    except Error as e:
        print(f"Error getting connection: {e}")
        return None

def create_database():
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            cursor.execute('CREATE DATABASE IF NOT EXISTS car')
            connection.close()
            #print('Database created successfully')
    except Error as e:
        print(f'Error creating database: {e}')

def create_table():
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            create_table_query = '''
            CREATE TABLE IF NOT EXISTS car_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                driver_status VARCHAR(255)
            )
            '''
            cursor.execute(create_table_query)
            connection.close()
            #print('Table created successfully')
    except Error as e:
        print(f'Error creating table: {e}')

def add_row(driver_status):
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            insert_query = '''
            INSERT INTO car_data (driver_status)
            VALUES (%s)
            '''
            data = (driver_status,)
            cursor.execute(insert_query, data)
            connection.commit()
            connection.close()
            #print('Row added successfully')
    except Error as e:
        print(f'Error adding row: {e}')

def add_row_new(driver_status):
    global last_insertion_time
    try:
        current_time = time.time()
        if (current_time - last_insertion_time) > 5:
            connection = get_connection()
            if connection:
                cursor = connection.cursor()
                insert_query = '''
                INSERT INTO car_data (driver_status)
                VALUES (%s)
                '''
                data = (driver_status,)
                cursor.execute(insert_query, data)
                connection.commit()
                connection.close()
                #print('Row added successfully')
                last_insertion_time = current_time
        else:
            #print(f"Skipped adding row for {driver_status} as it's less than 5 seconds since the last insertion.")
            pass
    except Error as e:
        print(f'Error adding row: {e}')

def add_gps_data(latitude, longitude, speed, time, driver_status, acceleration):
    try:
        # Validate GPS coordinates - skip if lat/long is 0 or invalid
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return  # Skip invalid GPS data
            
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            insert_query = '''
            INSERT INTO gps_data (latitude, longitude, speed, timestamp, driver_status, acceleration)
            VALUES (%s, %s, %s, %s, %s, %s)
            '''
            data = (latitude, longitude, speed, time, driver_status, acceleration)
            cursor.execute(insert_query, data)
            connection.commit()
            connection.close()
            #print('Row added successfully')
    except Error as e:
        print(f'Error adding row: {e}')

def get_last_gps_data():
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            select_query = '''
            SELECT latitude, longitude
            FROM gps_data
            ORDER BY id DESC
            LIMIT 1
            '''
            cursor.execute(select_query)
            result = cursor.fetchone()
            connection.close()
            return result
    except Error as e:
        print(f'Error getting last GPS data: {e}')
        return None

def add_gps_data_if_changed(latitude, longitude, speed):
    try:
        # Validate GPS coordinates - skip if lat/long is 0 or invalid
        lat = float(latitude) if latitude else 0.0
        lng = float(longitude) if longitude else 0.0
        if lat == 0.0 or lng == 0.0:
            return  # Skip invalid GPS data
            
        formatted_latitude = _format_coord(latitude)
        formatted_longitude = _format_coord(longitude)
        
        last_gps_data = get_last_gps_data()
        last_latitude, last_longitude = last_gps_data if last_gps_data else (None, None)

        last_formatted_latitude = _format_coord(last_latitude) if last_latitude is not None else None
        last_formatted_longitude = _format_coord(last_longitude) if last_longitude is not None else None

        if (last_latitude is None or last_longitude is None or
            last_formatted_latitude != formatted_latitude or last_formatted_longitude != formatted_longitude):

            connection = get_connection()
            if connection:
                cursor = connection.cursor()
                insert_query = '''
                INSERT INTO gps_data (latitude, longitude, speed)
                VALUES (%s, %s, %s)
                '''
                data = (formatted_latitude, formatted_longitude, speed)
                cursor.execute(insert_query, data)
                connection.commit()
                connection.close()
                #print('Row added successfully')
        else:
            #print('GPS data is the same as the previous record. Not adding.')
            pass

    except Error as e:
        print(f'Error adding GPS data: {e}')

def update_count(date):
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()

            select_query = '''
            SELECT id, count
            FROM count_table
            WHERE date = %s
            '''
            cursor.execute(select_query, (date,))
            result = cursor.fetchone()

            if result is None:
                insert_query = '''
                INSERT INTO count_table (date, count)
                VALUES (%s, 1)
                '''
                cursor.execute(insert_query, (date,))
                connection.commit()
                connection.close()
                return 'added'
            else:
                update_query = '''
                UPDATE count_table
                SET count = count + 1
                WHERE id = %s
                '''
                cursor.execute(update_query, (result[0],))
                connection.commit()
                connection.close()
                return 'existed'
    except Error as e:
        print(f'Error: {e}')
        return 'error'

def fetch_rows():
    try:
        connection = get_connection()
        if connection:
            cursor = connection.cursor()
            select_query = 'SELECT * FROM car_data WHERE timestamp >= NOW() - INTERVAL 15 MINUTE'
            cursor.execute(select_query)
            rows = cursor.fetchall()
            connection.close()
            return rows
    except Error as e:
        print(f'Error fetching rows: {e}')
        return []

def analyze_data(rows):
    status_counts = {}
    for row in rows:
        driver_status = row[2]  # Assuming driver_status is in the third column
        if driver_status in status_counts:
            status_counts[driver_status] += 1
        else:
            status_counts[driver_status] = 1
    return status_counts

# Call the functions to create the database, table, and add rows
# create_database()
# create_table()
# add_row('Driving')
# add_row('Parking')

