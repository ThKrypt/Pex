# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
from contextlib import contextmanager

from twitter.common.contextutil import temporary_dir, temporary_file

from pex.compatibility import nested
from pex.environment import PEXEnvironment
from pex.pex_builder import PEXBuilder
from pex.pex_info import PexInfo
from pex.testing import make_bdist


@contextmanager
def yield_pex_builder(zip_safe=True):
  with nested(temporary_dir(), make_bdist('p1', zipped=True, zip_safe=zip_safe)) as (td, p1):
    pb = PEXBuilder(path=td)
    pb.add_egg(p1.location)
    yield pb


def test_force_local():
  with nested(yield_pex_builder(), temporary_dir(), temporary_file()) as (pb, pex_root, pex_file):
    pex = pb.path()
    pb.info.pex_root = pex_root
    pb.build(pex_file.name)

    code_cache = PEXEnvironment.force_local(pex_file.name, pb.info)
    assert os.path.exists(pb.info.zip_unsafe_cache)
    assert len(os.listdir(pb.info.zip_unsafe_cache)) == 1
    assert [os.path.basename(code_cache)] == os.listdir(pb.info.zip_unsafe_cache)
    assert set(os.listdir(code_cache)) == set([PexInfo.PATH, '__main__.py'])

    # idempotence
    assert PEXEnvironment.force_local(pex_file.name, pb.info) == code_cache


def normalize(path):
  return os.path.normpath(os.path.realpath(path))


def test_write_zipped_internal_cache():
  # zip_safe pex will not be written to install cache unless always_write_cache
  with nested(yield_pex_builder(zip_safe=True), temporary_dir(), temporary_file()) as (
      pb, pex_root, pex_file):

    pex = pb.path()
    pb.info.pex_root = pex_root
    pb.build(pex_file.name)

    dists = PEXEnvironment.write_zipped_internal_cache(pex_file.name, pb.info)
    assert len(dists) == 1
    assert normalize(dists[0].location).startswith(
        normalize(os.path.join(pex_file.name, pb.info.internal_cache))), (
        'loc: %s, cache: %s' % (
            normalize(dists[0].location),
            normalize(os.path.join(pex_file.name, pb.info.internal_cache))))

    pb.info.always_write_cache = True
    dists = PEXEnvironment.write_zipped_internal_cache(pex_file.name, pb.info)
    assert len(dists) == 1
    assert normalize(dists[0].location).startswith(normalize(pb.info.install_cache))

  # zip_safe pex will not be written to install cache unless always_write_cache
  with nested(yield_pex_builder(zip_safe=False), temporary_dir(), temporary_file()) as (
      pb, pex_root, pex_file):

    pex = pb.path()
    pb.info.pex_root = pex_root
    pb.build(pex_file.name)

    dists = PEXEnvironment.write_zipped_internal_cache(pex_file.name, pb.info)
    assert len(dists) == 1
    assert normalize(dists[0].location).startswith(normalize(pb.info.install_cache))
    original_location = normalize(dists[0].location)

    # do the second time to validate idempotence of caching
    dists = PEXEnvironment.write_zipped_internal_cache(pex_file.name, pb.info)
    assert len(dists) == 1
    assert normalize(dists[0].location) == original_location


def test_load_internal_cache_unzipped():
  # zip_safe pex will not be written to install cache unless always_write_cache
  with nested(yield_pex_builder(zip_safe=True), temporary_dir()) as (pb, pex_root):
    pex = pb.path()
    pb.info.pex_root = pex_root
    pb.freeze()

    dists = list(PEXEnvironment.load_internal_cache(pb.path(), pb.info))
    assert len(dists) == 1
    assert normalize(dists[0].location).startswith(
        normalize(os.path.join(pb.path(), pb.info.internal_cache)))

def test_access_zipped_assets():
  with nested(yield_pex_builder(zip_safe=False), temporary_dir()) as (pb, pex_root):
    print("pb: %s" % pb)
    pex = pb.path()
    print("pex: %s" % pex)
    pb.info.pex_root = pex_root
    print("pex_root: %s" % pex_root)
    pb.freeze()
    import subprocess; subprocess.check_call('%s/*.pex' % pex)
