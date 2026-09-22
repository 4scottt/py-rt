"""Traceability: every function point of ``docs/function-points.md`` has a test.

The ids come from the matrix; a test claims one by its name,
``def test_fp_<id lower-case>_…``. Three sets keep the check honest:

``PENDING``
    ids a later milestone's package will test. The check fails when a
    pending id already has a test, so a package that tests an id must take
    it out of this set in the same change.
``CI_ONLY``
    ids a Python test cannot make (O07 is the image build and its
    footprint: a CI job asserts it).
``WALK_ONLY``
    ids the Playwright walk makes (O08 is the walk itself).

An id in none of the three must have a test, or this fails.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX = REPO_ROOT / "docs" / "function-points.md"
TESTS_DIR = REPO_ROOT / "tests"

ID_IN_MATRIX = re.compile(r"^\| ([LQTSFURMO]\d\d) \|")
TEST_NAME = re.compile(r"^\s*def (test_fp_([lqtsfurmo]\d\d)_\w*)", re.MULTILINE)

#: Tested elsewhere than in Python.
CI_ONLY = frozenset({"O07"})
WALK_ONLY = frozenset({"O08"})

#: Not yet due. M1-foundation tests O01, Q05 and U04; M1-auth-acl-shell the
#: sign-in, session, shell and resolver ids; every other id belongs to a later
#: package, which removes it from here as it tests it.
PENDING = frozenset(
    {
        "O03", "O04",
    }
)  # fmt: skip


def matrix_ids() -> list[str]:
    """Every function-point id, in the order the matrix lists them."""
    ids: list[str] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        match = ID_IN_MATRIX.match(line)
        if match:
            ids.append(match.group(1))
    return ids


def ids_claimed_by_tests() -> dict[str, list[str]]:
    """The ids claimed by a test name, each with the names claiming it."""
    found: dict[str, list[str]] = {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        for name, fp_id in TEST_NAME.findall(path.read_text(encoding="utf-8")):
            found.setdefault(fp_id.upper(), []).append(f"{path.name}::{name}")
    return found


def test_matrix_parses() -> None:
    ids = matrix_ids()
    assert len(ids) == 70, f"expected the 70 function points of plan §9, read {len(ids)}"
    assert len(set(ids)) == len(ids), "a function-point id appears twice"


def test_every_function_point_is_tested_or_declared_pending() -> None:
    ids = set(matrix_ids())
    tested = ids_claimed_by_tests()

    unknown = set(tested) - ids
    assert not unknown, f"tests claim ids the matrix does not list: {sorted(unknown)}"

    exempt = PENDING | CI_ONLY | WALK_ONLY
    missing = sorted(i for i in ids - exempt if i not in tested)
    assert not missing, f"no test names these function points: {missing}"

    stale = sorted(i for i in PENDING if i in tested)
    assert not stale, (
        f"these ids have a test but are still listed PENDING, remove them: {stale} "
        f"({[tested[i] for i in stale]})"
    )

    covered_elsewhere = sorted((CI_ONLY | WALK_ONLY) & set(tested))
    assert not covered_elsewhere, (
        f"CI_ONLY/WALK_ONLY ids should not have a Python test: {covered_elsewhere}"
    )
