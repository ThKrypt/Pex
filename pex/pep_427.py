# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import itertools
import json
import os.path
import re
import shutil
import subprocess
import sys
from textwrap import dedent

from pex import pex_warnings
from pex.common import chmod_plus_x, is_pyc_file, open_zip, safe_open, touch
from pex.compatibility import commonpath, urlparse
from pex.dist_metadata import (
    DistMetadata,
    Distribution,
    MetadataFiles,
    MetadataType,
    ProjectNameAndVersion,
    load_metadata,
    parse_message,
)
from pex.interpreter import PythonInterpreter
from pex.pep_376 import InstalledFile, InstalledWheel, Record
from pex.pep_503 import ProjectName
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Iterable, List, Optional, Text

    import attr  # vendor:skip
else:
    from pex.third_party import attr


@attr.s(frozen=True)
class InstallPaths(object):

    CHROOT_STASH = ".prefix"

    @classmethod
    def chroot(
        cls,
        destination,  # type: str
        project_name,  # type: ProjectName
    ):
        # type: (...) -> InstallPaths
        base = os.path.join(destination, cls.CHROOT_STASH)
        return InstallPaths(
            purelib=destination,
            platlib=destination,
            headers=os.path.join(base, "include", "site", "pythonX.Y", project_name.raw),
            scripts=os.path.join(base, "bin"),
            data=base,
        )

    @classmethod
    def interpreter(cls, interpreter):
        # type: (PythonInterpreter) -> InstallPaths
        sysconfig_paths = interpreter.identity.paths
        return InstallPaths(
            purelib=sysconfig_paths["purelib"],
            platlib=sysconfig_paths["platlib"],
            headers=sysconfig_paths["include"],
            scripts=sysconfig_paths["scripts"],
            data=sysconfig_paths["data"],
        )

    purelib = attr.ib()  # type: str
    platlib = attr.ib()  # type: str
    headers = attr.ib()  # type: str
    scripts = attr.ib()  # type: str
    data = attr.ib()  # type: str

    def __getitem__(self, item):
        # type: (Text) -> str
        if "purelib" == item:
            return self.purelib
        elif "platlib" == item:
            return self.platlib
        elif "headers" == item:
            return self.headers
        elif "scripts" == item:
            return self.scripts
        elif "data" == item:
            return self.data
        raise KeyError("Not a known install path: {item}".format(item=item))


def install_wheel_chroot(
    wheel_path,  # type: str
    destination,  # type: str
    compile=False,  # type: bool
    requested=True,  # type: bool
):
    # type: (...) -> InstalledWheel

    metadata_files = install_wheel(
        wheel_path,
        InstallPaths.chroot(
            destination,
            project_name=ProjectNameAndVersion.from_filename(wheel_path).canonicalized_project_name,
        ),
        compile=compile,
        requested=requested,
    )

    record_relpath = metadata_files.metadata_file_rel_path("RECORD")
    assert (
        record_relpath is not None
    ), "The {module}.install_wheel function should always create a RECORD.".format(module=__name__)
    return InstalledWheel.save(
        prefix_dir=destination, stash_dir=InstallPaths.CHROOT_STASH, record_relpath=record_relpath
    )


def install_wheel_interpreter(
    wheel_path,  # type: str
    interpreter,  # type: PythonInterpreter
    compile=True,  # type: bool
    requested=True,  # type: bool
):
    # type: (...) -> MetadataFiles

    return install_wheel(
        wheel_path,
        InstallPaths.interpreter(interpreter),
        interpreter=interpreter,
        compile=compile,
        requested=requested,
    )


