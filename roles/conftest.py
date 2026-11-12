"""Put the code each role's tests exercise on the import path.

Every Python file a role carries sits under its files/ with tests in a
tests/ directory beside it, which import it by bare name. This finds every
such pair, so a new role's tests run with "make test" and nothing to register.
"""

import os
import sys
from pathlib import Path

# What git hands a hook, as `git rev-parse --local-env-vars` lists it. Run
# from the pre-commit hook, the tests would inherit them, and their own git
# commands would then write to the repository being committed: a test's
# `git init --bare` set core.bare in it. The tests make repositories of
# their own and need none of these.
for name in (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
):
    os.environ.pop(name, None)

for tests in sorted(Path(__file__).parent.glob("*/files/**/tests")):
    sys.path.insert(0, str(tests.parent))
