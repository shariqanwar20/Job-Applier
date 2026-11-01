import json
import subprocess


def notify(title: str, message: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"], check=False)
