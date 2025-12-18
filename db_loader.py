"""
Database Data Loaders
Loads reference data from MySQL into Polars DataFrames
"""
import polars as pl
from db_config import get_db_connection
import mysql.connector


def load_staff_from_db(active_only: bool = True, lp_only: bool = True) -> pl.DataFrame:
    """
    Load staff data from MySQL database

    Args:
        active_only: Only load staff with status='Active' (default: True)
        lp_only: Only load Loco Pilots and Assistant Loco Pilots (default: True)

    Returns:
        Polars DataFrame with columns: hrms_id, name, designation, cli_name, hq_station, cug_number, email, status
    """
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build query with optional filters
        query = """
            SELECT
                s.hrms_id,
                s.name,
                d.designation_name AS designation,
                c.cli_name,
                s.hq_station,
                s.cug_number,
                s.email,
                s.status
            FROM div_staff_master s
            LEFT JOIN designations d ON s.designation_id = d.id
            LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
            WHERE 1=1
        """

        if active_only:
            query += " AND s.status = 'Active'"

        if lp_only:
            # Filter for loco pilot designations
            # Add more designation names as needed
            query += """ AND (
                d.designation_name LIKE '%Loco Pilot%'
                OR d.designation_name LIKE '%LP%'
                OR d.designation_name LIKE '%ALP%'
                OR d.designation_name LIKE '%Assistant Loco Pilot%'
            )"""

        query += " ORDER BY s.name"

        cursor.execute(query)
        rows = cursor.fetchall()

        # Convert to Polars DataFrame
        if rows:
            df = pl.DataFrame(rows)
            print(f"[DB] ✓ Loaded {len(df)} staff records from MySQL")
            return df
        else:
            print("[DB] ⚠ No staff records found matching criteria")
            # Return empty DataFrame with correct schema
            return pl.DataFrame(schema={
                "hrms_id": pl.Utf8,
                "name": pl.Utf8,
                "designation": pl.Utf8,
                "cli_name": pl.Utf8,
                "hq_station": pl.Utf8,
                "cug_number": pl.Utf8,
                "email": pl.Utf8,
                "status": pl.Utf8,
            })

    except mysql.connector.Error as e:
        print(f"[DB] ✗ MySQL error while loading staff data: {e}")
        raise
    except Exception as e:
        print(f"[DB] ✗ Unexpected error while loading staff data: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def load_staff_from_csv_fallback(csv_path: str = "mail_staff.csv") -> pl.DataFrame:
    """
    Fallback: Load staff from CSV if MySQL connection fails

    Args:
        csv_path: Path to CSV file (default: "mail_staff.csv")

    Returns:
        Polars DataFrame with staff data
    """
    try:
        df = pl.read_csv(csv_path)
        print(f"[FALLBACK] ✓ Loaded {len(df)} staff records from CSV: {csv_path}")
        return df
    except FileNotFoundError:
        print(f"[FALLBACK] ✗ CSV file not found: {csv_path}")
        return pl.DataFrame()
    except Exception as e:
        print(f"[FALLBACK] ✗ Failed to load CSV: {e}")
        return pl.DataFrame()


def get_staff_by_name(name: str, staff_df: pl.DataFrame) -> pl.DataFrame:
    """
    Search for staff by name (case-insensitive, partial match)

    Args:
        name: Name to search for
        staff_df: Staff DataFrame from load_staff_from_db()

    Returns:
        Filtered DataFrame with matching staff
    """
    if staff_df.is_empty():
        return staff_df

    # Case-insensitive partial match
    return staff_df.filter(
        pl.col("name").str.to_lowercase().str.contains(name.lower())
    )


def get_staff_by_hrms_id(hrms_id: str, staff_df: pl.DataFrame) -> pl.DataFrame:
    """
    Get staff by exact HRMS ID

    Args:
        hrms_id: HRMS ID to search for
        staff_df: Staff DataFrame from load_staff_from_db()

    Returns:
        Filtered DataFrame with matching staff (should be 0 or 1 row)
    """
    if staff_df.is_empty():
        return staff_df

    return staff_df.filter(pl.col("hrms_id") == hrms_id)


if __name__ == "__main__":
    # Test loading staff data when running this file directly
    print("Testing staff data loading from MySQL...\n")

    try:
        # Load all active loco pilots
        staff_df = load_staff_from_db(active_only=True, lp_only=True)

        if not staff_df.is_empty():
            print(f"\nTotal staff loaded: {len(staff_df)}")
            print("\nFirst 5 records:")
            print(staff_df.head(5))

            print("\nColumn names:")
            print(staff_df.columns)

            # Test search
            print("\n\nTesting search for 'Prabhu':")
            result = get_staff_by_name("Prabhu", staff_df)
            print(result)

        else:
            print("No staff data loaded")

    except Exception as e:
        print(f"Error: {e}")
        print("\nMake sure:")
        print("1. SSH tunnel is running: ./start-ssh-tunnel.sh")
        print("2. .env file has correct credentials")
