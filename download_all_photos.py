"""Download all player photos, team logos, and owner photos from Supabase Storage to local static/ folder."""

import os
import sys
import sqlite3
import urllib.request
import urllib.error
from sqlalchemy import create_engine, text

# Target directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE_DIR, "static", "player_photos"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "static", "team_logos"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "static", "owner_photos"), exist_ok=True)

SUPABASE_PG = "postgresql+psycopg2://postgres.bqoszyelbcxrqojvutva:Rachana%401237@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require"
PROJECT_URL = "https://bqoszyelbcxrqojvutva.supabase.co"
BUCKET = "gml-media"

def get_all_storage_objects():
    """Retrieve full list of all file paths stored in Supabase storage.objects."""
    try:
        engine = create_engine(SUPABASE_PG)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM storage.objects WHERE bucket_id = :bucket"), {"bucket": BUCKET}).fetchall()
            names = [r[0] for r in rows if r[0]]
            print(f"Found {len(names)} total files directly in Supabase storage.objects table.")
            return names
    except Exception as e:
        print(f"Could not read storage.objects from PostgreSQL ({e}). Falling back to SQLite...")
        return get_objects_from_sqlite()

def get_objects_from_sqlite():
    db_path = os.path.join(BASE_DIR, "instance", "league.db")
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT photo FROM player WHERE photo IS NOT NULL AND photo != ''")
    p_photos = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT team_logo, team_owner_photo FROM team_owner")
    o_photos = []
    for r in cur.fetchall():
        if r[0]: o_photos.append(r[0])
        if r[1]: o_photos.append(r[1])
    conn.close()

    names = []
    for u in p_photos + o_photos:
        # extract path e.g. player_photos/xyz.jpg
        if "/gml-media/" in u:
            names.append(u.split("/gml-media/")[-1])
        elif "player_photos/" in u:
            names.append("player_photos/" + u.split("player_photos/")[-1])
        elif "team_logos/" in u:
            names.append("team_logos/" + u.split("team_logos/")[-1])
        elif "owner_photos/" in u:
            names.append("owner_photos/" + u.split("owner_photos/")[-1])
    return list(set(names))

def download_with_key(key: str):
    key = key.strip().strip("'").strip('"')
    objects = get_all_storage_objects()
    print(f"Starting download of {len(objects)} files using provided key...")

    success = 0
    skipped = 0
    failed = 0

    endpoints_to_try = [
        # Authenticated endpoint (service_role bypass)
        lambda name: f"{PROJECT_URL}/storage/v1/object/authenticated/{BUCKET}/{name}",
        # Direct object endpoint
        lambda name: f"{PROJECT_URL}/storage/v1/object/{BUCKET}/{name}",
    ]

    for idx, obj_name in enumerate(objects, 1):
        filename = os.path.basename(obj_name)
        folder = os.path.dirname(obj_name) or "player_photos"
        dest_folder = os.path.join(BASE_DIR, "static", folder)
        os.makedirs(dest_folder, exist_ok=True)
        dest_path = os.path.join(dest_folder, filename)

        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 200:
            skipped += 1
            continue

        downloaded = False
        last_error = None
        for make_url in endpoints_to_try:
            url = make_url(obj_name)
            req = urllib.request.Request(url, headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "User-Agent": "Mozilla/5.0 GM-League-Storage-Backup"
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read()
                    with open(dest_path, "wb") as f:
                        f.write(content)
                    print(f"[{idx}/{len(objects)}] OK: {obj_name} ({len(content)} bytes)")
                    success += 1
                    downloaded = True
                    break
            except urllib.error.HTTPError as he:
                last_error = f"HTTP {he.code}: {he.read().decode('utf-8', errors='ignore')[:100]}"
            except Exception as ex:
                last_error = str(ex)

        if not downloaded:
            print(f"[{idx}/{len(objects)}] FAIL: {obj_name} -> {last_error}")
            failed += 1

    print("=" * 60)
    print(f"Completed! Downloaded: {success}, Already existed: {skipped}, Failed: {failed}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download_all_photos.py <service_role_key>")
        sys.exit(1)
    download_with_key(sys.argv[1])
