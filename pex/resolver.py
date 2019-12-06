# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import errno
import functools
import json
import os
import subprocess
from collections import OrderedDict, defaultdict, namedtuple
from textwrap import dedent
from uuid import uuid4

from pex.common import safe_mkdtemp
from pex.distribution_target import DistributionTarget
from pex.jobs import SpawnedJob, execute_parallel, spawn_python_job
from pex.orderedset import OrderedSet
from pex.pip import spawn_build_wheels, spawn_download_distributions, spawn_install_wheel
from pex.platforms import Platform
from pex.requirements import local_project_from_requirement, local_projects_from_requirement_file
from pex.third_party.pkg_resources import Distribution, Environment, Requirement
from pex.tracer import TRACER
from pex.util import CacheHelper


class Untranslateable(Exception):
  pass


class Unsatisfiable(Exception):
  pass


class ResolvedDistribution(namedtuple('ResolvedDistribution', ['requirement', 'distribution'])):
  """A requirement and the resolved distribution that satisfies it."""

  def __new__(cls, requirement, distribution):
    assert isinstance(requirement, Requirement)
    assert isinstance(distribution, Distribution)
    return super(ResolvedDistribution, cls).__new__(cls, requirement, distribution)


class DistributionRequirements(object):
  class Request(namedtuple('DistributionRequirementsRequest', ['target', 'distributions'])):
    def spawn_calculation(self):
      search_path = [dist.location for dist in self.distributions]

      program = dedent("""
        import json
        import sys
        from collections import defaultdict
        from pkg_resources import Environment


        env = Environment(search_path={search_path!r})
        dependency_requirements = []
        for key in env:
          for dist in env[key]:
            dependency_requirements.extend(str(req) for req in dist.requires())
        json.dump(dependency_requirements, sys.stdout)
      """.format(search_path=search_path))

      job = spawn_python_job(
        args=['-c', program],
        stdout=subprocess.PIPE,
        interpreter=self.target.get_interpreter(),
        expose=['setuptools']
      )
      return SpawnedJob.stdout(job=job, result_func=self._markers_by_requirement)

    @staticmethod
    def _markers_by_requirement(stdout):
      dependency_requirements = json.loads(stdout.decode('utf-8'))
      markers_by_req_key = defaultdict(OrderedSet)
      for requirement in dependency_requirements:
        req = Requirement.parse(requirement)
        if req.marker:
          markers_by_req_key[req.key].add(req.marker)
      return markers_by_req_key

  @classmethod
  def merged(cls, markers_by_requirement_key_iter):
    markers_by_requirement_key = defaultdict(OrderedSet)
    for distribution_markers in markers_by_requirement_key_iter:
      for requirement, markers in distribution_markers.items():
        markers_by_requirement_key[requirement].update(markers)
    return cls(markers_by_requirement_key)

  def __init__(self, markers_by_requirement_key):
    self._markers_by_requirement_key = markers_by_requirement_key

  def to_requirement(self, dist):
    req = dist.as_requirement()
    markers = self._markers_by_requirement_key.get(req.key)
    if not markers:
      return req

    if len(markers) == 1:
      marker = next(iter(markers))
      req.marker = marker
      return req

    # Here we have a resolve with multiple paths to the dependency represented by dist. At least
    # two of those paths had (different) conditional requirements for dist based on environment
    # marker predicates. Since the pip resolve succeeded, the implication is that the environment
    # markers are compatible; i.e.: their intersection selects the target interpreter. Here we
    # make that intersection explicit.
    # See: https://www.python.org/dev/peps/pep-0496/#micro-language
    marker = ' and '.join('({})'.format(marker) for marker in markers)
    return Requirement.parse('{}; {}'.format(req, marker))


def parsed_platform(platform=None):
  """Parse the given platform into a `Platform` object.

  Unlike `Platform.create`, this function supports the special platform of 'current' or `None`. This
  maps to the platform of any local python interpreter.

  :param platform: The platform string to parse. If `None` or 'current', return `None`. If already a
                   `Platform` object, return it.
  :type platform: str or :class:`Platform`
  :return: The parsed platform or `None` for the current platform.
  :rtype: :class:`Platform` or :class:`NoneType`
  """
  return Platform.create(platform) if platform and platform != 'current' else None


