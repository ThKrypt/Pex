# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import os

from pex.compatibility import urlparse
from pex.distribution_target import DistributionTarget
from pex.resolve.locked_resolve import LockedResolve
from pex.resolve.resolver_configuration import ResolverVersion
from pex.sorted_tuple import SortedTuple
from pex.third_party.packaging import tags
from pex.third_party.pkg_resources import Requirement, RequirementParseError
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    import attr  # vendor:skip
    from typing import (
        Iterable,
        Iterator,
        List,
        Mapping,
        Optional,
        Tuple,
    )
else:
    from pex.third_party import attr


@attr.s(frozen=True)
class _RankedLock(object):
    @classmethod
    def rank(
        cls,
        locked_resolve,  # type: LockedResolve
        supported_tags,  # type: Mapping[tags.Tag, int]
    ):
        # type: (...) -> Optional[_RankedLock]

        resolve_rank = None  # type: Optional[int]
        for req in locked_resolve.locked_requirements:
            requirement_rank = None  # type: Optional[int]
            for artifact in req.iter_artifacts():
                url_info = urlparse.urlparse(artifact.url)
                artifact_file = os.path.basename(url_info.path)
                if artifact_file.endswith(
                    (".sdist", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".zip")
                ):
                    # N.B.: This is greater (worse) than any wheel rank can be by 1, ensuring sdists
                    # are picked last amongst a set of artifacts. We do this, since a wheel is known
                    # to work with a target by the platform tags on the tin, whereas an sdist may
                    # not successfully build for a given target at all. This is an affordance for
                    # LockStyle.SOURCES and LockStyle.CROSS_PLATFORM lock styles.
                    sdist_rank = len(supported_tags)
                    requirement_rank = (
                        sdist_rank
                        if requirement_rank is None
                        else min(sdist_rank, requirement_rank)
                    )
                elif artifact_file.endswith(".whl"):
                    artifact_stem, _ = os.path.splitext(artifact_file)
                    for tag in tags.parse_tag(artifact_stem.split("-", 2)[-1]):
                        wheel_rank = supported_tags.get(tag)
                        if wheel_rank:
                            requirement_rank = (
                                wheel_rank
                                if requirement_rank is None
                                else min(wheel_rank, requirement_rank)
                            )

            if requirement_rank is None:
                return None

            resolve_rank = (
                requirement_rank if resolve_rank is None else resolve_rank + requirement_rank
            )

        if resolve_rank is None:
            return None

        average_requirement_rank = float(resolve_rank) / len(locked_resolve.locked_requirements)
        return cls(average_requirement_rank=average_requirement_rank, locked_resolve=locked_resolve)

    average_requirement_rank = attr.ib()  # type: float
    locked_resolve = attr.ib()  # type: LockedResolve


@attr.s(frozen=True)
class Lockfile(object):
    @classmethod
    def create(
        cls,
        pex_version,  # type: str
        resolver_version,  # type: ResolverVersion.Value
        requirements,  # type: Iterable[Requirement]
        constraints,  # type: Iterable[Requirement]
        allow_prereleases,  # type: bool
        allow_wheels,  # type: bool
        allow_builds,  # type: bool
        transitive,  # type: bool
        locked_resolves,  # type: Iterable[LockedResolve]
    ):
        # type: (...) -> Lockfile
        return cls(
            pex_version=pex_version,
            resolver_version=resolver_version,
            requirements=SortedTuple(requirements, key=str),
            constraints=SortedTuple(constraints, key=str),
            allow_prereleases=allow_prereleases,
            allow_wheels=allow_wheels,
            allow_builds=allow_builds,
            transitive=transitive,
            locked_resolves=SortedTuple(locked_resolves),
        )

    pex_version = attr.ib()  # type: str
    resolver_version = attr.ib()  # type: ResolverVersion.Value
    requirements = attr.ib()  # type: SortedTuple[Requirement]
    constraints = attr.ib()  # type: SortedTuple[Requirement]
    allow_prereleases = attr.ib()  # type: bool
    allow_wheels = attr.ib()  # type: bool
    allow_builds = attr.ib()  # type: bool
    transitive = attr.ib()  # type: bool
    locked_resolves = attr.ib()  # type: SortedTuple[LockedResolve]

    def select(self, targets):
        # type: (Iterable[DistributionTarget]) -> Iterator[Tuple[DistributionTarget, LockedResolve]]
        """Finds the most appropriate lock, if any, for each of the given targets.

        :param targets: The targets to select locked resolves for.
        :return: The selected locks.
        """
        for target in targets:
            lock = self._select(target)
            if lock:
                yield target, lock

    def _select(self, target):
        # type: (DistributionTarget) -> Optional[LockedResolve]
        ranked_locks = []  # type: List[_RankedLock]

        supported_tags = {tag: index for index, tag in enumerate(target.get_supported_tags())}
        for locked_resolve in self.locked_resolves:
            ranked_lock = _RankedLock.rank(locked_resolve, supported_tags)
            if ranked_lock is not None:
                ranked_locks.append(ranked_lock)

        if not ranked_locks:
            return None

        ranked_lock = sorted(ranked_locks)[0]
        count = len(supported_tags)
        TRACER.log(
            "Selected lock generated by {platform} with an average requirement rank of "
            "{average_requirement_rank:.2f} (out of {count}, so ~{percent:.1%} platform specific) "
            "from locks generated by {platforms}".format(
                platform=ranked_lock.locked_resolve.platform_tag,
                average_requirement_rank=ranked_lock.average_requirement_rank,
                count=count,
                percent=(count - ranked_lock.average_requirement_rank) / count,
                platforms=", ".join(
                    sorted(str(lock.platform_tag) for lock in self.locked_resolves)
                ),
            )
        )
        return ranked_lock.locked_resolve
