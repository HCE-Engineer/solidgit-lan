"""The repository: a workspace folder plus the `.solidgit` metadata beside it.

Layout:

    <workspace>/
      Montaj1/…            the real files the user works on
      .solidgit/
        repo.json          repo_id, name, protocol version
        objects/           content-addressed blobs
        commits/           one JSON file per commit
        HEAD               current commit id
        tmp/               scratch space for atomic writes
"""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Iterable, Mapping

from .. import PROTOCOL_VERSION
from .commits import Author, Commit, CommitStore, build_commit
from .ignore import DEFAULT_RULES, REPO_DIR, IgnoreRules
from .manifest import Manifest, StatCache, changed_paths, scan_workspace
from .objects import ObjectStore, clear_readonly


class RepositoryError(Exception):
    """Raised when a workspace is not a repository, or its metadata is unusable."""


@dataclass(frozen=True)
class RepoInfo:
    repo_id: str
    name: str
    protocol: int
    created_at: str

    def to_json(self) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "name": self.name,
            "protocol": self.protocol,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_json(data: Mapping[str, object]) -> RepoInfo:
        try:
            return RepoInfo(
                repo_id=str(data["repo_id"]),
                name=str(data["name"]),
                protocol=int(data["protocol"]),  # type: ignore[arg-type]
                created_at=str(data["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise RepositoryError(f"Malformed repo.json: {e}") from e


@dataclass(frozen=True)
class WorkspaceStatus:
    """What the working folder looks like against the current commit."""

    head: str | None
    added: frozenset[str]
    modified: frozenset[str]
    removed: frozenset[str]

    @property
    def is_clean(self) -> bool:
        return not (self.added or self.modified or self.removed)

    @property
    def changed(self) -> frozenset[str]:
        return self.added | self.modified | self.removed


def utc_now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


class Repository:
    """A workspace and its history. Local only — nothing here knows about the network."""

    def __init__(self, root: str | Path, rules: IgnoreRules = DEFAULT_RULES) -> None:
        self.root = Path(root).resolve()
        self.repo_dir = self.root / REPO_DIR
        self.rules = rules
        if not (self.repo_dir / "repo.json").exists():
            raise RepositoryError(
                f"'{self.root}' is not a SolidGit workspace (no {REPO_DIR}/repo.json)."
            )
        self.objects = ObjectStore(self.repo_dir)
        self.commits = CommitStore(self.repo_dir)
        # Lives for as long as the repository is open. The interface rescans about once a
        # second; without this, that means re-reading the whole assembly every second.
        self.stat_cache = StatCache()
        self.info = RepoInfo.from_json(
            json.loads((self.repo_dir / "repo.json").read_text(encoding="utf-8"))
        )
        if self.info.protocol != PROTOCOL_VERSION:
            raise RepositoryError(
                f"Workspace '{self.root}' was created by protocol version "
                f"{self.info.protocol}; this build speaks {PROTOCOL_VERSION}."
            )

    # -- creation -------------------------------------------------------------------------

    @staticmethod
    def initialise(
        root: str | Path,
        name: str | None = None,
        rules: IgnoreRules = DEFAULT_RULES,
        repo_id: str | None = None,
    ) -> Repository:
        """Turn a plain folder into a workspace. Existing files are left untouched.

        `repo_id` is supplied when cloning from a peer: both sides must agree on the project's
        identity, or every later handshake would report two unrelated projects.
        """
        root_path = Path(root).resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        repo_dir = root_path / REPO_DIR
        if (repo_dir / "repo.json").exists():
            raise RepositoryError(f"'{root_path}' is already a SolidGit workspace.")

        repo_dir.mkdir(parents=True, exist_ok=True)
        info = RepoInfo(
            repo_id=repo_id or uuid.uuid4().hex,
            name=name or root_path.name,
            protocol=PROTOCOL_VERSION,
            created_at=utc_now_iso(),
        )
        (repo_dir / "repo.json").write_text(
            json.dumps(info.to_json(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return Repository(root_path, rules)

    @staticmethod
    def is_workspace(root: str | Path) -> bool:
        return (Path(root) / REPO_DIR / "repo.json").exists()

    # -- reading state --------------------------------------------------------------------

    def head_manifest(self) -> Manifest:
        commit = self.commits.head_commit()
        return commit.manifest if commit else Manifest.empty()

    def scan(self, use_cache: bool = True) -> Manifest:
        return scan_workspace(self.root, self.rules, self.stat_cache if use_cache else None)

    def status(self) -> WorkspaceStatus:
        added, modified, removed = changed_paths(self.head_manifest(), self.scan())
        return WorkspaceStatus(
            head=self.commits.head(), added=added, modified=modified, removed=removed
        )

    def latest_hashes(self) -> dict[str, str]:
        """path -> hash as of HEAD. This is what a lock request is checked against."""
        return {path: ref.sha256 for path, ref in self.head_manifest().entries.items()}

    def local_hashes(self) -> dict[str, str | None]:
        """path -> hash currently on disk, the `base` half of a lock request."""
        return {path: ref.sha256 for path, ref in self.scan().entries.items()}

    # -- writing history ------------------------------------------------------------------

    def ingest(self, manifest: Manifest) -> int:
        """Store the content of any manifest entry we do not already have.

        Returns how many blobs were newly written. Unchanged files cost nothing: their
        content is already in the store under the same hash.
        """
        stored = 0
        for path, ref in manifest.entries.items():
            if self.objects.has(ref.sha256):
                continue
            self.objects.put_file(self.root / path)
            stored += 1
        return stored

    def commit(
        self,
        message: str,
        author: Author,
        manifest: Manifest | None = None,
        parents: tuple[str, ...] | None = None,
        allow_empty: bool = False,
    ) -> Commit:
        """Snapshot the workspace (or an explicit manifest) into a new commit."""
        snapshot = manifest if manifest is not None else self.scan()

        if not allow_empty and parents is None:
            added, modified, removed = changed_paths(self.head_manifest(), snapshot)
            if not (added or modified or removed):
                raise RepositoryError("Nothing to commit — the workspace matches HEAD.")

        if parents is None:
            behind = self.commits.behind_tip()
            if behind:
                # Committing on top of a stale HEAD is how a divergence gets created by
                # accident. Refusing here turns a painful reconciliation into a one-click
                # update the person makes before they start.
                raise RepositoryError(
                    f"Takımdan {behind} yeni değişiklik var ve henüz almadın. "
                    "Kaydetmeden önce onları al — yoksa iki ayrı sürüm oluşur."
                )

        self.ingest(snapshot)

        head = self.commits.head()
        commit = build_commit(
            parents=parents if parents is not None else ((head,) if head else ()),
            lamport=self.commits.next_lamport(),
            author=author,
            wall_clock=utc_now_iso(),
            message=message,
            manifest=snapshot,
        )
        self.commits.write(commit)
        self.commits.set_head(commit.id)
        return commit

    # -- materialising files --------------------------------------------------------------

    def checkout_paths(self, manifest: Manifest, paths: Iterable[str]) -> tuple[str, ...]:
        """Write specific files to disk from the store.

        This is the targeted "update just this one part before locking it" path, which is why
        the pull-based model does not force a full 200 MB sync before you can start working.
        """
        written: list[str] = []
        for path in paths:
            ref = manifest.get(path)
            if ref is None:
                raise RepositoryError(f"'{path}' is not in the given manifest.")
            if not self.objects.has(ref.sha256):
                raise RepositoryError(
                    f"Cannot write '{path}': its content ({ref.sha256[:12]}) is not in the "
                    "object store yet."
                )
            self.objects.extract_to(ref.sha256, self.root / path)
            written.append(path)
        return tuple(written)

    def behind(self) -> int:
        """Commits waiting that this workspace has not taken onto disk yet."""
        return self.commits.behind_tip()

    def take_updates(self) -> WorkspaceStatus:
        """Bring the workspace up to the shared front, using content we already hold."""
        tip = self.commits.tip()
        if tip is None or tip == self.commits.head():
            raise RepositoryError("Alınacak yeni değişiklik yok.")
        return self.update_to(tip)

    def update_to(self, commit_id: str) -> WorkspaceStatus:
        """Move to `commit_id`, touching only what actually differs between here and there.

        This is the safe way to take someone else's work. `checkout` compares the *disk* with
        the target, so it rewrites every file that differs — including one you are halfway
        through editing and have not saved yet, silently putting the old version back. Here
        the comparison is HEAD against the target, and any file the update would touch that
        has unsaved changes stops the whole update before a byte is written.

        Files the other side deleted are removed too, but only when they are untouched here;
        leaving them would make them look like new files and invite resurrecting them.
        """
        current = self.head_manifest()
        target = self.commits.read(commit_id).manifest
        added, modified, removed = changed_paths(current, target)

        at_risk = self.unsaved_in_the_way(target)
        if at_risk:
            names = ", ".join(path.rsplit("/", 1)[-1] for path in at_risk[:4])
            more = f" ve {len(at_risk) - 4} dosya daha" if len(at_risk) > 4 else ""
            raise RepositoryError(
                f"Gelen değişiklik kaydedilmemiş işinin üzerine yazacaktı: {names}{more}. "
                "Önce kendi değişikliğini kaydet, sonra tekrar al."
            )

        self.checkout_paths(target, sorted(added | modified))
        for path in sorted(removed):
            file_path = self.root / path
            clear_readonly(file_path)
            file_path.unlink(missing_ok=True)

        self.commits.set_head(commit_id)
        return WorkspaceStatus(head=commit_id, added=added, modified=modified, removed=removed)

    def unsaved_in_the_way(self, target: Manifest) -> list[str]:
        """Files an update to `target` would touch that have unsaved changes on disk.

        Asked before downloading as well as before writing, so nobody waits through a long
        transfer only to be told at the end that it cannot be applied.
        """
        current = self.head_manifest()
        disk = self.scan()
        added, modified, removed = changed_paths(current, target)
        return sorted(
            path for path in (added | modified | removed) if disk.get(path) != current.get(path)
        )

    def checkout(self, commit_id: str, remove_extra: bool = False) -> WorkspaceStatus:
        """Bring the whole workspace to a commit.

        Rewrites every file that differs from the target, unsaved edits included — this is
        the command line's explicit "make the folder look like that commit". Taking a
        teammate's work goes through `update_to` instead.

        `remove_extra` is off by default: deleting a teammate's untracked file because they
        happened to switch versions is not a recoverable mistake.
        """
        target = self.commits.read(commit_id)
        before = self.scan()
        added, modified, removed = changed_paths(before, target.manifest)

        self.checkout_paths(target.manifest, sorted(added | modified))

        if remove_extra:
            for path in sorted(removed):
                file_path = self.root / path
                clear_readonly(file_path)
                file_path.unlink(missing_ok=True)

        self.commits.set_head(commit_id)
        return WorkspaceStatus(
            head=commit_id, added=added, modified=modified, removed=frozenset()
        )

    def missing_digests(self, manifest: Manifest) -> tuple[str, ...]:
        """Blobs this manifest needs that we do not have — the sync shopping list."""
        return tuple(sorted(d for d in manifest.digests() if not self.objects.has(d)))

    # -- read-only enforcement ------------------------------------------------------------

    def apply_readonly(self, writable_paths: Iterable[str]) -> tuple[int, int]:
        """Make every tracked file read-only except the ones we hold a lock on.

        This is what turns a lock from a polite convention into something SolidWorks itself
        enforces: it opens a read-only file read-only, so an unlocked part cannot be edited
        by accident in the first place.

        Returns (made writable, made read-only).
        """
        writable = {p for p in writable_paths}
        made_writable = 0
        made_readonly = 0
        for path in self.scan().paths():
            file_path = self.root / path
            try:
                mode = file_path.stat().st_mode
            except OSError:
                continue
            currently_writable = bool(mode & stat.S_IWRITE)
            if path in writable and not currently_writable:
                file_path.chmod(mode | stat.S_IWRITE)
                made_writable += 1
            elif path not in writable and currently_writable:
                file_path.chmod(mode & ~stat.S_IWRITE)
                made_readonly += 1
        return made_writable, made_readonly

    def make_all_writable(self) -> int:
        """Escape hatch: drop read-only on everything (used when leaving a project)."""
        count = 0
        for path in self.scan().paths():
            file_path = self.root / path
            mode = file_path.stat().st_mode
            if not mode & stat.S_IWRITE:
                file_path.chmod(mode | stat.S_IWRITE)
                count += 1
        return count

    # -- housekeeping ---------------------------------------------------------------------

    def reachable_digests(self) -> frozenset[str]:
        digests: set[str] = set()
        for commit_id in self.commits.iter_ids():
            digests |= self.commits.read(commit_id).manifest.digests()
        return frozenset(digests)

    def garbage_collect(self, dry_run: bool = True) -> tuple[str, ...]:
        """Find (and optionally delete) blobs no commit refers to any more."""
        unreachable = tuple(
            sorted(set(self.objects.iter_digests()) - self.reachable_digests())
        )
        if not dry_run:
            for digest in unreachable:
                self.objects.delete(digest)
        return unreachable

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Repository {self.info.name!r} at {self.root} head={self.commits.head()}>"