class AtomicDirectory(namedtuple('AtomicDirectory', ['work_dir', 'target_dir'])):
  @classmethod
  def for_target_dir(cls, target_dir):
    return cls(work_dir='{}.{}'.format(target_dir, uuid4().hex), target_dir=target_dir)

  @property
  def is_finalized(self):
    return os.path.exists(self.target_dir)

  def finalize(self):
    if self.is_finalized:
      return

    try:
      # Perform an atomic rename.
      #
      # Per the docs: https://docs.python.org/2.7/library/os.html#os.rename
      #
      #   The operation may fail on some Unix flavors if src and dst are on different filesystems.
      #   If successful, the renaming will be an atomic operation (this is a POSIX requirement).
      #
      # We have satisfied the single filesystem constraint by arranging the `work_dir` to be a
      # sibling of the `target_dir`.
      os.rename(self.work_dir, self.target_dir)
    except OSError as e:
      if e.errno != errno.ENOTEMPTY:
        raise e


class ResolveResult(namedtuple('ResolveResult', ['target', 'download_dir'])):
  @staticmethod
  def _is_wheel(path):
    return os.path.isfile(path) and path.endswith('.whl')

  def _iter_distribution_paths(self):
    if not os.path.exists(self.download_dir):
      return
    for distribution in os.listdir(self.download_dir):
      yield os.path.join(self.download_dir, distribution)

  def build_requests(self):
    for distribution_path in self._iter_distribution_paths():
      if not self._is_wheel(distribution_path):
        yield BuildRequest.create(target=self.target, source_path=distribution_path)

  def install_requests(self):
    for distribution_path in self._iter_distribution_paths():
      if self._is_wheel(distribution_path):
        yield InstallRequest.create(target=self.target, wheel_path=distribution_path)


class BuildRequest(namedtuple('BuildRequest', ['target', 'source_path', 'fingerprint'])):
  @classmethod
  def create(cls, target, source_path):
    hasher = CacheHelper.dir_hash if os.path.isdir(source_path) else CacheHelper.hash
    fingerprint = hasher(source_path)
    return cls(target=target, source_path=source_path, fingerprint=fingerprint)

  def result(self, dist_root):
    return BuildResult.from_request(self, dist_root=dist_root)


class BuildResult(namedtuple('BuildResult', ['request', 'atomic_dir'])):
  @classmethod
  def from_request(cls, build_request, dist_root):
    dist_dir = os.path.join(
      dist_root,
      'sdists' if os.path.isfile(build_request.source_path) else 'local_projects',
      os.path.basename(build_request.source_path),
      build_request.fingerprint,
      build_request.target.id
    )
    return cls(request=build_request, atomic_dir=AtomicDirectory.for_target_dir(dist_dir))

  @property
  def is_built(self):
    return self.atomic_dir.is_finalized

  @property
  def build_dir(self):
    return self.atomic_dir.work_dir

  @property
  def dist_dir(self):
    return self.atomic_dir.target_dir

  def finalize_build(self):
    self.atomic_dir.finalize()
    for wheel in os.listdir(self.dist_dir):
      yield InstallRequest.create(self.request.target, os.path.join(self.dist_dir, wheel))


class InstallRequest(namedtuple('InstallRequest', ['target', 'wheel_path', 'fingerprint'])):
  @classmethod
  def create(cls, target, wheel_path):
    fingerprint = CacheHelper.hash(wheel_path)
    return cls(target=target, wheel_path=wheel_path, fingerprint=fingerprint)

  @property
  def wheel_file(self):
    return os.path.basename(self.wheel_path)

  def result(self, installation_root):
    return InstallResult.from_request(self, installation_root=installation_root)


