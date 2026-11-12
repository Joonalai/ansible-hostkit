"""Put the code each role's tests exercise on the import path.

Every Python file a role carries sits under its files/ with tests in a
tests/ directory beside it, which import it by bare name. This finds every
such pair, so a new role's tests run with "make test" and nothing to register.
"""

import sys
from pathlib import Path

for tests in sorted(Path(__file__).parent.glob("*/files/**/tests")):
    sys.path.insert(0, str(tests.parent))
