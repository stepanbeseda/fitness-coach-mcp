"""Online backup and restore of the gateway database (``python -m gateway.cli backup-db | restore-db``).

The gateway keeps running while a backup is taken. Its database is in WAL mode, so copying
the file alone would miss committed rows that still sit in the ``-wal`` file; SQLite's online
backup API copies a consistent snapshot including them. The snapshot is held in memory (a temp
file would land in the data volume or in the small ``/tmp`` tmpfs), capped by
``MAX_SNAPSHOT_BYTES``. At most two copies of the database exist at once (the ``:memory:``
copy and the ``serialize()`` bytes); validation runs on the ``:memory:`` copy before
``serialize()``, and the header fix happens while writing. The same code lives in
``coach_mcp/backup.py`` on purpose: separate images, no shared package.

Nothing here logs and nothing needs the gateway settings or secrets: the commands print one
line on stderr, and messages carry names, sizes and versions, never row data. Stdlib only.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TypeVar

T = TypeVar("T")

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
BUSY_DEADLINE_SECONDS = 60.0
BUSY_TIMEOUT_SECONDS = 5.0

_CHUNK = 1024 * 1024
_SIDECARS = ("-journal", "-wal", "-shm")


class SnapshotError(Exception):
    """A backup or restore problem; the message is safe to print (no row data)."""


def _cap_text() -> str:
    cap = MAX_SNAPSHOT_BYTES
    return f"{cap // 2**20} MiB" if cap % 2**20 == 0 else f"{cap} bytes"


def snapshot(
    path: Path,
    check: Callable[[sqlite3.Connection], T],
    *,
    deadline_s: float = BUSY_DEADLINE_SECONDS,
    busy_timeout_s: float = BUSY_TIMEOUT_SECONDS,
    pages: int = -1,
    progress: Callable[[int, int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bytes, T]:
    """Consistent copy of the SQLite database at ``path`` as bytes, plus what ``check`` returned.

    Raises ``SnapshotError`` (too big, locked for too long, integrity or ``check`` failed) or a
    ``sqlite3.Error`` (missing file, not a database).
    """
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        # mode=rw never creates a missing file. No Store (it would migrate), no pragma.
        src = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=busy_timeout_s)
        try:
            size = src.execute("PRAGMA page_count").fetchone()[0] * src.execute("PRAGMA page_size").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):  # an exclusive writer outlasted the busy timeout
                raise SnapshotError(f"database stayed locked for {busy_timeout_s:g} s") from exc
            raise
        if size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError(f"database is larger than {_cap_text()} ({size} bytes)")
        dst = sqlite3.connect(":memory:")
        start = clock()

        def _progress(status: int, remaining: int, total: int) -> None:
            if progress is not None:
                progress(status, remaining, total)
            # backup() loops forever while the source stays BUSY/LOCKED; raising here ends it.
            if clock() - start > deadline_s:
                raise SnapshotError(f"database stayed locked for {deadline_s:g} s")

        src.backup(dst, pages=pages, progress=_progress)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SnapshotError("integrity check failed")
        info = check(dst)
        data = dst.serialize()
        dst.close()
        dst = None
        return data, info
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def write_normalised(data: bytes, out: BinaryIO) -> None:
    """Write ``data``; a WAL header (bytes 18-19 = 2) is rewritten to rollback-journal mode (1).

    A WAL-flagged file makes SQLite create ``-wal``/``-shm`` siblings even on a read-only open;
    with the flag cleared the archived file is self-contained. The service that owns the
    database switches to WAL again on its next open.
    """
    view = memoryview(data)
    if len(data) >= 20 and (data[18] == 2 or data[19] == 2):
        out.write(view[:18])
        out.write(b"\x01\x01")
        out.write(view[20:])
    else:
        out.write(data)


def check_gateway(conn: sqlite3.Connection) -> None:
    """Validate that ``conn`` holds a gateway database (the schema is ``CREATE TABLE IF NOT EXISTS``, no versions)."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"grants", "tokens"} <= names:
        raise SnapshotError("není databáze gateway (chybí tabulka grants/tokens)")


