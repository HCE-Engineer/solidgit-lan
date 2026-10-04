"""Getting a project from someone else's machine onto yours.

The order matters. Blobs are downloaded and verified first; commits are written next; the
workspace files are replaced last. So an interrupted sync leaves a workspace that is still
exactly what it was — never a half-updated assembly, which is the one outcome SolidWorks
cannot recover from.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from collections.abc import Callable

from ..core.commits import Author, Commit, Relation, build_commit, compare_heads
from ..core.objects import ObjectStoreError
from ..core.reconcile import (
    ReconcileError,
    Side,
    apply_plan,
    conflicting_paths,
    diff_manifests,
    draft_plan,
    validate_plan,
)
from ..core.repo import Repository, utc_now_iso
from .client import Connection, PeerClient, TransferCancelled
from .protocol import ErrorCode, ProtocolError


@dataclass
class SyncPlan:
    """What a sync would do, worked out before a single byte moves."""

    their_head: str | None
    relation: Relation
    new_commits: list[Commit] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)
    """Stored (compressed) sizes — what actually travels."""
    content_sizes: dict[str, int] = field(default_factory=dict)
    """Real file sizes. Progress is reported in these, because "200 MB assembly" is the
    number in the user's head, and a bar counting compressed bytes finishes at 40% of it."""

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())

    @property
    def total_content_bytes(self) -> int:
        return sum(self.content_sizes.values())

    @property
    def is_up_to_date(self) -> bool:
        return self.relation in (Relation.EQUAL, Relation.AHEAD)

    @property
    def can_fast_forward(self) -> bool:
        return self.relation is Relation.BEHIND

    def describe(self) -> str:
        if self.relation is Relation.EQUAL:
            return "Her şey güncel."
        if self.relation is Relation.AHEAD:
            return "Sendeki sürüm daha yeni — alınacak bir şey yok."
        if self.relation is Relation.UNRELATED:
            return "Bu tamamen farklı bir proje; birleştirilemez."
        if self.relation is Relation.DIVERGED:
            return (
                "İkiniz de ayrı ayrı çalışmışsınız. Bunu birleştirmek karşılıklı onay "
                "gerektiriyor (henüz eklenmedi)."
            )
        megabytes = self.total_content_bytes / 1024 / 1024
        return (
            f"{len(self.new_commits)} yeni kayıt, {len(self.missing)} dosya "
            f"({megabytes:.1f} MB) indirilecek."
        )


@dataclass
class SyncProgress:
    objects_total: int = 0
    objects_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current: str = ""
    stage: str = "hazirlik"

    @property
    def fraction(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.bytes_total)


@dataclass
class SyncResult:
    ok: bool
    message: str
    head: str | None = None
    files_written: int = 0
    bytes_transferred: int = 0
    seconds: float = 0.0
    diverged: bool = False
    """The two sides have each done work the other lacks. A flag, not a message to grep:
    the caller decides what to offer next from this, and Turkish text is no contract."""


def plan_pull(
    repo: Repository, client: PeerClient, connection: Connection, token: str
) -> SyncPlan:
    """Work out what we would need, without changing anything."""
    our_head = repo.commits.head()
    their_head, commits, since_known = client.commits_since(connection, token, since=our_head)

    # Reconstructing their id set from a trimmed list is where this goes wrong if you are
    # careless. When they recognised our head, the list omits everything we already have and
    # our history is genuinely a prefix of theirs — so our ids belong in the set. When they
    # did not, they sent everything they have, and folding our ids in would make any
    # divergence look like a plain fast-forward and quietly overwrite our work.
    theirs_known = {c.id for c in commits} | ({their_head} if their_head else set())
    for commit in commits:
        theirs_known.update(commit.parents)
    ours_known = repo.commits.all_ids()
    if since_known:
        theirs_known |= set(ours_known)

    relation = compare_heads(our_head, ours_known, their_head, frozenset(theirs_known))

    plan = SyncPlan(their_head=their_head, relation=relation, new_commits=commits)
    if not plan.can_fast_forward:
        return plan

    wanted: list[str] = []
    content: dict[str, int] = {}
    for commit in commits:
        for ref in commit.manifest.entries.values():
            if ref.sha256 in content or repo.objects.has(ref.sha256):
                continue
            content[ref.sha256] = ref.size
            wanted.append(ref.sha256)

    plan.missing = wanted
    plan.content_sizes = content
    plan.sizes = client.object_sizes(connection, token, wanted)
    return plan


