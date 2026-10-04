from __future__ import annotations

import socket
import time


from solidgit_lan.net.discovery import PeerRegistry, UdpBeacon
from solidgit_lan.net.protocol import PEER_TIMEOUT_SECONDS, Announcement, Role


def announcement(device_id: str = "dev-a", user: str = "Ahmet", port: int = 7801) -> Announcement:
    return Announcement(
        repo_id="repo-1",
        repo_name="Montaj1",
        device_id=device_id,
        device_name=device_id.upper(),
        user_name=user,
        role=Role.PEER,
        port=port,
        head="c" * 64,
        fingerprint="9C1B4F2A",
    )


def test_announcement_survives_a_round_trip():
    original = announcement()
    assert Announcement.from_json(original.to_json()) == original


def test_announcement_round_trip_with_no_commits_yet():
    original = announcement()
    empty_head = Announcement(**{**original.__dict__, "head": None})
    assert Announcement.from_json(empty_head.to_json()).head is None


def test_registry_ignores_our_own_beacon():
    """Every node hears its own broadcast; showing yourself in the peer list is noise."""
    registry = PeerRegistry(self_device_id="me")
    assert registry.observe(announcement("me"), "127.0.0.1", now=0.0) is None
    assert registry.peers() == ()


def test_registry_tracks_and_refreshes_a_peer():
    registry = PeerRegistry(self_device_id="me")
    registry.observe(announcement("dev-a"), "192.168.137.42", now=0.0)
    registry.observe(announcement("dev-a"), "192.168.137.42", now=5.0)

    peers = registry.peers()
    assert len(peers) == 1
    assert peers[0].last_seen == 5.0
    assert peers[0].url == "https://192.168.137.42:7801"


def test_peer_drops_out_after_missed_beacons():
    registry = PeerRegistry(self_device_id="me")
    registry.observe(announcement("dev-a"), "10.0.0.2", now=0.0)

    assert registry.peers(now=PEER_TIMEOUT_SECONDS - 1) != ()
    gone = registry.prune(now=PEER_TIMEOUT_SECONDS + 1)

    assert [p.device_id for p in gone] == ["dev-a"]
    assert registry.peers() == ()


def test_registry_separates_projects():
    registry = PeerRegistry(self_device_id="me")
    registry.observe(announcement("dev-a"), "10.0.0.2", now=0.0)
    other = Announcement(**{**announcement("dev-b").__dict__, "repo_id": "repo-2"})
    registry.observe(other, "10.0.0.3", now=0.0)

    assert [p.device_id for p in registry.for_repo("repo-1")] == ["dev-a"]


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("", 0))
        return probe.getsockname()[1]


def test_two_beacons_on_one_machine_find_each_other():
    """The multi-instance loop this project depends on: two peers, one laptop, no hotspot."""
    port = free_udp_port()
    registry_a = PeerRegistry(self_device_id="dev-a")
    registry_b = PeerRegistry(self_device_id="dev-b")

    beacon_a = UdpBeacon(lambda: announcement("dev-a", "Ahmet"), registry_a, port=port, interval=0.2)
    beacon_b = UdpBeacon(
        lambda: announcement("dev-b", "Hüseyin"), registry_b, port=port, interval=0.2
    )

    with beacon_a, beacon_b:
        deadline = time.time() + 15
        while time.time() < deadline:
            if registry_a.peers() and registry_b.peers():
                break
            time.sleep(0.2)

    assert [p.announcement.user_name for p in registry_a.peers()] == ["Hüseyin"]
    assert [p.announcement.user_name for p in registry_b.peers()] == ["Ahmet"]


def test_beacon_stays_silent_while_there_is_nothing_to_announce():
    """The app runs one long-lived beacon that only speaks up once its owner starts hosting."""
    port = free_udp_port()
    listener_registry = PeerRegistry(self_device_id="listener")
    announcing = {"on": False}

    quiet = UdpBeacon(
        lambda: announcement("dev-a") if announcing["on"] else None,
        PeerRegistry(self_device_id="dev-a"),
        port=port,
        interval=0.2,
    )
    listener = UdpBeacon(None, listener_registry, port=port, interval=0.2)

    with quiet, listener:
        time.sleep(1.2)
        assert listener_registry.peers() == ()  # silent, and still running

        announcing["on"] = True
        deadline = time.time() + 10
        while time.time() < deadline and not listener_registry.peers():
            time.sleep(0.2)

    assert [p.device_id for p in listener_registry.peers()] == ["dev-a"]
    assert quiet.errors == []


def test_malformed_datagram_does_not_take_the_beacon_down():
    """Anything on the Wi-Fi can send us a packet; a bad one must not be fatal."""
    port = free_udp_port()
    registry = PeerRegistry(self_device_id="dev-a")
    beacon = UdpBeacon(lambda: announcement("dev-a"), registry, port=port, interval=0.2)

    with beacon:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"this is not json", ("127.0.0.1", port))
        time.sleep(1.0)
        # Still running and still able to hear a genuine peer.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            import json

            sender.sendto(
                json.dumps(announcement("dev-b").to_json()).encode("utf-8"),
                ("127.0.0.1", port),
            )
            deadline = time.time() + 10
            while time.time() < deadline and not registry.peers():
                time.sleep(0.2)

    assert [p.device_id for p in registry.peers()] == ["dev-b"]
    assert any("malformed" in message for message in beacon.errors)