def install_wheel(
    wheel_path,  # type: str
    install_paths,  # type: InstallPaths
    interpreter=None,  # type: Optional[PythonInterpreter]
    compile=False,  # type: bool
    requested=True,  # type: bool
):
    # type: (...) -> MetadataFiles

    # See: https://packaging.python.org/en/latest/specifications/binary-distribution-format/#installing-a-wheel-distribution-1-0-py32-none-any-whl
    metadata_files = load_metadata(wheel_path, restrict_types_to=(MetadataType.DIST_INFO,))
    if not metadata_files:
        raise ValueError("Could not find any metadata in {wheel}.".format(wheel=wheel_path))

    wheel_metadata_path = metadata_files.metadata_file_rel_path("WHEEL")
    wheel_metadata = metadata_files.read("WHEEL")
    if not wheel_metadata_path or not wheel_metadata:
        raise ValueError("Could not find WHEEL metadata in {wheel}.".format(wheel=wheel_path))
    wheel_metadata_dir = os.path.dirname(wheel_metadata_path)
    if not wheel_metadata_dir.endswith(".dist-info"):
        raise ValueError(
            "Expected WHEEL metadata for {wheel} to be housed in a .dist-info directory, but was "
            "found at {wheel_metadata_path}.".format(
                wheel=wheel_path, wheel_metadata_path=wheel_metadata_path
            )
        )

    purelib = "true" == parse_message(wheel_metadata).get("Root-Is-Purelib")
    dest = install_paths.purelib if purelib else install_paths.platlib

    record_relpath = os.path.join(wheel_metadata_dir, "RECORD")
    record_abspath = os.path.join(dest, record_relpath)

    data_rel_path = re.sub(r"\.dist-info$", ".data", wheel_metadata_dir)
    data_path = os.path.join(dest, data_rel_path)

    installed_files = []  # type: List[InstalledFile]

    def record_files(
        root_dir,  # type: Text
        names,  # type: Iterable[Text]
    ):
        # type: (...) -> None
        for name in sorted(names):
            if is_pyc_file(name):
                # These files are both optional to RECORD and should never be present in wheels
                # anyway per the spec.
                continue
            file_abspath = os.path.join(root_dir, name)
            if record_relpath == name:
                # We'll generate a new RECORD below.
                os.unlink(file_abspath)
                continue
            installed_files.append(
                InstalledWheel.create_installed_file(path=file_abspath, dest_dir=dest)
            )

    with open_zip(wheel_path) as zf:
        zf.extractall(dest)
        # TODO(John Sirois): Consider verifying signatures.
        # N.B.: Pip does not and its also not clear what good this does. A zip can be easily poked
        # on a per-entry basis allowing forging a RECORD entry and its associated file. Only an
        # outer fingerprint of the whole wheel really solves this sort of tampering.
        record_files(
            root_dir=dest,
            names=[
                name
                for name in zf.namelist()
                if not name.endswith("/") and data_rel_path != commonpath((data_rel_path, name))
            ],
        )
        if os.path.isdir(data_path):
            for entry in sorted(os.listdir(data_path)):
                try:
                    dest_dir = install_paths[entry]
                except KeyError as e:
                    raise ValueError(
                        "The wheel at {wheel_path} is invalid and cannot be installed: "
                        "{err}".format(wheel_path=wheel_path, err=e)
                    )
                entry_path = os.path.join(data_path, entry)
                shutil.copytree(entry_path, dest_dir)
                record_files(
                    root_dir=dest_dir,
                    names=[
                        os.path.relpath(os.path.join(root, f), entry_path)
                        for root, _, files in os.walk(entry_path)
                        for f in files
                    ],
                )
            shutil.rmtree(data_path)

    if compile:
        args = [
            interpreter.binary if interpreter else sys.executable,
            "-sE",
            "-m",
            "compileall",
        ]  # type: List[Text]
        py_files = [
            os.path.join(dest, installed_file.path)
            for installed_file in installed_files
            if installed_file.path.endswith(".py")
        ]
        process = subprocess.Popen(
            args=args + py_files, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _, stderr = process.communicate()
        if process.returncode != 0:
            pex_warnings.warn(
                "Failed to compile some .py files for install of {wheel} to {dest}:\n"
                "{stderr}".format(wheel=wheel_path, dest=dest, stderr=stderr.decode("utf-8"))
            )
        for root, _, files in os.walk(commonpath(py_files)):
            for f in files:
                if f.endswith(".pyc"):
                    file = InstalledFile(path=os.path.relpath(os.path.join(root, f), dest))
                    installed_files.append(file)

    dist = Distribution(location=dest, metadata=DistMetadata.from_metadata_files(metadata_files))
    entry_points = dist.get_entry_map()
    for entry_point in itertools.chain.from_iterable(
        entry_points.get(key, {}).values() for key in ("console_scripts", "gui_scripts")
    ):
        script_abspath = os.path.join(install_paths.scripts, entry_point.name)
        with safe_open(script_abspath, "w") as fp:
            fp.write(
                dedent(
                    """\
                    {shebang}
                    # -*- coding: utf-8 -*-
                    import importlib
                    import sys

                    object_ref = "{object_ref}"
                    modname, qualname_separator, qualname = object_ref.partition(':')
                    entry_point = importlib.import_module(modname)
                    if qualname_separator:
                        for attr in qualname.split('.'):
                            entry_point = getattr(entry_point, attr)

                    if __name__ == '__main__':
                        sys.exit(entry_point())
                    """
                ).format(
                    shebang=interpreter.shebang() if interpreter else "#!python",
                    object_ref=str(entry_point),
                )
            )
        chmod_plus_x(fp.name)
        installed_files.append(
            InstalledWheel.create_installed_file(path=script_abspath, dest_dir=dest)
        )

    with safe_open(os.path.join(dest, wheel_metadata_dir, "INSTALLER"), "w") as fp:
        print("pex", file=fp)
    installed_files.append(InstalledWheel.create_installed_file(path=fp.name, dest_dir=dest))

    if requested:
        requested_path = os.path.join(dest, wheel_metadata_dir, "REQUESTED")
        touch(requested_path)
        installed_files.append(
            InstalledWheel.create_installed_file(path=requested_path, dest_dir=dest)
        )

    installed_files.append(InstalledFile(path=record_relpath, hash=None, size=None))
    Record.write(dst=record_abspath, installed_files=installed_files)
    return metadata_files
