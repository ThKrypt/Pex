# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
from contextlib import contextmanager
import subprocess
from textwrap import dedent

try:
  import mock
except ImportError:
  from unittest import mock
import pkg_resources
from twitter.common.contextutil import temporary_dir, temporary_file

from pex.common import safe_mkdir, safe_mkdtemp
from pex.compatibility import nested
from pex.environment import PEXEnvironment
from pex.pex_builder import PEXBuilder
from pex.pex_info import PexInfo
from pex.testing import make_bdist, run_simple_pex_test


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

@mock.patch('__builtin__.open')
@mock.patch('pex.environment.resource_string', spec=pkg_resources.resource_string)
@mock.patch('pex.environment.resource_isdir', spec=pkg_resources.resource_isdir)
@mock.patch('pex.environment.resource_listdir', spec=pkg_resources.resource_listdir)
def test_access_zipped_assets(mock_resource_listdir, mock_resource_isdir, mock_resource_string, mock_open):
  mock_resource_listdir.side_effect = [['./__init__.py', './directory/'], ['file.py']]
  mock_resource_isdir.side_effect = [False, True, False]
  mock_resource_string.return_value = 'testing'

  PEXEnvironment.access_zipped_assets('twitter.common', 'dirutil', 'dirutil')

  assert mock_resource_listdir.call_count == 2
  assert mock_open.call_count == 2
  file_handle = mock_open.return_value.__enter__.return_value
  assert file_handle.write.call_count == 2

def test_access_zipped_assets_integration():
  test_executable = dedent('''
      import os
      from _pex.environment import PEXEnvironment
      temp_dir = PEXEnvironment.access_zipped_assets('my_package', 'submodule', 'submodule')
      with open(os.path.join(temp_dir, 'mod.py'), 'r') as fp:
        for line in fp:
          print(line)
  ''')
  with nested(temporary_dir(), temporary_dir()) as (td1, td2):
    pb = PEXBuilder(path=td1)
    with open(os.path.join(td1, 'exe.py'), 'w') as fp:
      fp.write(test_executable)
      pb.set_executable(fp.name)

    submodule = os.path.join(td1, 'my_package', 'submodule')
    safe_mkdir(submodule)
    mod_path = os.path.join(submodule, 'mod.py')
    with open(mod_path, 'w') as fp:
      fp.write('accessed')
      pb.add_source(fp.name, 'my_package/submodule/mod.py')

    pex = os.path.join(td2, 'app.pex')
    pb.build(pex)

    po = subprocess.Popen(
        [pex],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    po.wait()
    assert po.stdout.read() == 'accessed\n'
    assert po.returncode == 0
