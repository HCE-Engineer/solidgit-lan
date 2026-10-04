"""Command line entry point.

Its first job is developer-facing: driving the core without a GUI, a second machine, or
SolidWorks. Every command takes `--workspace`, and the networking milestones add `--port` and
`--name`, so several peers can be run side by side on one machine. Testing a LAN app by
gathering three friends and a hotspot every time is not a workable loop.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from .appdata import load_device_identity
from .core.commits import Author, Relation, best_common_ancestor, compare_heads
from .core.reconcile import diff_manifests, summarise
from .core.repo import Repository, RepositoryError
from .net.client import PeerClient
from .net.discovery import PeerRegistry, UdpBeacon
from .net.protocol import DEFAULT_HTTPS_PORT, JoinStatus, Peer, ProtocolError, Role
from .net.security import local_ip_addresses
from .net.server import CoordinatorServer


def default_author() -> Author:
    """Identify the committer from the environment until real identity handling lands."""
    return Author(
        name=os.environ.get("SOLIDGIT_USER") or os.environ.get("USERNAME") or "unknown",
        device=os.environ.get("SOLIDGIT_DEVICE") or os.environ.get("COMPUTERNAME") or "unknown",
    )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_init(args: argparse.Namespace) -> int:
    repo = Repository.initialise(args.workspace, name=args.name)
    print(f"Initialised workspace '{repo.info.name}' at {repo.root}")
    print(f"  repo_id: {repo.info.repo_id}")
    status = repo.status()
    if status.added:
        print(f"  {len(status.added)} existing file(s) found — run 'commit' to record them.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = Repository(args.workspace)
    status = repo.status()
    head = repo.commits.head_commit()

    print(f"Workspace: {repo.root}")
    print(f"Project:   {repo.info.name}  ({repo.info.repo_id[:12]})")
    if head is None:
        print("HEAD:      (no commits yet)")
    else:
        print(f"HEAD:      {head.short_id}  #{head.lamport}  {head.message}")

    if status.is_clean:
        print("\nWorking folder is clean.")
        return 0

    for label, paths in (
        ("added", status.added),
        ("modified", status.modified),
        ("removed", status.removed),
    ):
        for path in sorted(paths):
            print(f"  {label:>8}: {path}")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    repo = Repository(args.workspace)
    try:
        commit = repo.commit(args.message, default_author())
    except RepositoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[{commit.short_id}] #{commit.lamport} {commit.message}")
    print(f"  {len(commit.manifest)} file(s), {_human_size(commit.manifest.total_size())}")
    print(f"  store on disk: {_human_size(repo.objects.disk_usage())}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    repo = Repository(args.workspace)
    history = repo.commits.history(limit=args.limit)
    if not history:
        print("No commits yet.")
        return 0
    for commit in history:
        marker = " (reconciliation)" if commit.is_reconciliation else ""
        print(f"{commit.short_id}  #{commit.lamport}{marker}")
        print(f"    {commit.author.name} on {commit.author.device} — {commit.wall_clock}")
        print(f"    {commit.message}")
        print(f"    {len(commit.manifest)} file(s), {_human_size(commit.manifest.total_size())}")
    return 0


def cmd_checkout(args: argparse.Namespace) -> int:
    repo = Repository(args.workspace)
    matches = [cid for cid in repo.commits.iter_ids() if cid.startswith(args.commit)]
    if len(matches) != 1:
        problem = "is ambiguous" if matches else "matches no commit"
        print(f"error: '{args.commit}' {problem}.", file=sys.stderr)
        return 1
    result = repo.checkout(matches[0], remove_extra=args.remove_extra)
    print(f"Workspace is now at {matches[0][:12]}")
    print(f"  {len(result.added | result.modified)} file(s) written")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare two local workspaces the way two peers would compare over the network."""
    mine = Repository(args.workspace)
    theirs = Repository(args.other)

    if mine.info.repo_id != theirs.info.repo_id:
        print("error: these are different projects (repo_id mismatch).", file=sys.stderr)
        return 1

    relation = compare_heads(
        mine.commits.head(), mine.commits.all_ids(), theirs.commits.head(), theirs.commits.all_ids()
    )
    print(f"Relation: {relation.value.upper()}")

    if relation is Relation.EQUAL:
        return 0

    # Each side knows only its own history, so the shared ancestor has to be found by
    # intersecting the two id sets — exactly what happens over the wire when a peer sends
    # its commit list.
    base_id = None
    mine_head, theirs_head = mine.commits.head(), theirs.commits.head()
    if mine_head and theirs_head:
        shared = mine.commits.ancestors(mine_head) & theirs.commits.ancestors(theirs_head)
        base_id = best_common_ancestor(
            shared,
            lambda cid: (mine.commits if mine.commits.has(cid) else theirs.commits)
            .read(cid)
            .lamport,
        )

    base = None
    if base_id:
        source = mine.commits if mine.commits.has(base_id) else theirs.commits
        base = source.read(base_id).manifest
        print(f"Merge base: {base_id[:12]}")

    diffs = diff_manifests(mine.head_manifest(), theirs.head_manifest(), base)
    for status, count in sorted(summarise(diffs).items()):
        if status != "same":
            print(f"  {status:>20}: {count}")

    conflicts = [d.path for d in diffs if d.needs_decision]
    if conflicts:
        print("\nNeeds a human decision:")
        for path in conflicts:
            print(f"  {path}")
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    repo = Repository(args.workspace)
    unreachable = repo.garbage_collect(dry_run=not args.delete)
    verb = "Deleted" if args.delete else "Would delete"
    print(f"{verb} {len(unreachable)} unreachable blob(s).")
    return 0


