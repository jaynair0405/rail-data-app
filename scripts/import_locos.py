#!/usr/bin/env python3
"""
One-time script to import locos from cr_locos.csv into div_cr_locos table.
Run after creating the tables with sql/create_loco_tables.sql
"""

import csv
import mysql.connector
from mysql.connector import Error
import os

# Database configuration - update these values
DB_CONFIG = {
    'host': 'localhost',
    'database': 'aborad1_locodata',  # Update if different
    'user': 'root',                   # Update with your username
    'password': ''                    # Update with your password
}

# Path to CSV file
CSV_FILE = os.path.join(os.path.dirname(__file__), '..', 'cr_locos.csv')


def import_locos():
    """Import locos from CSV to database."""
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        cursor = connection.cursor()

        # Read CSV file
        with open(CSV_FILE, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)

            insert_count = 0
            skip_count = 0

            for row in reader:
                loco_number = row.get('Loco No.', '').strip()
                loco_type = row.get('Type of Loco', '').strip()
                current_shed = row.get('Base Shed', '').strip()

                if not loco_number:
                    continue

                # Insert or update loco
                sql = """
                    INSERT INTO div_cr_locos (loco_number, loco_type, current_shed, status)
                    VALUES (%s, %s, %s, 'Active')
                    ON DUPLICATE KEY UPDATE
                        loco_type = VALUES(loco_type),
                        current_shed = VALUES(current_shed)
                """

                try:
                    cursor.execute(sql, (loco_number, loco_type, current_shed))
                    if cursor.rowcount > 0:
                        insert_count += 1
                    else:
                        skip_count += 1
                except Error as e:
                    print(f"Error inserting loco {loco_number}: {e}")
                    skip_count += 1

            connection.commit()

            print(f"\nImport completed!")
            print(f"  Inserted/Updated: {insert_count}")
            print(f"  Skipped: {skip_count}")

            # Show summary by shed
            cursor.execute("""
                SELECT current_shed, COUNT(*) as count
                FROM div_cr_locos
                WHERE status = 'Active'
                GROUP BY current_shed
                ORDER BY count DESC
            """)

            print(f"\nLocos by Shed:")
            for row in cursor.fetchall():
                print(f"  {row[0]}: {row[1]}")

            # Show summary by type
            cursor.execute("""
                SELECT loco_type, COUNT(*) as count
                FROM div_cr_locos
                WHERE status = 'Active'
                GROUP BY loco_type
                ORDER BY count DESC
            """)

            print(f"\nLocos by Type:")
            for row in cursor.fetchall():
                print(f"  {row[0]}: {row[1]}")

    except Error as e:
        print(f"Database error: {e}")
    except FileNotFoundError:
        print(f"CSV file not found: {CSV_FILE}")
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()


if __name__ == '__main__':
    print("CR Locos Import Script")
    print("=" * 40)
    print(f"CSV File: {CSV_FILE}")
    print(f"Database: {DB_CONFIG['database']}")
    print()

    confirm = input("Proceed with import? (y/n): ")
    if confirm.lower() == 'y':
        import_locos()
    else:
        print("Import cancelled.")
