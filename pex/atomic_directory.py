# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import errno
import fcntl
import os
from contextlib import contextmanager

from pex.common import safe_mkdir, safe_rmtree
from pex.enum import Enum
from pex.typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import Callable, Iterator, Optional


class AtomicDirectory(object):
    def __init__(self, target_dir):
        # type: (str) -> None
        self._target_dir = target_dir
        self._work_dir = "{}.workdir".format(target_dir)

    @property
    def work_dir(self):
        # type: () -> str
        return self._work_dir

    @property
    def target_dir(self):
        # type: () -> str
        return self._target_dir

    def is_finalized(self):
        # type: () -> bool
        return os.path.exists(self._target_dir)

    def finalize(self, source=None):
        # type: (Optional[str]) -> None
        """Rename `work_dir` to `target_dir` using `os.rename()`.

        :param source: An optional source offset into the `work_dir`` to use for the atomic update
                       of `target_dir`. By default the whole `work_dir` is used.

        If a race is lost and `target_dir` already exists, the `target_dir` dir is left unchanged and
        the `work_dir` directory will simply be removed.
        """
        if self.is_finalized():
            return

        source = os.path.join(self._work_dir, source) if source else self._work_dir
        try:
            # Perform an atomic rename.
            #
            # Per the docs: https://docs.python.org/2.7/library/os.html#os.rename
            #
            #   The operation may fail on some Unix flavors if src and dst are on different
            #   filesystems. If successful, the renaming will be an atomic operation (this is a
            #   POSIX requirement).
            #
            # We have satisfied the single filesystem constraint by arranging the `work_dir` to be a
            # sibling of the `target_dir`.
            os.rename(source, self._target_dir)
        except OSError as e:
            if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise e
        finally:
            self.cleanup()

    def cleanup(self):
        # type: () -> None
        safe_rmtree(self._work_dir)


class FileLockStyle(Enum["FileLockStyle.Value"]):
    class Value(Enum.Value):
        pass

    BSD = Value("bsd")
    POSIX = Value("posix")


@contextmanager
def atomic_directory(
    target_dir,  # type: str
    lock_style=FileLockStyle.POSIX,  # type: FileLockStyle.Value
    source=None,  # type: Optional[str]
):
    # type: (...) -> Iterator[AtomicDirectory]
    """A context manager that yields a potentially exclusively locked AtomicDirectory.

    :param target_dir: The target directory to atomically update.
    :param lock_style: By default, a POSIX fcntl lock will be used to ensure exclusivity.
    :param source: An optional source offset into the work directory to use for the atomic update
                   of the target directory. By default, the whole work directory is used.

    If the `target_dir` already exists the enclosed block will be yielded an AtomicDirectory that
    `is_finalized` to signal there is no work to do.

    If the enclosed block fails the `target_dir` will not be created.

    The new work directory will be cleaned up regardless of whether the enclosed block succeeds.
    """
    atomic_dir = AtomicDirectory(target_dir=target_dir)
    if atomic_dir.is_finalized():
        # Our work is already done for us so exit early.
        yield atomic_dir
        return

    head, tail = os.path.split(atomic_dir.target_dir)
    if head:
        safe_mkdir(head)

    # N.B.: We don't actually write anything to the lock file but the fcntl file locking
    # operations only work on files opened for at least write.
    lock_fd = os.open(
        os.path.join(head, ".{}.atomic_directory.lck".format(tail or "here")),
        os.O_CREAT | os.O_WRONLY,
    )

    lock_api = cast(
        "Callable[[int, int], None]",
        fcntl.flock if lock_style is FileLockStyle.BSD else fcntl.lockf,
    )

    def unlock():
        # type: () -> None
        try:
            lock_api(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    # N.B.: Since lockf and flock operate on an open file descriptor and these are
    # guaranteed to be closed by the operating system when the owning process exits,
    # this lock is immune to staleness.
    lock_api(lock_fd, fcntl.LOCK_EX)  # A blocking write lock.
    if atomic_dir.is_finalized():
        # We lost the double-checked locking race and our work was done for us by the race
        # winner so exit early.
        try:
            yield atomic_dir
        finally:
            unlock()
        return

    # If there is an error making the work_dir that means file-locking guarantees have failed
    # somehow and another process has the lock and has made the work_dir already. We let the error
    # from os.mkdir propagate in that case.
    os.mkdir(atomic_dir.work_dir)
    try:
        yield atomic_dir
    except Exception:
        atomic_dir.cleanup()
        raise
    else:
        atomic_dir.finalize(source=source)
    finally:
        unlock()