class InstallResult(namedtuple('InstallResult', ['request', 'atomic_dir'])):
  @classmethod
  def from_request(cls, install_request, installation_root):
    install_chroot = os.path.join(
      installation_root,
      install_request.fingerprint,
      install_request.wheel_file
    )
    return cls(request=install_request, atomic_dir=AtomicDirectory.for_target_dir(install_chroot))

  @property
  def is_installed(self):
    return self.atomic_dir.is_finalized

  @property
  def build_chroot(self):
    return self.atomic_dir.work_dir

  @property
  def install_chroot(self):
    return self.atomic_dir.target_dir

  def finalize_install(self, install_requests):
    self.atomic_dir.finalize()
    return self._iter_requirements_requests(install_requests)

  def _iter_requirements_requests(self, install_requests):
    if self.is_installed:
      # N.B.: Direct snip from the Environment docs:
      #
      #  You may explicitly set `platform` (and/or `python`) to ``None`` if you
      #  wish to map *all* distributions, not just those compatible with the
      #  running platform or Python version.
      #
      # Since our requested target may be foreign, we make sure find all distributions installed by
      # explicitly setting both `python` and `platform` to `None`.
      environment = Environment(search_path=[self.install_chroot], python=None, platform=None)

      distributions = []
      for dist_project_name in environment:
        distributions.extend(environment[dist_project_name])

      for install_request in install_requests:
        yield DistributionRequirements.Request(
          target=install_request.target,
          distributions=distributions
        )


