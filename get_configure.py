import mysql.connector
from mysql.connector import Error

# Default configuration values
DEFAULT_CONFIG = {
    'speed': '20',  # Activation speed threshold (km/h)
}


def get_configure(config_key):
    try:
        # Establish a connection to the MySQL database
        connection = mysql.connector.connect(
            host='127.0.0.1',
            database='car',
            user='root',
            password='raspberry@123'
        )
        # Create a MySQL cursor object to execute queries
        cursor = connection.cursor()
        # SQL query to retrieve the config_value for the specified config_key
        select_query = "SELECT config_value FROM configure WHERE config_key = %s"
        cursor.execute(select_query, (config_key,))
        # Fetch the result
        result = cursor.fetchone()
        if result:
            print(f"Value for '{config_key}' is {result[0]}")
            return result[0]  # Return the config_value
        else:
            # Return default value if available
            default_val = DEFAULT_CONFIG.get(config_key)
            if default_val:
                print(f"No value found for '{config_key}', using default: {default_val}")
                return default_val
            print(f"No value found for config_key '{config_key}' in the configure table.")
            return None
    except Error as e:
        print("Error:", e)
        return None
    finally:
        # Close the cursor and connection
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def set_configure(config_key, config_value):
    """Set or update a configuration value in the database"""
    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(
            host='127.0.0.1',
            database='car',
            user='root',
            password='raspberry@123'
        )
        cursor = connection.cursor()
        
        # Check if key exists
        cursor.execute("SELECT config_value FROM configure WHERE config_key = %s", (config_key,))
        result = cursor.fetchone()
        
        if result:
            # Update existing
            cursor.execute(
                "UPDATE configure SET config_value = %s WHERE config_key = %s",
                (str(config_value), config_key)
            )
        else:
            # Insert new
            cursor.execute(
                "INSERT INTO configure (config_key, config_value) VALUES (%s, %s)",
                (config_key, str(config_value))
            )
        
        connection.commit()
        print(f"Set '{config_key}' = '{config_value}'")
        return True
    except Error as e:
        print(f"Error setting config: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def ensure_configure_table():
    """Ensure configure table exists"""
    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(
            host='127.0.0.1',
            database='car',
            user='root',
            password='raspberry@123'
        )
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS configure (
                id INT AUTO_INCREMENT PRIMARY KEY,
                config_key VARCHAR(100) UNIQUE NOT NULL,
                config_value VARCHAR(255)
            )
        """)
        connection.commit()
        return True
    except Error as e:
        print(f"Error ensuring configure table: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()