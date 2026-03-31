"""
example_task.py — sample scheduled task for Redis Operator
"""
import datetime
import os

def run():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Example task executed.")
    # Write a timestamped line to a log file in the same directory
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example_output.log")
    with open(log_path, "a") as f:
        f.write(f"[{ts}] Task ran successfully.\n")

if __name__ == "__main__":
    run()
