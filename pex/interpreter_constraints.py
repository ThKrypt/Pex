# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

# A library of functions for filtering Python interpreters based on compatibility constraints

from .common import die
from .interpreter import PythonIdentity
from .tracer import TRACER


def _matching(interpreters, constraints, meet_all_constraints=False):
  for interpreter in interpreters:
    check = all if meet_all_constraints else any
    if check(interpreter.identity.matches(filt) for filt in constraints):
      yield interpreter


def validate_constraints(constraints):
  # TODO: add check to see if constraints are mutually exclusive (bad) so no time is wasted
  # Check that the compatibility requirements are well-formed.
  for req in constraints:
    try:
      PythonIdentity.parse_requirement(req)
    except ValueError as e:
      die("Compatibility requirements are not formatted properly: %s" % str(e))


def matched_interpreters(interpreters, constraints, meet_all_constraints=False):
  """Given some filters, yield any interpreter that matches at least one of them, or all of them
     if meet_all_constraints is set to True.

  :param interpreters: a list of PythonInterpreter objects for filtering
  :param constraints: A sequence of strings that constrain the interpreter compatibility for this
    pex, using the Requirement-style format, e.g. ``'CPython>=3', or just ['>=2.7','<3']``
    for requirements agnostic to interpreter class.
  :param meet_all_constraints: whether to match against all filters.
    Defaults to matching interpreters that match at least one filter.
  """
  for match in _matching(interpreters, constraints, meet_all_constraints):
    TRACER.log("Constraints on interpreters: %s, Matching Interpreter: %s"
              % (constraints, match.binary), V=3)
    yield match
