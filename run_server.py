"""Production server launcher for Windows VM using Waitress WSGI."""
import os
from dotenv import load_dotenv

load_dotenv()

from app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    host = os.getenv("HOST", "0.0.0.0")
    print("=" * 60)
    print("  GM League Season 4 Production Server Starting")
    print(f"  Local Access:   http://localhost:{port}")
    print(f"  Network Access: http://{host}:{port}")
    print("=" * 60)
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        print("[Notice] 'waitress' not found. Falling back to built-in Flask WSGI server...")
        app.run(host=host, port=port, debug=False)

