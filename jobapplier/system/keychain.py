import subprocess


def keychain(service: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, check=True)
    return out.stdout.strip()
