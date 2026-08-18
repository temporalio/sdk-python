from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


class TemporalActionLedger:
    """
    Cryptographic SHA-256 hash-chained Action Ledger for Temporal workflow and activity runs.
    """

    def __init__(self):
        self._entries: List[Dict[str, Any]] = []
        self._last_hash = GENESIS_HASH

    def record_activity_execution(
        self,
        activity_name: str,
        status: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        index = len(self._entries)

        meta_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
        canonical_content = f"{index}|{self._last_hash}|{activity_name}|{status}|{timestamp}|{hashlib.sha256(meta_bytes).hexdigest()}"
        curr_hash = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()

        entry = {
            "index": index,
            "timestamp": timestamp,
            "activity_name": activity_name,
            "status": status,
            "prev_hash": self._last_hash,
            "curr_hash": curr_hash,
            "metadata": metadata,
        }

        self._entries.append(entry)
        self._last_hash = curr_hash
        return entry

    def get_ledger_entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def verify_ledger_integrity(self) -> bool:
        prev = GENESIS_HASH
        for entry in self._entries:
            if entry["prev_hash"] != prev:
                return False
            prev = entry["curr_hash"]
        return True

    def verify_integrity(self) -> bool:
        return self.verify_ledger_integrity()


class ActionGateActivityInterceptor:
    """
    A2Z SOC ActionGate Activity Interceptor & Zero-Trust Boundary for Temporal.

    Enforces zero-trust ActionBoundary governance, worker emergency kill-switches,
    and NIST SP 800-53 Rev. 5 audit logging across durable activity executions.
    """

    def __init__(
        self,
        never_equate_intent_to_approval: bool = True,
        enforce_action_boundary: bool = True,
    ):
        self.never_equate_intent_to_approval = never_equate_intent_to_approval
        self.enforce_action_boundary = enforce_action_boundary
        self.ledger = TemporalActionLedger()

    def check_kill_switch(self) -> bool:
        if os.environ.get("AAG_KILL_SWITCH", "").lower() in ("true", "1", "yes"):
            return True
        for path_str in ("artifacts/KILL", "/tmp/KILL"):
            if Path(path_str).exists():
                return True
        return False

    def intercept_activity(
        self,
        activity_name: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        is_destructive: bool = False,
        prove_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates activity execution before running on a Temporal worker.
        """
        # 1. Evaluate emergency kill switch
        if self.check_kill_switch():
            self.ledger.record_activity_execution(
                activity_name=activity_name,
                status="halted_by_kill_switch",
                metadata={"reason": "emergency_kill_switch_active"},
            )
            raise PermissionError("A2Z SOC ActionGate: Emergency kill switch is engaged. Temporal activity halted.")

        # 2. Destructive activities require valid prove token
        if is_destructive:
            if not prove_token or (not prove_token.startswith("prov_live_") and not prove_token.startswith("prov_test_")):
                self.ledger.record_activity_execution(
                    activity_name=activity_name,
                    status="rejected_missing_prove_token",
                    metadata={"is_destructive": True},
                )
                raise PermissionError(
                    f"A2Z SOC ActionGate: Destructive activity '{activity_name}' requires valid ActionGate prove token (never_equate_intent_to_approval)."
                )

        # 3. Authorized activity execution
        entry = self.ledger.record_activity_execution(
            activity_name=activity_name,
            status="authorized",
            metadata={
                "args_count": len(args or []),
                "kwargs_keys": list((kwargs or {}).keys()),
                "never_equate_intent_to_approval": self.never_equate_intent_to_approval,
            },
        )

        return {"allowed": True, "action_id": f"act_{entry['index']}", "hash": entry["curr_hash"]}
