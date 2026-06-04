"""
d2a/swarm.py — frozen swarm transport interface + built-in LAN transport.

The SwarmTransport ABC is the ONLY contract that matters for D2A.
LANSwarm ships built-in and works on any device with no setup.
DHTSwarm (swarm_dht.py) plugs into the same interface later.
"""

import abc
import json
import socket
import threading
import time

TTL = 30  # seconds — records older than this are pruned from discover()


class SwarmTransport(abc.ABC):
    """
    Frozen transport contract. Three operations only.
    A capability record is:
      {"node_id", "name", "tags", "live_state", "public_key", "address": [ip, port], "ts"}
    """

    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def publish(self, record: dict) -> None:
        """Announce a capability record to the network."""

    @abc.abstractmethod
    def discover(self, capability_name: str = None) -> list[dict]:
        """Return live capability records. None = all."""

    @abc.abstractmethod
    def send(self, target_node_id: str, message: dict) -> bool:
        """Fire-and-forget direct message to a node. Returns True if delivered."""


# ── built-in LAN transport ─────────────────────────────────────────────────────

class LANSwarm(SwarmTransport):
    """
    Zero-setup LAN transport. Works on any Linux-ish device out of the box.

    Discovery: UDP broadcast on discovery_port (default 50055).
      - publish()  → broadcasts {"type":"announce", "record":...}
      - discover() → broadcasts {"type":"query"}, waits 1.5 s, returns local cache

    Messaging: TCP on an OS-assigned free port.
      - send()          → fire-and-forget JSON line
      - send_and_recv() → request/response on same connection (for bind_request)

    Fallbacks:
      - add_known_peer(node_id, ip, port): manual seed when UDP broadcast is blocked
        (AP isolation, Docker bridge, same-machine loopback)
      - probe_peer(ip, port): TCP probe that fetches capabilities from a known address
        without needing the remote node_id first
    """

    def __init__(
        self,
        node_id: str,
        host: str = "0.0.0.0",
        port: int = 0,
        discovery_port: int = 50055,
    ):
        self.node_id = node_id
        self.host = host
        self.discovery_port = discovery_port
        self._lock = threading.Lock()
        self.records: dict = {}       # (node_id, cap_name) -> record dict
        self._peers: dict = {}        # node_id -> (ip, port)
        self.message_handler = None   # callable(msg: dict) -> dict | None
        self._running = False
        self._udp_bound = False

        # TCP server — OS picks a free port immediately
        self._tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_srv.bind((host, port))
        self._tcp_srv.listen(32)
        self.port: int = self._tcp_srv.getsockname()[1]

    @property
    def address(self) -> tuple[str, int]:
        return (self._local_ip(), self.port)

    def _local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True

        # UDP discovery listener
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.bind(("", self.discovery_port))
            self._udp_bound = True
            threading.Thread(target=self._udp_loop, args=(udp,), daemon=True).start()
        except OSError as e:
            print(f"[LANSwarm:{self.node_id[:8]}] UDP unavailable on :{self.discovery_port}: {e}. TCP-only.")

        # TCP message server
        threading.Thread(target=self._tcp_loop, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        try:
            self._tcp_srv.close()
        except Exception:
            pass

    # ── UDP discovery ──────────────────────────────────────────────────────────

    def _udp_loop(self, sock: socket.socket) -> None:
        sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = sock.recvfrom(65535)
                try:
                    self._handle_udp(json.loads(data.decode()), addr)
                except Exception:
                    pass
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_udp(self, msg: dict, addr) -> None:
        mtype = msg.get("type")
        if mtype == "announce":
            rec = msg.get("record", {})
            nid = rec.get("node_id")
            if not nid:
                return
            rec["ts"] = time.time()
            with self._lock:
                self.records[(nid, rec.get("name", ""))] = rec
                if rec.get("address"):
                    self._peers[nid] = tuple(rec["address"])
        elif mtype == "query":
            # Re-announce own records so querying nodes learn us
            with self._lock:
                own = [r for r in self.records.values() if r.get("node_id") == self.node_id]
            for rec in own:
                self._broadcast({"type": "announce", "record": rec})

    def _broadcast(self, msg: dict) -> None:
        try:
            data = json.dumps(msg, default=str).encode()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.0)
            s.sendto(data, ("<broadcast>", self.discovery_port))
            s.close()
        except Exception:
            pass

    # ── TCP messaging ──────────────────────────────────────────────────────────

    def _tcp_loop(self) -> None:
        self._tcp_srv.settimeout(1.0)
        while self._running:
            try:
                conn, _ = self._tcp_srv.accept()
                threading.Thread(target=self._handle_tcp, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_tcp(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65535)
                if not chunk:
                    break
                data += chunk
            line = data.split(b"\n")[0].strip()
            if not line:
                return
            msg = json.loads(line.decode())
            response = self.message_handler(msg) if self.message_handler else None
            if response is not None:
                conn.sendall((json.dumps(response, default=str) + "\n").encode())
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── SwarmTransport interface ───────────────────────────────────────────────

    def publish(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("ts", time.time())
        with self._lock:
            self.records[(rec.get("node_id", ""), rec.get("name", ""))] = rec
            if rec.get("node_id") and rec.get("address"):
                self._peers[rec["node_id"]] = tuple(rec["address"])
        self._broadcast({"type": "announce", "record": rec})

    def discover(self, capability_name: str = None) -> list[dict]:
        if self._udp_bound:
            self._broadcast({"type": "query", "name": capability_name})
            time.sleep(1.5)
        now = time.time()
        with self._lock:
            return [
                dict(r) for r in self.records.values()
                if now - r.get("ts", 0) <= TTL
                and (capability_name is None or r.get("name") == capability_name)
            ]

    def send(self, target_node_id: str, message: dict) -> bool:
        return self._tcp_send(target_node_id, message) is True

    # ── Extensions (not in ABC) ────────────────────────────────────────────────

    def send_and_recv(self, target_node_id: str, message: dict, timeout: float = 5.0) -> dict | None:
        """Request-response over TCP. Used by RemoteAgent.bind_remote()."""
        return self._tcp_send(target_node_id, message, recv=True, timeout=timeout)

    def _tcp_send(self, target_node_id: str, message: dict, recv: bool = False, timeout: float = 5.0):
        with self._lock:
            addr = self._peers.get(target_node_id)
        if not addr:
            return None if recv else False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall((json.dumps(message, default=str) + "\n").encode())
            if recv:
                data = b""
                while b"\n" not in data:
                    chunk = s.recv(65535)
                    if not chunk:
                        break
                    data += chunk
                s.close()
                line = data.split(b"\n")[0].strip()
                return json.loads(line.decode()) if line else None
            s.close()
            return True
        except Exception:
            return None if recv else False

    def add_known_peer(self, node_id: str, ip: str, port: int) -> None:
        """Manual peer seed — use when UDP broadcast is blocked (AP isolation, Docker, loopback)."""
        with self._lock:
            self._peers[node_id] = (ip, port)

    def probe_peer(self, ip: str, port: int) -> list[dict]:
        """
        TCP probe to a known address — discovers capabilities without UDP.
        Populates local records + peer table from the response.
        Use when the provider's IP:port is known but node_id is not.
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, port))
            s.sendall((json.dumps({"type": "capabilities_request", "from_node": self.node_id}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65535)
                if not chunk:
                    break
                data += chunk
            s.close()
            line = data.split(b"\n")[0].strip()
            if not line:
                return []
            records = json.loads(line.decode()).get("records", [])
            now = time.time()
            with self._lock:
                for r in records:
                    r["ts"] = now
                    self.records[(r.get("node_id", ""), r.get("name", ""))] = r
                    if r.get("node_id") and r.get("address"):
                        self._peers[r["node_id"]] = tuple(r["address"])
            return records
        except Exception:
            return []
