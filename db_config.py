"""
Database Connection Configuration
Handles MySQL connection pooling and configuration
"""
import os
from typing import Optional
import mysql.connector
from mysql.connector import pooling
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Database configuration from environment
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE", "bbtro"),
}

# Connection pool (initialized when needed)
connection_pool: Optional[pooling.MySQLConnectionPool] = None


def init_connection_pool(pool_size: int = 5) -> bool:
    """
    Initialize MySQL connection pool

    Args:
        pool_size: Number of connections in the pool (default: 5)

    Returns:
        True if successful, False otherwise
    """
    global connection_pool
    try:
        connection_pool = pooling.MySQLConnectionPool(
            pool_name="rail_analysis_pool",
            pool_size=pool_size,
            **DB_CONFIG
        )
        print(f"[DB] ✓ Connection pool initialized ({pool_size} connections)")
        return True
    except mysql.connector.Error as e:
        print(f"[DB] ✗ Failed to create connection pool: {e}")
        return False


def get_db_connection():
    """
    Get a database connection from the pool (or create new if pool not initialized)

    Returns:
        MySQL connection object
    """
    if connection_pool:
        return connection_pool.get_connection()
    else:
        # Fallback: direct connection (for testing or desktop app)
        return mysql.connector.connect(**DB_CONFIG)


def test_connection() -> bool:
    """
    Test database connectivity

    Returns:
        True if connection successful, False otherwise
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 AS test")
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result and result[0] == 1:
            print("[DB] ✓ Connection test successful!")
            return True
        else:
            print("[DB] ✗ Connection test returned unexpected result")
            return False
    except mysql.connector.Error as e:
        print(f"[DB] ✗ Connection test failed: {e}")
        print("[DB] Make sure SSH tunnel is running: ./start-ssh-tunnel.sh")
        return False
    except Exception as e:
        print(f"[DB] ✗ Unexpected error during connection test: {e}")
        return False


if __name__ == "__main__":
    # Test connection when running this file directly
    print("Testing MySQL connection...")
    print(f"Host: {DB_CONFIG['host']}")
    print(f"Port: {DB_CONFIG['port']}")
    print(f"Database: {DB_CONFIG['database']}")
    print(f"User: {DB_CONFIG['user']}")
    print()

    if test_connection():
        print("\n✓ Database configuration is correct!")
    else:
        print("\n✗ Database connection failed!")
        print("\nTroubleshooting:")
        print("1. Make sure SSH tunnel is running: ./start-ssh-tunnel.sh")
        print("2. Check .env file has correct credentials")
        print("3. Verify MySQL is running on server: sudo systemctl status mysql")
