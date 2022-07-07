# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import json
import os
from textwrap import dedent

import pytest

from pex.interpreter import PythonInterpreter
from pex.resolve.lockfile import json_codec
from pex.targets import LocalInterpreter, Target
from pex.testing import PY27, PY37, IntegResults, ensure_python_interpreter, run_pex_command
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


def create_target(python_version):
    # type: (str) -> Target
    return LocalInterpreter.create(
        PythonInterpreter.from_binary(ensure_python_interpreter(python_version))
    )


@pytest.fixture
def py27():
    return create_target(PY27)


@pytest.fixture
def py37():
    return create_target(PY37)


LOCK_STYLE_SOURCES = json_codec.loads(
    dedent(
        """\
        {
          "allow_builds": true,
          "allow_prereleases": false,
          "allow_wheels": true,
          "build_isolation": true,
          "constraints": [],
          "locked_resolves": [
            {
              "locked_requirements": [
                {
                  "artifacts": [
                    {
                      "algorithm": "sha256",
                      "hash": "20129f25683fab2099d954379fecd36c13ccc0cc0159eaf59afee53a23d749f1",
                      "url": "https://files.pythonhosted.org/packages/7c/39/fcd0a978eb327ce8d170ee763264cee1a3a43b0e5f962312d4a37567523d/p537-1.0.4-cp37-cp37m-manylinux1_x86_64.whl"
                    },
                    {
                      "algorithm": "sha256",
                      "hash": "b1818f434c559706039fa6ca9812f120fc6421b977d5862fb7b411ebaffc074f",
                      "url": "https://files.pythonhosted.org/packages/56/05/6f01bef57523f6ab7ba5b8fa9831a2204c7ef49dfc194c0d689863f3ae1c/p537-1.0.4.tar.gz"
                    }
                  ],
                  "project_name": "p537",
                  "requires_dists": [],
                  "requires_python": null,
                  "version": "1.0.4"
                }
              ],
              "platform_tag": [
                "cp37",
                "cp37m",
                "manylinux_2_33_x86_64"
              ]
            }
          ],
          "pex_version": "2.1.50",
          "prefer_older_binary": false,
          "requirements": [
            "p537==1.0.4"
          ],
          "requires_python": [],
          "resolver_version": "pip-legacy-resolver",
          "style": "sources",
          "transitive": true,
          "use_pep517": null
        }
        """
    )
)


def test_lockfile_style_sources(
    py27,  # type: Target
    py37,  # type: Target
    tmpdir,  # type: Any
):
    # type: (...) -> None

    lockfile = os.path.join(str(tmpdir), "lock.json")
    with open(lockfile, "w") as fp:
        json.dump(json_codec.as_json_data(LOCK_STYLE_SOURCES), fp, sort_keys=True, indent=2)

    def use_lock(target):
        # type: (Target) -> IntegResults
        return run_pex_command(
            args=["--lock", lockfile, "--", "-c", "import p537"],
            python=target.get_interpreter().binary,
        )

    use_lock(py37).assert_success()

    # N.B.: We created a lock above that falsely advertises there is a solution for Python 2.7.
    # This is the devil's bargain with non-strict lock styles and the lock will fail only some time
    # later when it is exercised using the wrong interpreter.
    result = use_lock(py27)
    result.assert_failure()
    assert "Building wheel for p537 (setup.py): started" in result.error
    assert "Building wheel for p537 (setup.py): finished with status 'error'" in result.error
