"""SSH tunnel to the receiver: local NBD + API ports -> receiver 127.0.0.1:10809/8787.

The receiver's agent listens on localhost only; the tunnel is the sole data
and control path (encrypted, authenticated with the per-migration key).
"""
import json
import shlex
import socket
import subprocess
import time
import urllib.request

from .util import log

REMOTE_NBD, REMOTE_API = 10809, 8787


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CIPHERS = "aes128-gcm@openssh.com,aes256-gcm@openssh.com,chacha20-poly1305@openssh.com"   # AES-NI first


class Tunnel:
    def __init__(self, host, user, key, known_hosts, jump=None):
        """jump: {"host", "user", "key", "known_hosts"} of a relay to go through
        (rehearsal copies have no public IP)."""
        self.host, self.user, self.key, self.known_hosts = host, user, key, known_hosts
        self.jump = jump
        self.proc = None
        self.nbd_port = self.api_port = None

    @property
    def api(self):
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def nbd(self):
        return f"127.0.0.1:{self.nbd_port}"

    def ssh_base(self):
        base = ["ssh", "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ConnectTimeout=15",
                "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", f"Ciphers={CIPHERS}"]
        if self.jump:
            # ProxyCommand, not -J: -J would not carry these options to the relay hop
            j = self.jump
            base += ["-o", "ProxyCommand=" + " ".join(shlex.quote(x) for x in [
                "ssh", "-i", j["key"], "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={j['known_hosts']}",
                "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=15",
                "-o", f"Ciphers={CIPHERS}", "-W", "%h:%p", f"{j['user']}@{j['host']}"])]
        return base

    def start(self, timeout=60):
        self.nbd_port, self.api_port = free_port(), free_port()
        cmd = self.ssh_base() + ["-N", "-o", "ExitOnForwardFailure=yes",
                                 "-L", f"{self.nbd_port}:127.0.0.1:{REMOTE_NBD}",
                                 "-L", f"{self.api_port}:127.0.0.1:{REMOTE_API}",
                                 f"{self.user}@{self.host}"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise ConnectionError(f"ssh tunnel exited: {self.proc.stderr.read().decode()[-300:]}")
            try:
                self.get("/status", timeout=5)
                return self
            except OSError:
                time.sleep(1)
        self.stop()
        raise ConnectionError("receiver API not reachable through the tunnel")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        if not self.alive():
            log("tunnel down - reconnecting")
            self.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- receiver API ---
    def call(self, method, path, body=None, timeout=600):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def get(self, path, timeout=600):
        return self.call("GET", path, timeout=timeout)

    def post(self, path, body=None, timeout=600):
        return self.call("POST", path, body, timeout=timeout)

    def ssh(self, command, input=None, timeout=1800):
        """Run a command on the receiver (RAM root) as its login user."""
        return subprocess.run(self.ssh_base() + [f"{self.user}@{self.host}", command], input=input,
                              capture_output=True, text=True, timeout=timeout)
