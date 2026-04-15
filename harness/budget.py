"""Hard spend cap tracker. Reads from audit log for persistence across restarts."""

import json
import threading
from pathlib import Path

from config import config


HARD_BUDGET_USD = config.HARD_BUDGET_USD
MIN_RUN_COST_USD = config.MIN_RUN_COST_USD


class BudgetTracker:
    def __init__(self, hard_limit: float = HARD_BUDGET_USD):
        self._hard_limit = hard_limit
        self._lock = threading.Lock()
        self._spent = self._load_from_log()

    def _load_from_log(self) -> float:
        """Restore cumulative spend from the audit log on startup."""
        audit_log = config.AUDIT_LOG
        if not audit_log.exists():
            return 0.0
        total = 0.0
        try:
            with open(audit_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        cost = record.get("cost_usd")
                        if cost is not None:
                            total += float(cost)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            return 0.0
        return total

    def spent(self) -> float:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            return self._hard_limit - self._spent

    def can_dispatch(self, estimated_cost: float = MIN_RUN_COST_USD) -> bool:
        return self.remaining() >= estimated_cost

    def record(self, cost_usd: float) -> float:
        """Add cost and return new cumulative total. Raises if hard limit exceeded."""
        with self._lock:
            self._spent += cost_usd
            if self._spent > self._hard_limit:
                raise BudgetExceededError(
                    f"Hard budget ${self._hard_limit:.2f} exceeded: "
                    f"spent ${self._spent:.2f}"
                )
            return self._spent

    def cumulative(self) -> float:
        return self.spent()


class BudgetExceededError(Exception):
    pass


# ── unit tests ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # Temporarily override config paths for testing
        tmp_path = Path(tmp)
        test_audit_log = tmp_path / "audit.jsonl"

        # Write a test record so _load_from_log has something to read
        test_audit_log.write_text('{"cost_usd": 0.1}\n')

        t = BudgetTracker(hard_limit=5.00)
        # 5.00 - 0.10 (from test record) = 4.90
        assert abs(t.remaining() - 4.90) < 1e-9, f"Expected 4.90, got {t.remaining()}"
        t.record(1.50)
        assert abs(t.remaining() - 3.40) < 1e-9, f"Expected 3.40, got {t.remaining()}"
        assert t.can_dispatch(3.40)
        assert not t.can_dispatch(3.41)
        t.record(3.39)
        assert abs(t.remaining() - 0.01) < 1e-9

        try:
            t.record(0.02)
            assert False, "Should have raised"
        except BudgetExceededError:
            pass

        print("budget.py: all tests passed")
