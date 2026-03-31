"""
launch.py — Redis Operator launcher
Starts the Flask backend and opens the dashboard in the default browser.
Run this file to start Redis Operator: python launch.py
"""

import os
import sys
import time
import signal
import threading
import webbrowser
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"

def wait_for_server(timeout=15):
    import socket
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.create_connection((HOST, PORT), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False

def run_flask():
    """Run Flask app in-process (same Python interpreter)."""
    os.chdir(BASE_DIR)
    # Import and run the app
    sys.path.insert(0, str(BASE_DIR))
    from app import application
    application.run(host=HOST, port=PORT, debug=False, use_reloader=False)

def main():
    print("=" * 50)
    print("  Redis Operator")
    print("=" * 50)
    print(f"  Starting server at {URL} ...")

    # Run Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Wait for server to be ready
    if wait_for_server(timeout=15):
        print(f"  Server ready. Opening browser...")
        webbrowser.open(URL)
    else:
        print(f"  WARNING: Server did not respond within timeout.")
        print(f"  Try opening {URL} manually.")

    print(f"  Press Ctrl+C to stop Redis Operator.\n")

    # Keep the main thread alive
    try:
        while flask_thread.is_alive():
            flask_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\n  Shutting down Redis Operator...")
        sys.exit(0)

if __name__ == "__main__":
    main()
