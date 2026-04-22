# backend/browser/utils.py
import socket
import subprocess
import re


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def get_port(start=9222) -> int:
    port = start
    while is_port_in_use(port):
        port += 1
    return port


def kill_by_port(port: int):
    cmd = f'netstat -ano | findstr :{port}'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    pids = set()
    for line in result.stdout.splitlines():
        m = re.search(r'\s+(\d+)$', line)
        if m:
            pids.add(m.group(1))

    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", pid, "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