class ResolveRequest(object):
  def __init__(self,
               targets,
               requirements=None,
               requirement_files=None,
               constraint_files=None,
               allow_prereleases=False,
               transitive=True,
               indexes=None,
               find_links=None,
               cache=None,
               build=True,
               use_wheel=True,
               compile=False,
               max_parallel_jobs=None):

    self._targets = targets
    self._requirements = requirements
    self._requirement_files = requirement_files
    self._constraint_files = constraint_files
    self._allow_prereleases = allow_prereleases
    self._transitive = transitive
    self._indexes = indexes
    self._find_links = find_links
    self._cache = cache
    self._build = build
    self._use_wheel = use_wheel
    self._compile = compile
    self._max_parallel_jobs = max_parallel_jobs

  def _iter_local_projects(self):
    if self._requirements:
      for req in self._requirements:
        local_project = local_project_from_requirement(req)
        if local_project:
          for target in self._targets:
            yield BuildRequest.create(target=target, source_path=local_project)

    if self._requirement_files:
      for requirement_file in self._requirement_files:
        for local_project in local_projects_from_requirement_file(requirement_file):
          for target in self._targets:
            yield BuildRequest.create(target=target, source_path=local_project)

  def _run_parallel(self, inputs, spawn_func, raise_type):
    for result in execute_parallel(self._max_parallel_jobs, inputs, spawn_func, raise_type):
      yield result

  def _spawn_resolve(self, resolved_dists_dir, target):
    download_dir = os.path.join(resolved_dists_dir, target.id)
    download_job = spawn_download_distributions(
      download_dir=download_dir,
      requirements=self._requirements,
      requirement_files=self._requirement_files,
      constraint_files=self._constraint_files,
      allow_prereleases=self._allow_prereleases,
      transitive=self._transitive,
      target=target,
      indexes=self._indexes,
      find_links=self._find_links,
      cache=self._cache,
      build=self._build,
      use_wheel=self._use_wheel
    )
    return SpawnedJob.wait(job=download_job, result=ResolveResult(target, download_dir))

  def _categorize_build_requests(self, build_requests, dist_root):
    unsatisfied_build_requests = []
    install_requests = []
    for build_request in build_requests:
      build_result = build_request.result(dist_root)
      if not build_result.is_built:
        TRACER.log('Building {} to {}'.format(build_request.source_path, build_result.dist_dir))
        unsatisfied_build_requests.append(build_request)
      else:
        TRACER.log('Using cached build of {} at {}'
                   .format(build_request.source_path, build_result.dist_dir))
        install_requests.extend(build_result.finalize_build())
    return unsatisfied_build_requests, install_requests

  def _spawn_wheel_build(self, built_wheels_dir, build_request):
    build_result = build_request.result(built_wheels_dir)
    build_job = spawn_build_wheels(
      distributions=[build_request.source_path],
      wheel_dir=build_result.build_dir,
      cache=self._cache,
      interpreter=build_request.target.get_interpreter()
    )
    return SpawnedJob.wait(job=build_job, result=build_result)

  def _categorize_install_requests(self, install_requests, installed_wheels_dir):
    unsatisfied_install_requests = []
    install_results = []
    for install_request in install_requests:
      install_result = install_request.result(installed_wheels_dir)
      if not install_result.is_installed:
        TRACER.log('Installing {} in {}'
                   .format(install_request.wheel_path, install_result.install_chroot))
        unsatisfied_install_requests.append(install_request)
      else:
        TRACER.log('Using cached installation of {} at {}'
                   .format(install_request.wheel_file, install_result.install_chroot))
        install_results.append(install_result)
    return unsatisfied_install_requests, install_results

  def _spawn_install(self, installed_wheels_dir, install_request):
    install_result = install_request.result(installed_wheels_dir)
    install_job = spawn_install_wheel(
      wheel=install_request.wheel_path,
      install_dir=install_result.build_chroot,
      compile=self._compile,
      overwrite=True,
      cache=self._cache,
      target=install_request.target
    )
    return SpawnedJob.wait(job=install_job, result=install_result)

  def resolve_distributions(self):
    # This method has four stages:
    # 1. Resolve sdists and wheels.
    # 2. Build local projects and sdists.
    # 3. Install wheels in individual chroots.
    # 4. Calculate the final resolved requirements.
    #
    # You'd think we might be able to just pip install all the requirements, but pexes can be
    # multi-platform / multi-interpreter, in which case only a subset of distributions resolved into
    # the PEX should be activated for the runtime interpreter. Sometimes there are platform specific
    # wheels and sometimes python version specific dists (backports being the common case). As such,
    # we need to be able to add each resolved distribution to the `sys.path` individually
    # (`PEXEnvironment` handles this selective activation at runtime). Since pip install only
    # accepts a single location to install all resolved dists, that won't work.
    #
    # This means we need to seperately resolve all distributions, then install each in their own
    # chroot. To do this we use `pip download` for the resolve and download of all needed
    # distributions and then `pip install` to install each distribution in its own chroot.
    #
    # As a complicating factor, the runtime activation scheme relies on PEP 425 tags; i.e.: wheel
    # names. Some requirements are only available or applicable in source form - either via sdist,
    # VCS URL or local projects. As such we need to insert a `pip wheel` step to generate wheels for
    # all requirements resolved in source form via `pip download` / inspection of requirements to
    # discover those that are local directories (local setup.py or pyproject.toml python projects).
    #
    # Finally, we must calculate the pinned requirement corresponding to each distribution we
    # resolved along with any environment markers that control which runtime environments the
    # requirement should be activated in.

    if not self._requirements and not self._requirement_files:
      # Nothing to resolve.
      return []

    workspace = safe_mkdtemp()
    cache = self._cache or workspace

    resolved_dists_dir = os.path.join(workspace, 'resolved_dists')
    spawn_resolve = functools.partial(self._spawn_resolve, resolved_dists_dir)
    to_resolve = self._targets

    built_wheels_dir = os.path.join(cache, 'built_wheels')
    spawn_wheel_build = functools.partial(self._spawn_wheel_build, built_wheels_dir)
    to_build = list(self._iter_local_projects())

    installed_wheels_dir = os.path.join(cache, 'installed_wheels')
    spawn_install = functools.partial(self._spawn_install, installed_wheels_dir)
    to_install = []

    to_calculate_requirements_for = []

    # 1. Resolve sdists and wheels.
    with TRACER.timed('Resolving for:\n  '.format('\n  '.join(map(str, to_resolve)))):
      for resolve_result in self._run_parallel(inputs=to_resolve,
                                               spawn_func=spawn_resolve,
                                               raise_type=Unsatisfiable):
        to_build.extend(resolve_result.build_requests())
        to_install.extend(resolve_result.install_requests())

    if not any((to_build, to_install)):
      # Nothing to build or install.
      return []

    # 2. Build local projects and sdists.
    if to_build:
      with TRACER.timed('Building distributions for:\n  {}'
                        .format('\n  '.join(map(str, to_build)))):

        build_requests, install_requests = self._categorize_build_requests(
          build_requests=to_build,
          dist_root=built_wheels_dir
        )
        to_install.extend(install_requests)

        for build_result in self._run_parallel(inputs=build_requests,
                                               spawn_func=spawn_wheel_build,
                                               raise_type=Untranslateable):
          to_install.extend(build_result.finalize_build())

    # 3. Install wheels in individual chroots.

    # Dedup by wheel name; e.g.: only install universal wheels once even though they'll get
    # downloaded / built for each interpreter or platform.
    install_requests_by_wheel_file = OrderedDict()
    for install_request in to_install:
      install_requests = install_requests_by_wheel_file.setdefault(install_request.wheel_file, [])
      install_requests.append(install_request)

    representative_install_requests = [
      requests[0] for requests in install_requests_by_wheel_file.values()
    ]

    def add_requirements_requests(install_result):
      install_requests = install_requests_by_wheel_file[install_result.request.wheel_file]
      to_calculate_requirements_for.extend(install_result.finalize_install(install_requests))

    with TRACER.timed('Installing:\n  {}'
                      .format('\n  '.join(map(str, representative_install_requests)))):

      install_requests, install_results = self._categorize_install_requests(
        install_requests=representative_install_requests,
        installed_wheels_dir=installed_wheels_dir
      )
      for install_result in install_results:
        add_requirements_requests(install_result)

      for install_result in self._run_parallel(inputs=install_requests,
                                               spawn_func=spawn_install,
                                               raise_type=Untranslateable):
        add_requirements_requests(install_result)

    # 4. Calculate the final resolved requirements.
    with TRACER.timed('Calculating resolved requirements for:\n  {}'
                      .format('\n  '.join(map(str, to_calculate_requirements_for)))):
      distribution_requirements = DistributionRequirements.merged(
        self._run_parallel(
          inputs=to_calculate_requirements_for,
          spawn_func=DistributionRequirements.Request.spawn_calculation,
          raise_type=Untranslateable
        )
      )

    resolved_distributions = OrderedSet()
    for requirements_request in to_calculate_requirements_for:
      for distribution in requirements_request.distributions:
        resolved_distributions.add(
          ResolvedDistribution(
            requirement=distribution_requirements.to_requirement(distribution),
            distribution=distribution
          )
        )
    return resolved_distributions


