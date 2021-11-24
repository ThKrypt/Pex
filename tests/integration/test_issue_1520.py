# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import shutil
import subprocess

import pytest

from pex.testing import IS_LINUX, PY_VER, run_pex_command
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


@pytest.mark.skipif(
    PY_VER < (3, 6), reason="The mypy_protobuf 2.4 distribution is only available for Python 3.6+"
)
def test_hermetic_console_scripts(tmpdir):
    # type: (Any) -> None

    # N.B.: See pex/vendor/_vendored/pip/pip/_vendor/distlib/scripts.py lines 127-156.
    # https://github.com/pantsbuild/pex/blob/196b4cd5b8dd4b4af2586460530e9a777262be7d/pex/vendor/_vendored/pip/pip/_vendor/distlib/scripts.py#L127-L156
    length_pad = 127 if IS_LINUX else 512
    pex_root = os.path.join(str(tmpdir), "_" * length_pad, "pex_root")
    assert len(pex_root) > length_pad

    mypy_protobuf_pex = os.path.join(str(tmpdir), "mypy_protobuf.pex")

    run_pex_command(
        args=[
            "--pex-root",
            pex_root,
            "mypy_protobuf==2.4",
            "-o",
            mypy_protobuf_pex,
            "--venv",
            "prepend",
            "--seed",
        ],
    ).assert_success()

    scripts = [
        os.path.join(root, f)
        for root, dirs, files in os.walk(os.path.join(pex_root, "installed_wheels"))
        for f in files
        if "protoc-gen-mypy" == f
    ]
    assert 1 == len(scripts)
    with open(scripts[0]) as fp:
        assert "#!python" == fp.readline().strip()
        assert "# -*- coding: utf-8 -*-" == fp.readline().strip()

    shutil.rmtree(pex_root)
    # This should no-op (since there is no proto sent on stdin) and exit success.
    process = subprocess.Popen(
        [mypy_protobuf_pex, "-c", "import subprocess; subprocess.check_call(['protoc-gen-mypy'])"],
        stderr=subprocess.PIPE,
    )
    _, stderr = process.communicate()
    if IS_LINUX:
        # On modern Linux (starting with the 5.1 kernel shipped on May 19th 2019), the default max
        # shebang length limit is 256; so this should work fine.
        assert 0 == process.returncode
    else:
        # On Mac, we should hit a too long path since 512 has been a stable max path length there.
        # Although an (unlikely) error, this is the correct error to communicate to trigger fixes
        # through shortening the PEX_ROOT (much more likely fix) or somehow fixing Pex to deal with
        # this and yet still retain hermeticity (much less likely).
        assert 0 != process.returncode
        # We should see something like:
        # [Errno 63] File name too long: '/tmp/pytest-of-runner/pytest-0/popen-gw2/
        # test_hermetic_console_scripts0/<512 of `_`>/pex_root/isolated/
        # .488310d43ea7ca80b559c306f2db44914a184e37.atomic_directory.lck'
        err = stderr.decode("utf-8")
        assert "File name too long:" in err
        assert pex_root in err
