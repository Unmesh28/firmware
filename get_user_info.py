import mysql.connector
from mysql.connector import Error

def get_user_info(field_name):
    """Get field from user_info table (auth_key from device table)"""
    try:
        connection = mysql.connector.connect(
            host='127.0.0.1',
            database='car',
            user='root',
            password='raspberry@123'
        )
        cursor = connection.cursor()
        # auth_key is in device table, not user_info
        if field_name == 'auth_key':
            select_query = "SELECT auth_key FROM device LIMIT 1"
        else:
            select_query = f"SELECT {field_name} FROM user_info LIMIT 1"
        cursor.execute(select_query)
        result = cursor.fetchone()
        if result:
            return result[0]
        else:
            print(f"No {field_name} found.")
            return None
    except Error as e:
        print("Error:", e)
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Example usage
phone_number = get_user_info('phone_number')
ACCESS_TOKEN = get_user_info('auth_key')

if phone_number:
    print(f"Phone Number: {phone_number}")

if ACCESS_TOKEN:
    print(f"Access Token: {ACCESS_TOKEN[:8]}...")

