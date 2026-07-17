"""Pure-assert test runner — no pytest dependency.

Run with:  .venv/bin/python tests/run_all.py
Imports each test_*.py module, calls its run() function, prints PASS/FAIL,
and exits non-zero if any module raises. Tests are deterministic and offline
(synthetic DataFrames/dicts only — no network, no broker, no real orders).
"""
import importlib
import sys
import traceback
from pathlib import Path

# Make the repo root importable (so `import agents...` works).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = [
    "tests.test_screener",
    "tests.test_intel",
    "tests.test_exits",
    "tests.test_sizing",
    "tests.test_rotation",
    "tests.test_theme",
    "tests.test_regime",
    "tests.test_health",
    "tests.test_state",
]


def main() -> int:
    failures = 0
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
            mod.run()
            print(f"PASS  {name}")
        except Exception:
            failures += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(MODULES) - failures}/{len(MODULES)} modules passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
