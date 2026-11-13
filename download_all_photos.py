"""Download all player photos, team logos, and owner photos from Supabase Storage to local static/ folder."""

import os
import sys
import sqlite3
import urllib.request
import urllib.error

# Ensure target directories exist
os.makedirs("static/player_photos", exist_ok=True)
os.makedirs("static/team_logos", exist_ok=True)
os.makedirs("static/owner_photos", exist_ok=True)

DB_PATH = os.path.abspath("instance/league.db")

def get_all_photo_urls():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("SELECT id, name, photo FROM player WHERE photo IS NOT NULL AND photo != ''")
    players = cur.fetchall()
    
    cur.execute("SELECT id, team_name, team_logo, team_owner_photo FROM team_owner")
    owners = cur.fetchall()
    
    conn.close()
    return players, owners

def download_with_key(service_role_key: str):
    players, owners = get_all_photo_urls()
    print(f"Total player photos to check: {len(players)}")
    print(f"Total team owner assets to check: {len(owners)}")

    success_count = 0
    fail_count = 0

    # 1. Download player photos
    for pid, name, url in players:
        if not url:
            continue
        filename = url.split("/")[-1].split("?")[0]
        local_path = os.path.join("static", "player_photos", filename)

        if os.path.exists(local_path) and os.path.getsize(local_path) > 100:
            print(f"[Skip] Already exists: {filename}")
            success_count += 1
            continue

        # Convert public URL to authenticated storage URL if using service_role
        auth_url = url.replace("/storage/v1/object/public/", "/storage/v1/object/authenticated/")
        req = urllib.request.Request(auth_url, headers={
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "User-Agent": "GM-League-Backup/1.0"
        })

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                with open(local_path, "wb") as f:
                    f.write(data)
                print(f"[Downloaded] {name}: {filename} ({len(data)} bytes)")
                success_count += 1
        except Exception as e:
            print(f"[Failed] {name} ({url}): {e}")
            fail_count += 1

    # 2. Download team logos & owner photos
    for oid, team_name, logo_url, owner_photo_url in owners:
        for u, folder in [(logo_url, "team_logos"), (owner_photo_url, "owner_photos")]:
            if not u:
                continue
            filename = u.split("/")[-1].split("?")[0]
            local_path = os.path.join("static", folder, filename)

            if os.path.exists(local_path) and os.path.getsize(local_path) > 100:
                print(f"[Skip] Already exists: {filename}")
                success_count += 1
                continue

            auth_url = u.replace("/storage/v1/object/public/", "/storage/v1/object/authenticated/")
            req = urllib.request.Request(auth_url, headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "User-Agent": "GM-League-Backup/1.0"
            })

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                    with open(local_path, "wb") as f:
                        f.write(data)
                    print(f"[Downloaded] {team_name} ({folder}): {filename} ({len(data)} bytes)")
                    success_count += 1
            except Exception as e:
                print(f"[Failed] {team_name} ({u}): {e}")
                fail_count += 1

    print("=" * 60)
    print(f"Download complete: {success_count} succeeded, {fail_count} failed.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        key = sys.argv[1].strip()
        download_with_key(key)
    else:
        key = input("Enter your Supabase service_role secret key: ").strip()
        if key:
            download_with_key(key)
        else:
            print("No key provided. Exiting.")