def pull(
    repo: Repository,
    plan: SyncPlan,
    client: PeerClient,
    connection: Connection,
    token: str,
    on_progress: Callable[[SyncProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> SyncResult:
    """Carry out a plan. Safe to call again after an interruption — it resumes."""
    started = time.perf_counter()
    cancel = cancel or threading.Event()

    if plan.relation is Relation.UNRELATED:
        return SyncResult(False, "Bu tamamen farklı bir proje; birleştirilemez.")
    if plan.relation is Relation.DIVERGED:
        return SyncResult(
            False,
            "İkiniz de ayrı ayrı çalışmışsınız. Hiçbir şeyin üzerine yazılmadı — "
            "“Ayrılığı çöz” ile hangi sürümün kalacağına birlikte karar verin.",
            diverged=True,
        )
    if plan.is_up_to_date:
        return SyncResult(True, "Zaten güncel.", head=repo.commits.head())

    # Refuse before downloading anything if the update would land on unsaved work — waiting
    # through a 200 MB transfer to be told that at the end would be its own kind of insult.
    target = next((c for c in plan.new_commits if c.id == plan.their_head), None)
    if target is not None:
        at_risk = repo.unsaved_in_the_way(target.manifest)
        if at_risk:
            names = ", ".join(path.rsplit("/", 1)[-1] for path in at_risk[:4])
            return SyncResult(
                False,
                f"Gelen değişiklik kaydedilmemiş işinin üzerine yazacaktı: {names}. "
                "Önce kendi değişikliğini kaydet, sonra tekrar al.",
            )

    progress = SyncProgress(
        objects_total=len(plan.missing),
        bytes_total=plan.total_content_bytes,
        stage="indirme",
    )

    def report() -> None:
        if on_progress is not None:
            on_progress(progress)

    report()
    transferred = 0
    finished_content = 0
    for digest in plan.missing:
        if cancel.is_set():
            return SyncResult(False, "Durduruldu. İndirilenler korundu, tekrar başlatabilirsin.")

        content_size = plan.content_sizes.get(digest, 0)
        stored_size = plan.sizes.get(digest, 0)

        if repo.objects.has(digest):  # Arrived in an earlier, interrupted attempt.
            finished_content += content_size
            progress.objects_done += 1
            progress.bytes_done = finished_content
            report()
            continue

        progress.current = digest[:12]
        report()

        already = repo.objects.partial_size(digest)
        if stored_size and already > stored_size:
            repo.objects.discard_partial(digest)  # Left over from a different version.
            already = 0

        # Reported in real file bytes: the transfer moves compressed data, but "how much of
        # my 200 MB assembly is here" is the question actually being asked.
        stored_done = already

        # The sizes are bound as defaults on purpose. Closing over the loop variables works
        # today only because the callback runs within this same iteration; bound here, it
        # stays correct even if the download ever becomes deferred.
        def on_chunk(
            count: int,
            base: int = finished_content,
            stored: int = stored_size,
            content: int = content_size,
        ) -> None:
            nonlocal stored_done
            stored_done += count
            share = (stored_done / stored) if stored else 0.0
            progress.bytes_done = base + int(content * min(1.0, share))
            report()

        try:
            client.download_object(
                connection,
                token,
                digest,
                repo.objects.partial_path(digest),
                already_have=already,
                on_chunk=on_chunk,
                cancel=cancel,
            )
            repo.objects.adopt_partial(digest)
        except TransferCancelled:
            return SyncResult(False, "Durduruldu. İndirilenler korundu, tekrar başlatabilirsin.")
        except ObjectStoreError as e:
            # A blob that fails its own hash is either corruption in flight or a peer sending
            # something else; either way it must not reach the workspace.
            return SyncResult(False, f"Bozuk dosya geldi, aktarım durduruldu: {e}")
        except ProtocolError as e:
            return SyncResult(False, e.message)

        transferred += stored_size
        finished_content += content_size
        progress.objects_done += 1
        progress.bytes_done = finished_content
        report()

    progress.stage = "kayit"
    report()
    # Commits are written parent-first so the store is never left referring to a commit it
    # does not have.
    for commit in sorted(plan.new_commits, key=lambda c: c.lamport):
        repo.commits.write(commit)

    if plan.their_head is None or not repo.commits.has(plan.their_head):
        return SyncResult(False, "Karşı tarafın son kaydı eksik geldi; hiçbir şey değiştirilmedi.")

    progress.stage = "dosyalar"
    report()
    try:
        # update_to, not checkout: only what changed between our HEAD and theirs is written,
        # and an unsaved edit in the way stops it instead of being silently replaced.
        status = repo.update_to(plan.their_head)
    except Exception as e:  # noqa: BLE001 - surfaced to the user rather than swallowed
        return SyncResult(False, str(e))

    written = len(status.added | status.modified)
    return SyncResult(
        ok=True,
        message=f"{written} dosya güncellendi.",
        head=plan.their_head,
        files_written=written,
        bytes_transferred=transferred,
        seconds=time.perf_counter() - started,
    )


@dataclass
class PushPlan:
    """What sending our work would involve."""

    our_head: str | None
    their_tip: str | None
    commits: list[Commit] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())

    @property
    def has_work(self) -> bool:
        return bool(self.commits)

    def describe(self) -> str:
        if not self.commits:
            return "Gönderilecek yeni kayıt yok."
        megabytes = self.total_bytes / 1024 / 1024
        return (
            f"{len(self.commits)} kayıt, {len(self.missing)} dosya "
            f"({megabytes:.1f} MB) gönderilecek."
        )


def plan_push(
    repo: Repository, client: PeerClient, connection: Connection, token: str
) -> PushPlan:
    """Work out which of our commits the host lacks, and which files they need with them."""
    our_head = repo.commits.head()
    their_tip, _, since_known = client.commits_since(connection, token, since=our_head)

    if our_head is None:
        return PushPlan(our_head, their_tip)
    if since_known:
        # They already have our head, so there is nothing of ours to send.
        return PushPlan(our_head, their_tip)

    known_by_them = repo.commits.ancestors(their_tip) if their_tip and repo.commits.has(their_tip) else frozenset()
    ours = repo.commits.ancestors(our_head) - known_by_them
    commits = sorted((repo.commits.read(cid) for cid in ours), key=lambda c: c.lamport)

    digests: list[str] = []
    sizes: dict[str, int] = {}
    for commit in commits:
        for ref in commit.manifest.entries.values():
            if ref.sha256 not in sizes:
                sizes[ref.sha256] = ref.size
                digests.append(ref.sha256)

    plan = PushPlan(our_head, their_tip, commits)
    plan.missing = client.missing_on_host(connection, token, digests)
    plan.sizes = {d: sizes[d] for d in plan.missing}
    return plan


def push(
    repo: Repository,
    plan: PushPlan,
    client: PeerClient,
    connection: Connection,
    token: str,
    on_progress: Callable[[SyncProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> SyncResult:
    """Send our commits to the host. Content first, commits last.

    A commit is only accepted once every file it names is already on the other side, so the
    host can never end up with history pointing at content it does not have.
    """
    started = time.perf_counter()
    cancel = cancel or threading.Event()

    if not plan.has_work:
        return SyncResult(True, "Gönderilecek yeni kayıt yok.", head=repo.commits.head())

    progress = SyncProgress(
        objects_total=len(plan.missing), bytes_total=plan.total_bytes, stage="gonderme"
    )

    def report() -> None:
        if on_progress is not None:
            on_progress(progress)

    report()
    sent = 0
    for digest in plan.missing:
        if cancel.is_set():
            return SyncResult(False, "Durduruldu.")
        progress.current = digest[:12]
        report()
        try:
            client.upload_object(
                connection,
                token,
                digest,
                repo.objects.path_for(digest),
                on_chunk=lambda _: None,
                cancel=cancel,
            )
        except TransferCancelled:
            return SyncResult(False, "Durduruldu.")
        except ProtocolError as e:
            return SyncResult(False, e.message)

        sent += plan.sizes.get(digest, 0)
        progress.objects_done += 1
        progress.bytes_done = sent
        report()

    progress.stage = "kayit"
    report()
    for commit in plan.commits:
        try:
            client.push_commit(connection, token, commit)
        except ProtocolError as e:
            if e.code is ErrorCode.DIVERGED:
                return SyncResult(
                    False,
                    "Karşı tarafta senden sonra yeni değişiklik olmuş. Önce onları al, "
                    "sonra tekrar gönder.",
                    diverged=True,
                )
            return SyncResult(False, e.message)

    return SyncResult(
        ok=True,
        message=f"{len(plan.commits)} kayıt gönderildi.",
        head=repo.commits.head(),
        bytes_transferred=sent,
        seconds=time.perf_counter() - started,
    )


@dataclass
class Divergence:
    """Everything needed to show two people what they disagree about."""

    our_head: str
    their_tip: str
    base: str | None
    diffs: tuple = ()
    conflicts: tuple[str, ...] = ()

    @property
    def needs_decisions(self) -> bool:
        return bool(self.conflicts)


def inspect_divergence(
    repo: Repository, client: PeerClient, connection: Connection, token: str
) -> Divergence | None:
    """Pull down their history (not their files) and work out what actually differs.

    Their commits are stored locally so the comparison has a merge base to work from. That is
    what separates "they added this part" from "I deleted it" — identical in the manifests,
    opposite in what they call for.
    """
    our_head = repo.commits.head()
    their_tip, commits, since_known = client.commits_since(connection, token, since=our_head)
    if our_head is None or their_tip is None or since_known:
        return None

    for commit in sorted(commits, key=lambda c: c.lamport):
        repo.commits.write(commit)
    if not repo.commits.has(their_tip):
        return None

    base_id = repo.commits.merge_base(our_head, their_tip)
    ours = repo.commits.read(our_head).manifest
    theirs = repo.commits.read(their_tip).manifest
    base = repo.commits.read(base_id).manifest if base_id else None

    diffs = diff_manifests(ours, theirs, base)
    return Divergence(
        our_head=our_head,
        their_tip=their_tip,
        base=base_id,
        diffs=diffs,
        conflicts=conflicting_paths(diffs),
    )


def rebase_onto_theirs(
    repo: Repository,
    divergence: Divergence,
    client: PeerClient,
    connection: Connection,
    token: str,
    author: Author,
    on_progress: Callable[[SyncProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> SyncResult:
    """Two people changed *different* files at the same time — there is nothing to decide.

    This is the everyday case locks make possible: Ahmet holds the body, Hüseyin holds the
    cover, both save. Without this, whoever sent second was sent to the reconciliation
    dialog and had to wait for the host's approval over a "conflict" with nothing in it.

    Our work is replayed on top of theirs as one new commit, so the shared history stays a
    single line and the host sees an ordinary fast-forward. Only genuinely contested files
    (both sides touched the same one) still go to a person.
    """
    cancel = cancel or threading.Event()
    if divergence.needs_decisions:
        return SyncResult(
            False,
            "İkiniz de aynı dosyayı değiştirmişsiniz; hangisinin kalacağına birlikte karar "
            "verin (“Ayrılığı çöz”).",
            diverged=True,
        )

    plan = draft_plan(divergence.diffs, divergence.our_head, divergence.their_tip)
    merged = apply_plan(plan, divergence.diffs)

    at_risk = repo.unsaved_in_the_way(merged)
    if at_risk:
        names = ", ".join(path.rsplit("/", 1)[-1] for path in at_risk[:4])
        return SyncResult(False, f"Önce kaydedilmemiş değişikliklerini kaydet: {names}.")

    needed = [digest for digest in sorted(merged.digests()) if not repo.objects.has(digest)]
    progress = SyncProgress(objects_total=len(needed), stage="indirme")
    if on_progress is not None:
        on_progress(progress)
    for digest in needed:
        try:
            client.download_object(
                connection, token, digest, repo.objects.partial_path(digest), cancel=cancel
            )
            repo.objects.adopt_partial(digest)
        except TransferCancelled:
            return SyncResult(False, "Durduruldu.")
        except (ProtocolError, ObjectStoreError) as e:
            return SyncResult(False, f"Dosya alınamadı: {e}")
        progress.objects_done += 1
        if on_progress is not None:
            on_progress(progress)

    ours = repo.commits.ancestors(divergence.our_head) - repo.commits.ancestors(
        divergence.their_tip
    )
    own = sorted((repo.commits.read(cid) for cid in ours), key=lambda c: c.lamport)
    rebased = build_commit(
        parents=(divergence.their_tip,),
        lamport=max(
            repo.commits.read(divergence.our_head).lamport,
            repo.commits.read(divergence.their_tip).lamport,
        )
        + 1,
        author=author,
        wall_clock=utc_now_iso(),
        message=" · ".join(c.message for c in own) or "değişiklikler",
        manifest=merged,
    )
    repo.commits.write(rebased)

    progress.stage = "dosyalar"
    if on_progress is not None:
        on_progress(progress)
    status = repo.update_to(rebased.id)
    taken = len(status.added | status.modified | status.removed)
    return SyncResult(
        True,
        f"Arada gelen {taken} dosya alındı; senin değişikliklerin onların üzerine eklendi.",
        head=rebased.id,
        files_written=taken,
    )


def reconcile(
    repo: Repository,
    divergence: Divergence,
    choices: dict[str, Side],
    client: PeerClient,
    connection: Connection,
    token: str,
    author: Author,
    on_progress: Callable[[SyncProgress], None] | None = None,
    wait_seconds: float = 120.0,
    cancel: threading.Event | None = None,
) -> SyncResult:
    """Merge two diverged histories, with both people agreeing to the same plan.

    Nothing is written anywhere until the other side approves the exact set of decisions —
    not a summary of them, the decisions themselves.
    """
    started = time.perf_counter()
    cancel = cancel or threading.Event()

    plan = draft_plan(
        divergence.diffs, divergence.our_head, divergence.their_tip, choices=choices
    )
    try:
        validate_plan(plan, divergence.diffs)
        merged = apply_plan(plan, divergence.diffs)
    except ReconcileError as e:
        return SyncResult(False, str(e))

    # Checked before asking anyone for approval: an agreed merge that then cannot be put on
    # disk because of an unsaved edit would leave the two machines disagreeing.
    at_risk = repo.unsaved_in_the_way(merged)
    if at_risk:
        names = ", ".join(path.rsplit("/", 1)[-1] for path in at_risk[:4])
        return SyncResult(False, f"Önce kaydedilmemiş değişikliklerini kaydet: {names}.")

    ours = repo.commits.ancestors(divergence.our_head) - repo.commits.ancestors(
        divergence.their_tip
    )
    our_commits = [repo.commits.read(cid) for cid in ours]

    progress = SyncProgress(stage="onay")
    if on_progress is not None:
        on_progress(progress)

    try:
        plan_id = client.propose_reconcile(connection, token, plan, our_commits)
    except ProtocolError as e:
        return SyncResult(False, e.message)

    deadline = time.monotonic() + wait_seconds
    status = "pending"
    while time.monotonic() < deadline and not cancel.is_set():
        try:
            status = client.reconcile_status(connection, token, plan_id)
        except ProtocolError as e:
            return SyncResult(False, e.message)
        if status != "pending":
            break
        time.sleep(1.0)

    if cancel.is_set():
        return SyncResult(False, "Durduruldu.")
    if status == "rejected":
        return SyncResult(False, "Karşı taraf birleştirmeyi onaylamadı. Hiçbir şey değişmedi.")
    if status != "approved":
        return SyncResult(False, "Onay gelmedi. Hiçbir şey değişmedi.")

    # Both sides agreed. Content first — theirs down to us, ours up to them — and only then
    # the commit that names it.
    progress.stage = "indirme"
    needed = [d for d in merged.digests() if not repo.objects.has(d)]
    progress.objects_total = len(needed)
    sizes = client.object_sizes(connection, token, needed)
    progress.bytes_total = sum(sizes.values())
    if on_progress is not None:
        on_progress(progress)

    for digest in needed:
        try:
            client.download_object(
                connection, token, digest, repo.objects.partial_path(digest), cancel=cancel
            )
            repo.objects.adopt_partial(digest)
        except TransferCancelled:
            return SyncResult(False, "Durduruldu.")
        except (ProtocolError, ObjectStoreError) as e:
            return SyncResult(False, f"Dosya alınamadı: {e}")
        progress.objects_done += 1
        progress.bytes_done += sizes.get(digest, 0)
        if on_progress is not None:
            on_progress(progress)

    progress.stage = "gonderme"
    if on_progress is not None:
        on_progress(progress)
    for digest in client.missing_on_host(connection, token, sorted(merged.digests())):
        try:
            client.upload_object(
                connection, token, digest, repo.objects.path_for(digest), cancel=cancel
            )
        except TransferCancelled:
            return SyncResult(False, "Durduruldu.")
        except ProtocolError as e:
            return SyncResult(False, e.message)

    merge_commit = build_commit(
        parents=(divergence.our_head, divergence.their_tip),
        lamport=max(
            repo.commits.read(divergence.our_head).lamport,
            repo.commits.read(divergence.their_tip).lamport,
        )
        + 1,
        author=author,
        wall_clock=utc_now_iso(),
        message=f"Birleştirme ({len(plan.resolution)} dosya karara bağlandı)",
        manifest=merged,
    )
    repo.commits.write(merge_commit)

    try:
        client.push_commit(connection, token, merge_commit, plan_id=plan_id)
    except ProtocolError as e:
        return SyncResult(False, e.message)

    progress.stage = "dosyalar"
    if on_progress is not None:
        on_progress(progress)
    repo.update_to(merge_commit.id)

    return SyncResult(
        ok=True,
        message=f"Birleştirildi — {len(plan.resolution)} dosya karara bağlandı.",
        head=merge_commit.id,
        seconds=time.perf_counter() - started,
    )


def clone(
    root,
    repo_id: str,
    name: str,
    client: PeerClient,
    connection: Connection,
    token: str,
    on_progress: Callable[[SyncProgress], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Repository | None, SyncResult]:
    """Create a workspace from someone else's session and fill it.

    The repo id comes from them: both sides have to agree on the project's identity, or every
    later handshake would decide these were two unrelated projects.
    """
    try:
        repo = Repository.initialise(root, name=name, repo_id=repo_id)
    except Exception as e:  # noqa: BLE001
        return None, SyncResult(False, f"Klasör hazırlanamadı: {e}")

    plan = plan_pull(repo, client, connection, token)
    if plan.their_head is None:
        return repo, SyncResult(True, "Karşı tarafta henüz kayıt yok — boş proje oluşturuldu.")
    if not plan.can_fast_forward:
        return repo, SyncResult(False, plan.describe())

    return repo, pull(repo, plan, client, connection, token, on_progress, cancel)


def check_version(their_version: str, ours: str) -> str:
    """A short warning when a peer runs a different build, or empty when they match."""
    if not their_version or their_version == ours:
        return ""
    return (
        f"Karşı tarafta sürüm {their_version} var, sende {ours}. "
        "Farklı sürümler birbirine bağlanmayı reddedebilir — aynı sürümü kullanın."
    )
