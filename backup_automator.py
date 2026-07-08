# ==============================================
# 💾 AUTOMATED BACKUP SCRIPT
# ==============================================
# Run this script manually or schedule it via Windows Task Scheduler / cron
# It creates a complete backup of your system (schema + all data)

import requests
import json
import os
from datetime import datetime

# ---------- CONFIGURATION ----------
API_URL = "http://localhost:5000"  # Change if running on different port/host
BACKUP_DIR = "backups"  # Folder where backups will be saved

# ---------- CREATE BACKUP FOLDER ----------
os.makedirs(BACKUP_DIR, exist_ok=True)

# ---------- EXPORT BACKUP ----------
def create_backup():
    print(f"📤 Exporting backup from {API_URL}...")
    
    try:
        response = requests.get(f"{API_URL}/api/backup/export")
        if response.status_code != 200:
            print(f"❌ Error: {response.status_code} - {response.text}")
            return None
        
        data = response.json()
        
        # Add metadata
        timestamp = datetime.now().isoformat()
        data["_metadata"] = {
            "backup_date": timestamp,
            "version": "1.0",
            "record_counts": {
                "columns": len(data.get("columns", [])),
                "templates": len(data.get("templates", [])),
                "mappings": len(data.get("mappings", [])),
                "clients": len(data.get("clients", [])),
                "client_data": len(data.get("client_data", []))
            }
        }
        
        # Save to file
        filename = f"full_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(BACKUP_DIR, filename)
        
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)
        
        print(f"✅ Backup saved to: {filepath}")
        print(f"   📊 Stats: {data['_metadata']['record_counts']}")
        return filepath
        
    except requests.exceptions.ConnectionError:
        print(f"❌ Error: Cannot connect to server at {API_URL}. Is it running?")
        return None
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return None

# ---------- RESTORE BACKUP ----------
def restore_backup(filepath):
    print(f"📥 Restoring backup from {filepath}...")
    
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        
        # Remove metadata before sending
        if "_metadata" in data:
            del data["_metadata"]
        
        response = requests.post(
            f"{API_URL}/api/backup/import",
            json=data,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code != 200:
            print(f"❌ Error: {response.status_code} - {response.text}")
            return False
        
        result = response.json()
        print(f"✅ Restore successful: {result.get('message', '')}")
        return True
        
    except FileNotFoundError:
        print(f"❌ Error: File not found: {filepath}")
        return False
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return False

# ---------- LIST BACKUPS ----------
def list_backups():
    if not os.path.exists(BACKUP_DIR):
        print("📁 No backup folder found.")
        return []
    
    files = [f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')]
    if not files:
        print("📁 No backup files found.")
        return []
    
    print("\n📁 Available backups:")
    for idx, f in enumerate(sorted(files, reverse=True)):
        filepath = os.path.join(BACKUP_DIR, f)
        size = os.path.getsize(filepath) / 1024  # KB
        print(f"  {idx+1}. {f} ({size:.1f} KB)")
    
    return files

# ---------- MAIN ----------
if __name__ == "__main__":
    print("=" * 50)
    print("💾 System Analyst - Backup Tool")
    print("=" * 50)
    print("1. Create a new backup")
    print("2. Restore from backup")
    print("3. List backups")
    print("4. Exit")
    
    choice = input("\nSelect option (1-4): ").strip()
    
    if choice == "1":
        create_backup()
    elif choice == "2":
        files = list_backups()
        if files:
            try:
                idx = int(input("\nSelect backup number to restore: ")) - 1
                if 0 <= idx < len(files):
                    filepath = os.path.join(BACKUP_DIR, files[idx])
                    confirm = input(f"⚠️ This will REPLACE ALL data! Continue? (y/n): ")
                    if confirm.lower() == 'y':
                        restore_backup(filepath)
                    else:
                        print("❌ Restore cancelled.")
                else:
                    print("❌ Invalid selection.")
            except ValueError:
                print("❌ Invalid input.")
    elif choice == "3":
        list_backups()
    else:
        print("👋 Goodbye!")