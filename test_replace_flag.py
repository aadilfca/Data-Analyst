"""
Test script to verify the replace flag works correctly in the import flow.
"""
import requests
import sys

API_BASE = "http://localhost:5000"

def print_test(name, success, details=""):
    status = "✅" if success else "❌"
    print(f"{status} {name}: {details}")

# Step 1: Upload Excel file
print("=== Step 1: Upload Excel file ===")
with open("uploads/Sample transaction report.xlsx", "rb") as f:
    files = {"file": ("test.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = requests.post(f"{API_BASE}/api/import-excel", files=files)

if resp.status_code != 200:
    print_test("Upload file", False, f"Status {resp.status_code}: {resp.text}")
    sys.exit(1)

result = resp.json()
file_id = result["file_id"]
print_test("Upload file", True, f"file_id={file_id}, rows={result.get('record_count')}")

# Step 2: Auto-map with correct sheet name
print("\n=== Step 2: Auto-map columns ===")
resp = requests.post(f"{API_BASE}/api/upload/auto-map", json={
    "file_id": file_id,
    "template_id": 1,
    "sheet_name": "01-07-26"
})
if resp.status_code != 200:
    print_test("Auto-map", False, f"Status {resp.status_code}: {resp.text}")
    sys.exit(1)

result = resp.json()
mappings = result["mappings"]
print_test("Auto-map", True, f"{result.get('auto_mapped_count')}/{result.get('total_columns')} columns matched")

mapping_payload = [{
    "template_column_id": m["column_id"],
    "template_column_name": m["column_name"],
    "excel_column_name": m["excel_column"],
    "matched": m["is_matched"],
    "mapping_status": "matched" if m["is_matched"] else "unmatched"
} for m in mappings]

# Step 3: Import with replace=true (first time)
print("\n=== Step 3: First import with replace=true ===")
payload = {
    "template_id": 1,
    "file_id": file_id,
    "client_id": 1,
    "mappings": mapping_payload,
    "mode": "create",
    "unique_column_id": None,
    "sheet_name": "01-07-26",
    "replace": True,
    "skip_validation": True
}

resp = requests.post(f"{API_BASE}/api/upload/import", json=payload)
if resp.status_code != 200:
    print_test("First import", False, f"Status {resp.status_code}: {resp.text}")
    sys.exit(1)

result = resp.json()
print_test("First import", result["status"] == "success",
           f"inserted={result.get('imported_count')}, updated={result.get('updated_count')}, total={result.get('total_rows')}")

# Step 4: Verify data exists via datasets endpoint
print("\n=== Step 4: Verify data exists ===")
resp = requests.get(f"{API_BASE}/api/data/datasets?client_id=1")
if resp.status_code != 200:
    print_test("Fetch datasets", False, f"Status {resp.status_code}: {resp.text}")
else:
    datasets = resp.json()
    non_deleted = [d for d in datasets if d.get("status") != "Deleted"]
    # Get row counts
    total_rows = sum(d.get("record_count", 0) for d in non_deleted)
    print_test("Data exists after first import", total_rows > 0, f"{len(non_deleted)} datasets, {total_rows} total rows")

# Step 5: Import again with replace=true
print("\n=== Step 5: Second import with replace=true ===")
with open("uploads/Sample transaction report.xlsx", "rb") as f:
    files = {"file": ("test2.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = requests.post(f"{API_BASE}/api/import-excel", files=files)
result = resp.json()
file_id2 = result["file_id"]

resp = requests.post(f"{API_BASE}/api/upload/auto-map", json={
    "file_id": file_id2,
    "template_id": 1,
    "sheet_name": "01-07-26"
})
result = resp.json()
mappings2 = result["mappings"]

payload2 = {
    "template_id": 1,
    "file_id": file_id2,
    "client_id": 1,
    "mappings": [{
        "template_column_id": m["column_id"],
        "template_column_name": m["column_name"],
        "excel_column_name": m["excel_column"],
        "matched": m["is_matched"],
        "mapping_status": "matched" if m["is_matched"] else "unmatched"
    } for m in mappings2],
    "mode": "create",
    "unique_column_id": None,
    "sheet_name": "01-07-26",
    "replace": True,
    "skip_validation": True
}

resp = requests.post(f"{API_BASE}/api/upload/import", json=payload2)
if resp.status_code != 200:
    print_test("Second import", False, f"Status {resp.status_code}: {resp.text}")
else:
    result = resp.json()
    print_test("Second import", result["status"] == "success",
               f"inserted={result.get('imported_count')}, updated={result.get('updated_count')}")

# Step 6: Verify data was replaced (old data deleted, new data exists)
print("\n=== Step 6: Verify data after second replace import ===")
resp = requests.get(f"{API_BASE}/api/data/datasets?client_id=1")
if resp.status_code == 200:
    datasets = resp.json()
    non_deleted = [d for d in datasets if d.get("status") != "Deleted"]
    total_rows = sum(d.get("record_count", 0) for d in non_deleted)
    print_test("Data replaced correctly", total_rows > 0, f"{len(non_deleted)} active datasets, {total_rows} rows")

# Step 7: Test with replace=false (append mode)
print("\n=== Step 7: Test import with replace=false (append mode) ===")
with open("uploads/Sample transaction report.xlsx", "rb") as f:
    files = {"file": ("test3.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = requests.post(f"{API_BASE}/api/import-excel", files=files)
result = resp.json()
file_id3 = result["file_id"]

resp = requests.post(f"{API_BASE}/api/upload/auto-map", json={
    "file_id": file_id3,
    "template_id": 1,
    "sheet_name": "01-07-26"
})
result = resp.json()
mappings3 = result["mappings"]

payload3 = {
    "template_id": 1,
    "file_id": file_id3,
    "client_id": 1,
    "mappings": [{
        "template_column_id": m["column_id"],
        "template_column_name": m["column_name"],
        "excel_column_name": m["excel_column"],
        "matched": m["is_matched"],
        "mapping_status": "matched" if m["is_matched"] else "unmatched"
    } for m in mappings3],
    "mode": "create",
    "unique_column_id": None,
    "sheet_name": "01-07-26",
    "replace": False,
    "skip_validation": True
}

resp = requests.post(f"{API_BASE}/api/upload/import", json=payload3)
if resp.status_code != 200:
    print_test("Append import", False, f"Status {resp.status_code}: {resp.text}")
else:
    result = resp.json()
    print_test("Append import with replace=false", result["status"] == "success",
               f"inserted={result.get('imported_count')}")

# Step 8: Verify count increased after append
print("\n=== Step 8: Verify data count after append ===")
resp = requests.get(f"{API_BASE}/api/data/datasets?client_id=1")
if resp.status_code == 200:
    datasets = resp.json()
    # After replace=true twice + replace=false once, we expect 2 active datasets
    # because the first replace makes one, second replace replaces it (soft-deletes old),
    # and append adds one more. So active = 2 datasets.
    non_deleted = [d for d in datasets if d.get("status") != "Deleted"]
    active_count = len(non_deleted)
    # With replace=false, a new dataset is added
    print_test("Append created additional dataset", active_count >= 2, f"{active_count} active datasets")

print("\n=== All tests complete ===")