def resolve(requirements=None,
            requirement_files=None,
            constraint_files=None,
            allow_prereleases=False,
            transitive=True,
            interpreter=None,
            platform=None,
            indexes=None,
            find_links=None,
            cache=None,
            build=True,
            use_wheel=True,
            compile=False,
            max_parallel_jobs=None):
  """Produce all distributions needed to meet all specified requirements.

  :keyword requirements: A sequence of requirement strings.
  :type requirements: list of str
  :keyword requirement_files: A sequence of requirement file paths.
  :type requirement_files: list of str
  :keyword constraint_files: A sequence of constraint file paths.
  :type constraint_files: list of str
  :keyword bool allow_prereleases: Whether to include pre-release and development versions when
    resolving requirements. Defaults to ``False``, but any requirements that explicitly request
    prerelease or development versions will override this setting.
  :keyword bool transitive: Whether to resolve transitive dependencies of requirements.
    Defaults to ``True``.
  :keyword interpreter: The interpreter to use for building distributions and for testing
    distribution compatibility. Defaults to the current interpreter.
  :type interpreter: :class:`pex.interpreter.PythonInterpreter`
  :keyword str platform: The exact target platform to resolve distributions for. If ``None`` or
    ``'current'``, resolve for distributions appropriate for `interpreter`.
  :keyword indexes: A list of urls or paths pointing to PEP 503 compliant repositories to search for
    distributions. Defaults to ``None`` which indicates to use the default pypi index. To turn off
    use of all indexes, pass an empty list.
  :type indexes: list of str
  :keyword find_links: A list or URLs, paths to local html files or directory paths. If URLs or
    local html file paths, these are parsed for links to distributions. If a local directory path,
    its listing is used to discover distributons.
  :type find_links: list of str
  :keyword str cache: A directory path to use to cache distributions locally.
  :keyword bool build: Whether to allow building source distributions when no wheel is found.
    Defaults to ``True``.
  :keyword bool use_wheel: Whether to allow resolution of pre-built wheel distributions.
    Defaults to ``True``.
  :keyword bool compile: Whether to pre-compile resolved distribution python sources.
    Defaults to ``False``.
  :keyword int max_parallel_jobs: The maximum number of parallel jobs to use when resolving,
    building and installing distributions in a resolve. Defaults to the number of CPUs available.
  :returns: List of :class:`ResolvedDistribution` instances meeting ``requirements``.
  :raises Unsatisfiable: If ``requirements`` is not transitively satisfiable.
  :raises Untranslateable: If no compatible distributions could be acquired for
    a particular requirement.
  """

  target = DistributionTarget(interpreter=interpreter, platform=parsed_platform(platform))

  resolve_request = ResolveRequest(targets=[target],
                                   requirements=requirements,
                                   requirement_files=requirement_files,
                                   constraint_files=constraint_files,
                                   allow_prereleases=allow_prereleases,
                                   transitive=transitive,
                                   indexes=indexes,
                                   find_links=find_links,
                                   cache=cache,
                                   build=build,
                                   use_wheel=use_wheel,
                                   compile=compile,
                                   max_parallel_jobs=max_parallel_jobs)

  return list(resolve_request.resolve_distributions())


