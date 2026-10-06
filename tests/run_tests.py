"""Test runner: executes every test in tests/ and reports pass/fail counts.

Usage:  venv/bin/python tests/run_tests.py
"""
import asyncio
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_api, test_db, test_turso_backend  # noqa: E402

MODULES = [test_db, test_api, test_turso_backend]


async def main() -> int:
    total = passed = failed = 0
    failures = []
    started = time.time()
    for mod in MODULES:
        for name, fn in mod.TESTS:
            total += 1
            label = f"{mod.__name__.split('.')[-1]}::{name}"
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
                passed += 1
                print(f"  ok   {label}")
            except Exception:
                failed += 1
                failures.append(label)
                print(f"  FAIL {label}")
                traceback.print_exc()
    elapsed = time.time() - started
    print(f"\n{passed}/{total} passed ({elapsed:.1f}s)")
    if failures:
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
