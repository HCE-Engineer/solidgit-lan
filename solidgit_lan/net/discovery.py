"""Finding the other machines on the hotspot.

Two mechanisms run at once on purpose. mDNS is the right answer and works on a normal LAN,
but some Windows Mobile Hotspot drivers smother multicast, and "my teammates can't see me" is
the failure this project cannot afford. A plain UDP broadcast every couple of seconds is
crude, tiny, and works when multicast does not.

The registry itself is pure and clock-injected, so peer arrival, staleness and departure can
be tested without sockets or waiting in real time.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable

from .protocol import (
    BEACON_INTERVAL_SECONDS,
    DISCOVERY_PORT,
    PEER_TIMEOUT_SECONDS,
    SERVICE_TYPE,
    Announcement,
    Peer,
    ProtocolError,
)

#: A beacon is a few hundred bytes; this leaves room for growth without risking fragmentation.
MAX_DATAGRAM = 4096


@dataclass
class PeerRegistry:
    """Who we have heard from lately. Pure — the caller supplies the time."""

    self_device_id: str = ""
    timeout: float = PEER_TIMEOUT_SECONDS
    _peers: dict[str, Peer] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, announcement: Announcement, address: str, now: float) -> Peer | None:
        """Record a sighting. Returns the peer, or None if it was our own beacon."""
        if announcement.device_id == self.self_device_id:
            return None
        peer = Peer(announcement=announcement, address=address, last_seen=now)
        with self._lock:
            self._peers[announcement.device_id] = peer
        return peer

    def prune(self, now: float) -> tuple[Peer, ...]:
        """Drop peers we have not heard from. Returns the ones that went away."""
        with self._lock:
            gone = tuple(p for p in self._peers.values() if p.is_stale(now, self.timeout))
            for peer in gone:
                del self._peers[peer.device_id]
        return gone

    def peers(self, now: float | None = None) -> tuple[Peer, ...]:
        with self._lock:
            found = tuple(self._peers.values())
        if now is not None:
            found = tuple(p for p in found if not p.is_stale(now, self.timeout))
        return tuple(sorted(found, key=lambda p: (p.announcement.user_name, p.device_id)))

    def for_repo(self, repo_id: str, now: float | None = None) -> tuple[Peer, ...]:
        return tuple(p for p in self.peers(now) if p.announcement.repo_id == repo_id)

    def get(self, device_id: str) -> Peer | None:
        with self._lock:
            return self._peers.get(device_id)


class UdpBeacon:
    """Announces us and listens for others on a fixed UDP port.

    Send and receive use separate sockets: the receiver binds the shared port with
    SO_REUSEADDR so several app instances can run on one machine, which is what makes local
    multi-peer testing possible at all.
    """

    def __init__(
        self,
        announcement_of: Callable[[], Announcement | None] | None,
        registry: PeerRegistry,
        port: int = DISCOVERY_PORT,
        interval: float = BEACON_INTERVAL_SECONDS,
        on_change: Callable[[], None] | None = None,
        extra_targets: Iterable[str] = (),
    ) -> None:
        self._announcement_of = announcement_of
        self._registry = registry
        self._port = port
        self._interval = interval
        self._on_change = on_change
        self._extra_targets = tuple(extra_targets)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._receiver: socket.socket | None = None
        self.errors: list[str] = []

    # -- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        self._receiver = self._make_receiver()
        for target in (self._receive_loop, self._send_loop):
            thread = threading.Thread(target=target, daemon=True, name=target.__name__)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._receiver is not None:
            # Closing from another thread is what unblocks recvfrom; the loop expects it.
            self._receiver.close()
            self._receiver = None
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def __enter__(self) -> UdpBeacon:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- sockets --------------------------------------------------------------------------

    def _make_receiver(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self._port))
        sock.settimeout(0.5)
        return sock

    def _targets(self) -> tuple[str, ...]:
        # 127.0.0.1 keeps several instances on one machine talking even with no network up.
        return ("255.255.255.255", "127.0.0.1", *self._extra_targets)

    def announce_once(self) -> None:
        # Two ways to say "nothing to announce": no provider at all (a pure listener), or a
        # provider that returns None right now. The app uses the second so one long-lived
        # beacon can stay silent until its owner starts hosting.
        if self._announcement_of is None:
            return
        announcement = self._announcement_of()
        if announcement is None:
            return
        payload = json.dumps(announcement.to_json()).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in self._targets():
                try:
                    sender.sendto(payload, (target, self._port))
                except OSError as e:
                    self._note_error(f"broadcast to {target} failed: {e}")

    # -- loops ----------------------------------------------------------------------------

    def _send_loop(self) -> None:
        if self._announcement_of is None:
            return
        while not self._stop.is_set():
            self.announce_once()
            self._stop.wait(self._interval)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._receiver
            if sock is None:
                return
            try:
                data, sender = sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                self._expire()
                continue
            except OSError:
                return  # Socket closed by stop().

            try:
                announcement = Announcement.from_json(json.loads(data.decode("utf-8")))
            except (ProtocolError, ValueError, UnicodeDecodeError) as e:
                # Anything on the Wi-Fi can send us a datagram; a bad one is not our problem.
                self._note_error(f"ignored malformed beacon from {sender[0]}: {e}")
                continue

            if self._registry.observe(announcement, sender[0], time.time()) and self._on_change:
                self._on_change()
            self._expire()

    def _expire(self) -> None:
        if self._registry.prune(time.time()) and self._on_change:
            self._on_change()

    def _note_error(self, message: str) -> None:
        self.errors.append(message)
        del self.errors[:-20]  # Keep only the most recent, for the diagnostics panel.


class MdnsResponder:
    """mDNS advertisement and browsing, when the network allows it.

    Wrapped in best-effort error handling and reported through `available`, because a hotspot
    that blocks multicast must degrade to the UDP beacon rather than take the app down.
    """

    def __init__(
        self,
        announcement_of: Callable[[], Announcement],
        registry: PeerRegistry,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._announcement_of = announcement_of
        self._registry = registry
        self._on_change = on_change
        self._zeroconf = None
        self._service_info = None
        self._browser = None
        self.available = False
        self.error = ""

    def start(self) -> None:
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

            announcement = self._announcement_of()
            self._zeroconf = Zeroconf()
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                f"{announcement.device_id[:12]}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(a) for a in _own_addresses()],
                port=announcement.port,
                properties={
                    key: ("" if value is None else str(value))
                    for key, value in announcement.to_json().items()
                },
            )
            self._zeroconf.register_service(self._service_info)
            self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, _MdnsListener(self))
            self.available = True
        except Exception as e:  # noqa: BLE001 - any failure here must be survivable
            self.error = str(e)
            self.available = False

    def stop(self) -> None:
        try:
            if self._zeroconf is not None and self._service_info is not None:
                self._zeroconf.unregister_service(self._service_info)
            if self._zeroconf is not None:
                self._zeroconf.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        finally:
            self._zeroconf = None
            self._service_info = None
            self._browser = None

    def _observe(self, properties: dict[str, str], address: str) -> None:
        try:
            announcement = Announcement.from_json(_decode_txt(properties))
        except ProtocolError:
            return
        if self._registry.observe(announcement, address, time.time()) and self._on_change:
            self._on_change()


class _MdnsListener:
    """zeroconf calls these; each one just forwards a sighting to the registry."""

    def __init__(self, responder: MdnsResponder) -> None:
        self._responder = responder

    def add_service(self, zeroconf, service_type, name) -> None:
        self._handle(zeroconf, service_type, name)

    def update_service(self, zeroconf, service_type, name) -> None:
        self._handle(zeroconf, service_type, name)

    def remove_service(self, zeroconf, service_type, name) -> None:
        # Left to the UDP beacon's staleness timeout: mDNS goodbyes are unreliable on a
        # hotspot, and a peer vanishing from the list because of a lost packet is worse.
        return

    def _handle(self, zeroconf, service_type, name) -> None:
        # This runs on zeroconf's own browser thread. An exception escaping here (e.g.
        # EventLoopBlocked while zeroconf shuts down or the machine is busy) killed that
        # thread, silently ending mDNS discovery for the rest of the session.
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception:  # noqa: BLE001 - the UDP beacon still covers discovery
            return
        if info is None:
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        properties = {
            _as_text(key): _as_text(value) for key, value in (info.properties or {}).items()
        }
        self._responder._observe(properties, addresses[0])


def _as_text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _decode_txt(properties: dict[str, str]) -> dict[str, object]:
    """TXT records are all strings; restore the few fields that are not."""
    data: dict[str, object] = dict(properties)
    for key in ("proto", "port"):
        if key in data:
            data[key] = int(str(data[key]))
    if data.get("head") in ("", "None"):
        data["head"] = None
    return data


def _own_addresses() -> list[str]:
    try:
        return [
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ] or ["127.0.0.1"]
    except socket.gaierror:
        return ["127.0.0.1"]