def resolve_multi(requirements=None,
                  requirement_files=None,
                  constraint_files=None,
                  allow_prereleases=False,
                  transitive=True,
                  interpreters=None,
                  platforms=None,
                  indexes=None,
                  find_links=None,
                  cache=None,
                  build=True,
                  use_wheel=True,
                  compile=False,
                  max_parallel_jobs=None):
  """A generator function that produces all distributions needed to meet `requirements`
  for multiple interpreters and/or platforms.

  :keyword requirements: A sequence of requirement strings.
  :type requirements: list of str
  :keyword requirement_files: A sequence of requirement file paths.
  :type requirement_files: list of str
  :keyword constraint_files: A sequence of constraint file paths.
  :type constraint_files: list of str
  :keyword bool allow_prereleases: Whether to include pre-release and development versions when
    resolving requirements. Defaults to ``False``, but any requirements that explicitly request
    prerelease or development versions will override this setting.
  :keyword bool transitive: Whether to resolve transitive dependencies of requirements.
    Defaults to ``True``.
  :keyword interpreters: The interpreters to use for building distributions and for testing
    distribution compatibility. Defaults to the current interpreter.
  :type interpreters: list of :class:`pex.interpreter.PythonInterpreter`
  :keyword platforms: An iterable of PEP425-compatible platform strings to resolve distributions
    for. If ``None`` (the default) or an empty iterable, use the platforms of the given
    interpreters.
  :type platforms: list of str
  :keyword indexes: A list of urls or paths pointing to PEP 503 compliant repositories to search for
    distributions. Defaults to ``None`` which indicates to use the default pypi index. To turn off
    use of all indexes, pass an empty list.
  :type indexes: list of str
  :keyword find_links: A list or URLs, paths to local html files or directory paths. If URLs or
    local html file paths, these are parsed for links to distributions. If a local directory path,
    its listing is used to discover distributons.
  :type find_links: list of str
  :keyword str cache: A directory path to use to cache distributions locally.
  :keyword bool build: Whether to allow building source distributions when no wheel is found.
    Defaults to ``True``.
  :keyword bool use_wheel: Whether to allow resolution of pre-built wheel distributions.
    Defaults to ``True``.
  :keyword bool compile: Whether to pre-compile resolved distribution python sources.
    Defaults to ``False``.
  :keyword int max_parallel_jobs: The maximum number of parallel jobs to use when resolving,
    building and installing distributions in a resolve. Defaults to the number of CPUs available.
  :returns: List of :class:`ResolvedDistribution` instances meeting ``requirements``.
  :raises Unsatisfiable: If ``requirements`` is not transitively satisfiable.
  :raises Untranslateable: If no compatible distributions could be acquired for
    a particular requirement.
  """

  parsed_platforms = [parsed_platform(platform) for platform in platforms] if platforms else []

  def iter_targets():
    if not interpreters and not parsed_platforms:
      # No specified targets, so just build for the current interpreter (on the current platform).
      yield DistributionTarget.current()
      return

    if interpreters:
      for interpreter in interpreters:
        # Build for the specified local interpreters (on the current platform).
        yield DistributionTarget.for_interpreter(interpreter)

    if parsed_platforms:
      for platform in parsed_platforms:
        if platform is not None or not interpreters:
          # 1. Build for specific platforms.
          # 2. Build for the current platform (None) only if not done already (ie: no intepreters
          #    were specified).
          yield DistributionTarget.for_platform(platform)

  resolve_request = ResolveRequest(targets=list(iter_targets()),
                                   requirements=requirements,
                                   requirement_files=requirement_files,
                                   constraint_files=constraint_files,
                                   allow_prereleases=allow_prereleases,
                                   transitive=transitive,
                                   indexes=indexes,
                                   find_links=find_links,
                                   cache=cache,
                                   build=build,
                                   use_wheel=use_wheel,
                                   compile=compile,
                                   max_parallel_jobs=max_parallel_jobs)

  return list(resolve_request.resolve_distributions())