def _describe(peer: Peer) -> str:
    a = peer.announcement
    role = "HOST" if a.role is Role.HOST else "peer"
    head = a.head[:12] if a.head else "no commits"
    return f"  [{role}] {a.user_name} on {a.device_name} — {peer.url}  ({a.repo_name}, {head})"


def cmd_serve(args: argparse.Namespace) -> int:
    """Host a session: run the coordinator and announce it on the network."""
    repo = Repository(args.workspace)
    me = load_device_identity()

    server = CoordinatorServer(
        repo=repo,
        user_name=me.user_name,
        device_name=me.device_name,
        device_id=me.device_id,
        port=args.port,
        join_code=args.code,
        auto_approve=args.auto_approve,
        on_join_request=lambda r: print(
            f"\n>>> {r.user_name} on {r.device_name} wants to join. "
            f"Type 'y' to allow, 'n' to refuse.\n",
            flush=True,
        ),
    )
    server.start()

    print(f"Hosting '{repo.info.name}' on port {args.port}")
    print(f"  Join code:   {server.join_code}")
    print(f"  Certificate: {server.identity.short_fingerprint}")
    print(f"  Addresses:   {', '.join(local_ip_addresses())}")
    print(f"  mDNS:        {'on' if server.mdns_available else 'off (UDP beacon only)'}")
    if args.auto_approve:
        print("  Approval:    AUTOMATIC — for testing only.")
    print()

    if sys.stdin is not None and sys.stdin.isatty() and not args.auto_approve:
        threading.Thread(target=_approval_console, args=(server,), daemon=True).start()

    try:
        _watch_peers(server.registry, args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("\nStopped hosting.")
    return 0


def _approval_console(server: CoordinatorServer) -> None:
    """Approve or refuse pending devices from the terminal, when there is one."""
    for line in sys.stdin:
        answer = line.strip().lower()
        pending = server.pending_requests()
        if not pending:
            continue
        for request in pending:
            if answer.startswith("y"):
                server.approve(request.device_id, always_allow=answer == "ya")
                print(f"Allowed {request.user_name} ({request.device_name}).", flush=True)
            elif answer.startswith("n"):
                server.reject(request.device_id)
                print(f"Refused {request.user_name}.", flush=True)


def _watch_peers(registry: PeerRegistry, seconds: float | None) -> None:
    """Print the peer list whenever it changes, so a stale terminal is never misleading."""
    deadline = None if seconds is None else time.time() + seconds
    previous: tuple[str, ...] = ()
    while deadline is None or time.time() < deadline:
        now = time.time()
        registry.prune(now)
        current = tuple(p.device_id for p in registry.peers(now))
        if current != previous:
            peers = registry.peers(now)
            print(f"[{time.strftime('%H:%M:%S')}] {len(peers)} peer(s) on the network")
            for peer in peers:
                print(_describe(peer))
            previous = current
        time.sleep(0.5)


def cmd_peers(args: argparse.Namespace) -> int:
    """Listen for a while and report who is out there, without joining anything."""
    registry = PeerRegistry(self_device_id=load_device_identity().device_id)
    beacon = UdpBeacon(None, registry)  # Listen-only: we have nothing to announce yet.
    with beacon:
        print(f"Listening for {args.seconds:.0f}s...")
        time.sleep(args.seconds)
        found = registry.peers(time.time())

    if not found:
        print("No peers found.")
        print("  If a teammate is hosting, check that Windows Firewall is not blocking")
        print("  SolidGit LAN, and that you are both on the same network.")
        return 1
    print(f"{len(found)} peer(s):")
    for peer in found:
        print(_describe(peer))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    """Join a session someone else is hosting."""
    me = load_device_identity()
    client = PeerClient(
        device_id=me.device_id, device_name=me.device_name, user_name=me.user_name
    )

    host, port = args.host, args.port
    if host is None:
        found = _discover_host(me.device_id, args.discover_seconds)
        if found is None:
            print("error: no host found on this network.", file=sys.stderr)
            return 1
        host, port = found.address, found.announcement.port
        print(f"Found {found.announcement.user_name}'s session at {found.url}")

    try:
        with client.open(host, port) as connection:
            info = client.info(connection)
            print(f"Project:     {info.repo_name} ({info.repo_id[:12]})")
            print(f"Hosted by:   {info.user_name} on {info.device_name}")
            print(f"Certificate: {connection.short_fingerprint}")
            print(f"Head:        {info.head[:12] if info.head else 'no commits yet'}")

            if not args.code:
                print("\nNo join code given — stopping here. Pass --code to join.")
                return 0

            print("\nJoining...")
            result = client.join(
                connection, args.code, info.repo_id, wait_for_approval=args.wait
            )
    except ProtocolError as e:
        print(f"error [{e.code.value}]: {e.message}", file=sys.stderr)
        return 1

    if result.status is JoinStatus.APPROVED:
        print(f"Joined as {result.permission.value}.")
        return 0
    if result.status is JoinStatus.REJECTED:
        print("The host refused this device.", file=sys.stderr)
        return 1
    print("Waiting for the host to allow this device. Nothing was transferred.")
    return 2


def _discover_host(self_device_id: str, seconds: float) -> Peer | None:
    registry = PeerRegistry(self_device_id=self_device_id)
    with UdpBeacon(None, registry):
        deadline = time.time() + seconds
        while time.time() < deadline:
            hosts = [p for p in registry.peers(time.time()) if p.announcement.role is Role.HOST]
            if hosts:
                return hosts[0]
            time.sleep(0.3)
    return None


def cmd_build(args: argparse.Namespace) -> int:
    """Produce a package teammates can run, without opening the window."""
    from .packaging import build

    result = build(
        on_line=lambda line: print(line, flush=True),
        run_tests=not args.skip_tests,
        one_file=args.one_file,
    )
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1
    print(f"\n{result.package}  ({result.size_bytes / 1024 / 1024:.0f} MB)")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Open the desktop window."""
    from .ui import run

    workspace = args.workspace if Repository.is_workspace(args.workspace) else None
    return run(workspace)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solidgit-lan", description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace folder (default: current directory).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Turn a folder into a workspace.")
    p_init.add_argument("--name", help="Project name (default: folder name).")
    p_init.set_defaults(func=cmd_init)

    sub.add_parser("status", help="Show workspace changes against HEAD.").set_defaults(
        func=cmd_status
    )

    p_commit = sub.add_parser("commit", help="Record a snapshot of the workspace.")
    p_commit.add_argument("-m", "--message", required=True)
    p_commit.set_defaults(func=cmd_commit)

    p_log = sub.add_parser("log", help="Show commit history.")
    p_log.add_argument("-n", "--limit", type=int, default=20)
    p_log.set_defaults(func=cmd_log)

    p_checkout = sub.add_parser("checkout", help="Bring the workspace to a commit.")
    p_checkout.add_argument("commit", help="Commit id or unique prefix.")
    p_checkout.add_argument(
        "--remove-extra",
        action="store_true",
        help="Also delete files the target commit does not contain.",
    )
    p_checkout.set_defaults(func=cmd_checkout)

    p_compare = sub.add_parser("compare", help="Diff this workspace against another one.")
    p_compare.add_argument("other", type=Path, help="The other workspace folder.")
    p_compare.set_defaults(func=cmd_compare)

    p_gc = sub.add_parser("gc", help="Find blobs no commit references.")
    p_gc.add_argument("--delete", action="store_true", help="Actually remove them.")
    p_gc.set_defaults(func=cmd_gc)

    p_serve = sub.add_parser("serve", help="Host this workspace on the local network.")
    p_serve.add_argument("--port", type=int, default=DEFAULT_HTTPS_PORT)
    p_serve.add_argument("--code", help="Use a fixed join code instead of a fresh one.")
    p_serve.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the approval prompt. Testing only — it lets any device with the code in.",
    )
    p_serve.add_argument(
        "--seconds", type=float, default=None, help="Stop after this long (default: until Ctrl-C)."
    )
    p_serve.set_defaults(func=cmd_serve)

    p_peers = sub.add_parser("peers", help="List sessions visible on this network.")
    p_peers.add_argument("--seconds", type=float, default=6.0)
    p_peers.set_defaults(func=cmd_peers)

    p_connect = sub.add_parser("connect", help="Join a session hosted by a teammate.")
    p_connect.add_argument("--host", help="Skip discovery and connect to this address.")
    p_connect.add_argument("--port", type=int, default=DEFAULT_HTTPS_PORT)
    p_connect.add_argument("--code", help="The join code the host read out.")
    p_connect.add_argument("--discover-seconds", type=float, default=8.0)
    p_connect.add_argument(
        "--wait", type=float, default=30.0, help="How long to wait for the host to approve."
    )
    p_connect.set_defaults(func=cmd_connect)

    sub.add_parser("gui", help="Open the desktop window.").set_defaults(func=cmd_gui)

    p_build = sub.add_parser("build", help="Package a version teammates can run.")
    p_build.add_argument("--one-file", action="store_true", help="Single exe instead of a folder.")
    p_build.add_argument("--skip-tests", action="store_true", help="Do not run tests first.")
    p_build.set_defaults(func=cmd_build)

    return parser


def _make_output_utf8_safe() -> None:
    """Stop Turkish characters from killing the process on a legacy console.

    A Windows console defaults to cp1252, which cannot encode 'ğ' — so a progress message
    with a Turkish word in it raised UnicodeEncodeError and took the whole build down with
    it. Replacing unencodable characters is always better than crashing over a log line.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _make_output_utf8_safe()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RepositoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ProtocolError as e:
        print(f"error [{e.code.value}]: {e.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
