#!/usr/bin/env python
"""Test script for 12139 train CSV with route membership filter."""
import requests
import json

API = "http://localhost:8765"

# Upload CSV
print("1. Uploading CSV...")
with open("/Users/neeraja/Desktop/rail-data-app/12139-39320-SURENDRA.csv", "rb") as f:
    response = requests.post(f"{API}/load_csv", files={"file": f})
    print(f"   Status: {response.status_code}")
    if response.ok:
        data = response.json()
        print(f"   Rows: {data['rows']}, Cols: {data['cols']}")

# Get train info
print("\n2. Getting train info for 12139...")
response = requests.get(f"{API}/train_info?train_number=12139")
if response.ok:
    train_info = response.json()
    print(f"   Train: {train_info['train_number']} - {train_info['train_name']}")
    print(f"   From: {train_info['from_station']} -> To: {train_info['to_station']}")
    print(f"   Direction: {train_info['direction']}")

    # Analyze
    print("\n3. Running analyze...")
    criteria = {
        "from_station_equals": train_info['from_station'],
        "to_station_equals": train_info['to_station'],
        "direction_equals": train_info['direction']
    }
    response = requests.post(f"{API}/analyze", json=criteria)
    if response.ok:
        result = response.json()
        print(f"   Filtered rows: {result['rows']}")
        if result['rows'] > 0:
            sample = result['sample'][0]
            print(f"   First row GPS time: {sample.get('GPS Time')}")
            print(f"   First row station: {sample.get('Station Code')}")
else:
    print(f"   Error: {response.json()}")

print("\n4. Check /tmp/server.log for debug output")
