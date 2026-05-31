"""
Diagnostic utility for grouping unresolved review_queue matching failures.

Usage:
    python analyze_failures.py [path/to/deals.db]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/deals.db")
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return 1

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """
        SELECT failure_reason, matching_attempts
        FROM review_queue
        WHERE status IN ('pending', 'expired')
          AND COALESCE(matching_attempts, '') != ''
        """
    )

    reason_counter: Counter[str] = Counter()
    step_counter: Counter[str] = Counter()
    message_counter: Counter[str] = Counter()

    for failure_reason, attempts_raw in cur.fetchall():
        reason_counter[str(failure_reason or "unknown")] += 1
        try:
            attempts = json.loads(attempts_raw) if attempts_raw else []
        except json.JSONDecodeError:
            attempts = []

        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            step = str(attempt.get("step") or "unknown_step")
            message = str(attempt.get("message") or attempt.get("error") or "unknown_error")
            step_counter[step] += 1
            message_counter[message] += 1

    print("\n=== review_queue unresolved failure reasons ===")
    for reason, count in reason_counter.most_common(20):
        print(f"{count:>5}  {reason}")

    print("\n=== matching_attempts by step ===")
    for step, count in step_counter.most_common(20):
        print(f"{count:>5}  {step}")

    print("\n=== matching_attempts by message ===")
    for message, count in message_counter.most_common(20):
        print(f"{count:>5}  {message}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
