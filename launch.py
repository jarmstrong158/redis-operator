"""
launch.py — Redis Operator launcher
Starts the Flask backend and opens the dashboard in the default browser.
Run this file to start Redis Operator: python launch.py
"""

import os
import sys
import time
import threading
import webbrowser
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent.resolve()
else:
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
    sys.path.insert(0, str(BASE_DIR))
    from app import application
    application.run(host=HOST, port=PORT, debug=False, use_reloader=False)


def _make_tray_image():
    """Build a 64x64 RGBA PIL Image for the system tray icon."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Blue circle background
    draw.ellipse([2, 2, 62, 62], fill=(0, 119, 255, 255))
    # "RO" text centered
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    draw.text((32, 32), "RO", fill=(255, 255, 255, 255), font=font, anchor="mm")
    return img


def _run_tray(stop_event):
    """Run a system tray icon. Blocks until the icon is stopped."""
    try:
        import pystray
    except ImportError:
        return  # silently skip if pystray not installed

    img = _make_tray_image()
    if img is None:
        return

    def open_dashboard(icon, item):
        webbrowser.open(URL)

    def stop_app(icon, item):
        icon.stop()
        stop_event.set()

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Stop Redis Operator", stop_app),
    )
    icon = pystray.Icon("Redis Operator", img, "Redis Operator", menu)
    icon.run()


def main():
    print("=" * 50)
    print("  Redis Operator")
    print("=" * 50)
    print(f"  Starting server at {URL} ...")

    # Run Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Wait for server to be ready
    if wait_for_server(timeout=30):
        print(f"  Server ready. Opening browser...")
        webbrowser.open(URL)
    else:
        print(f"  WARNING: Server did not respond within timeout.")
        print(f"  Try opening {URL} manually.")

    print(f"  Press Ctrl+C to stop Redis Operator.\n")
    print(f"  A system tray icon will appear if pystray + Pillow are installed.")

    # Start tray icon in background thread
    stop_event = threading.Event()
    tray_thread = threading.Thread(target=_run_tray, args=(stop_event,), daemon=True)
    tray_thread.start()

    # Keep the main thread alive until Ctrl+C or tray Stop
    try:
        while flask_thread.is_alive() and not stop_event.is_set():
            flask_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\n  Shutting down Redis Operator...")
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