def backup_command(db_path: Path) -> int:
    """Write a consistent copy of the database to stdout. Exit codes: 0 ok, 1 failed, 2 refused."""
    if sys.stdout.isatty():
        print("nepíšu databázi na terminál; přesměruj stdout", file=sys.stderr)
        return 2
    if not db_path.is_file():
        print(f"záloha selhala: žádná databáze v {db_path}", file=sys.stderr)
        return 1
    try:
        data, _ = snapshot(db_path, check_gateway)
        write_normalised(data, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    except (SnapshotError, sqlite3.Error, OSError) as exc:
        print(f"záloha selhala: {exc}", file=sys.stderr)
        return 1
    return 0


def _remove(path: Path) -> None:
    """Remove a temp file and its SQLite sidecars."""
    for suffix in ("", *_SIDECARS):
        with contextlib.suppress(FileNotFoundError):
            Path(f"{path}{suffix}").unlink()


def _write_new(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_stdin(dest: Path) -> None:
    total = 0
    with _write_new(dest) as out:
        while chunk := sys.stdin.buffer.read(_CHUNK):
            total += len(chunk)
            if total > MAX_SNAPSHOT_BYTES:
                raise SnapshotError(f"vstup je větší než {_cap_text()}")
            out.write(chunk)
    if total == 0:
        raise SnapshotError("na stdin nic nepřišlo")


def restore_command(
    db_path: Path, source: str = "-", *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> int:
    """Replace the database at ``db_path`` with a validated backup (the service must be stopped).

    ``source`` is ``-`` (stdin) or a file. The previous database and its journal/WAL files stay
    next to it as ``<name>.pred-obnovou-<UTC time>[-journal|-wal|-shm]`` (SQLite pairs a hot
    journal or WAL with its database by name, so the side copy is restorable too). Exit codes:
    0 ok, 1 failed (``nic se nezměnilo`` unless the message says otherwise), 2 refused.
    """
    name = db_path.name
    stage_in = db_path.with_name(f"{name}.restore-in")
    stage_new = db_path.with_name(f"{name}.restore-new")
    try:
        return _restore(db_path, source, now, stage_in, stage_new)
    finally:
        _remove(stage_in)
        _remove(stage_new)


def _restore(db_path: Path, source: str, now: Callable[[], datetime], stage_in: Path, stage_new: Path) -> int:
    name = db_path.name
    parent = db_path.parent
    try:
        _remove(stage_in)
        _remove(stage_new)
        if source == "-":
            if sys.stdin.isatty():
                print("nečtu databázi z terminálu; pošli soubor rourou", file=sys.stderr)
                return 2
            _copy_stdin(stage_in)
            src_path = stage_in
        else:
            src_path = Path(source)
            if not src_path.is_file():
                raise SnapshotError(f"zdrojový soubor neexistuje: {source}")
            if src_path.resolve() == db_path.resolve():
                raise SnapshotError("zdroj je nahrazovaná databáze")
        data, _ = snapshot(src_path, check_gateway)
        with _write_new(stage_new) as out:
            write_normalised(data, out)
            out.flush()
            os.fsync(out.fileno())
        del data
        aside = f"{name}.pred-obnovou-{now().strftime('%Y%m%dT%H%M%SZ')}"
        if any((parent / f"{aside}{s}").exists() for s in ("", *_SIDECARS)):
            raise SnapshotError("předchozí soubory z této sekundy už existují; zkus to za sekundu")
        existing = [s for s in ("", *_SIDECARS) if (parent / f"{name}{s}").exists()]
    except (SnapshotError, sqlite3.Error, OSError) as exc:
        print(f"obnova selhala: {exc}; nic se nezměnilo", file=sys.stderr)
        return 1

    moved = False
    try:
        for suffix in existing:
            os.rename(parent / f"{name}{suffix}", parent / f"{aside}{suffix}")
            moved = True
        os.replace(stage_new, db_path)
        _fsync_dir(parent)
    except OSError as exc:
        where = f"předchozí soubory jsou v {parent} jako {aside}*" if moved else "nic se nezměnilo"
        print(f"obnova selhala: {exc}; {where}", file=sys.stderr)
        return 1

    kept = f"předchozí soubory jsou jako {aside}" if moved else "předchozí databáze neexistovala"
    print(f"obnoveno {name}; {kept}", file=sys.stderr)
    return 0
