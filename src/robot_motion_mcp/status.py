"""Canonical state semantics shared by all adapters.

Two independent state axes are kept apart on purpose:

* planner-run state (task management view): PENDING / RUNNING / COMPLETED / FAILED / TIMEOUT / UNKNOWN
* independent-validation state: NOT_VALIDATED / PASSED / FAILED / INCOMPLETE / TOOL_ERROR / UNKNOWN

Raw status strings come verbatim from upstream record files; every canonical value
produced by this module is accompanied by ``raw_status`` and a mapping rule so a
consumer can always audit the derivation. Timeouts are never reported as proof of
infeasibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- canonical enumerations -------------------------------------------------

PLANNER_STATES = (
    "PENDING",  # registered/gated but never attempted
    "RUNNING",  # not used by Phase 1 read-only snapshots, reserved
    "COMPLETED",  # planner finished and produced its recorded outcome
    "FAILED",
    "TIMEOUT",
    "UNKNOWN",
)

VALIDATION_STATES = (
    "NOT_VALIDATED",  # a candidate exists but no independent validation record
    "PASSED",  # independent validation record says passed
    "FAILED",  # independent validation record says failed
    "INCOMPLETE",  # validation started but did not complete (budget/tool timeout)
    "TOOL_ERROR",  # validation tooling itself failed/blocked
    "UNKNOWN",
)

ERROR_CLASSES = (
    None,
    "search_timeout",
    "harness_error",
    "process_failure",
    "input_contract_blocked",
    "unverified_success",
    "not_run_gated",
    "execution_failed",
    "invalid_reference_input",
    "unclassified",
)


@dataclass(frozen=True)
class StatusSemantics:
    """Canonical reading of one raw status string."""

    planner_state: str
    validation_state: str
    attempted: bool
    candidate_found: bool | None  # None = not applicable / not derivable
    error_class: str | None
    caveats: tuple[str, ...] = field(default_factory=tuple)


def _s(
    planner_state: str,
    validation_state: str,
    attempted: bool = True,
    candidate_found: bool | None = None,
    error_class: str | None = None,
    caveats: tuple[str, ...] = (),
) -> StatusSemantics:
    return StatusSemantics(planner_state, validation_state, attempted, candidate_found, error_class, caveats)


TIMEOUT_NOT_INFEASIBLE = "timeout is not a proof of infeasibility"

# Exact-match map for raw status strings found in upstream record files.
RAW_STATUS_MAP: dict[str, StatusSemantics] = {
    # --- candidate planner runs (rm_next_009 worker statuses) ---
    "CANDIDATE_SUCCESS_PENDING_REFERENCE": _s(
        "COMPLETED",
        "NOT_VALIDATED",
        candidate_found=True,
        caveats=("candidate path exists; independent reference validation is a separate record",),
    ),
    "SEARCH_TIMEOUT": _s(
        "TIMEOUT",
        "NOT_VALIDATED",
        error_class="search_timeout",
        caveats=(TIMEOUT_NOT_INFEASIBLE,),
    ),
    "HARD_TIMEOUT_PARTIAL_LOGS": _s(
        "TIMEOUT",
        "NOT_VALIDATED",
        error_class="search_timeout",
        caveats=(TIMEOUT_NOT_INFEASIBLE, "process hit hard cap, only partial logs written"),
    ),
    "HARNESS_ERROR": _s("FAILED", "TOOL_ERROR", error_class="harness_error"),
    "PROCESS_FAILURE": _s("FAILED", "TOOL_ERROR", error_class="process_failure"),
    "INPUT_CONTRACT_BLOCKED": _s(
        "FAILED",
        "NOT_VALIDATED",
        error_class="input_contract_blocked",
    ),
    # --- aggregated final statuses (rm_next_009 analyze.py) ---
    "SUCCESS_REFERENCE_GRID": _s(
        "COMPLETED",
        "PASSED",
        candidate_found=True,
        caveats=("passing scope is the recorded reference grid, not a continuous certificate",),
    ),
    "UNVERIFIED_SUCCESS": _s(
        "COMPLETED",
        "INCOMPLETE",
        candidate_found=True,
        error_class="unverified_success",
        caveats=(
            "candidate passed the shared terminal check, but the independent "
            "reference validation did not complete within budget; NOT a validated path",
        ),
    ),
    "REFERENCE_FAIL": _s(
        "FAILED",
        "FAILED",
        candidate_found=True,
        caveats=("independent reference validation rejected the candidate",),
    ),
    "CANDIDATE_VALIDATION_TIMEOUT": _s(
        "COMPLETED",
        "INCOMPLETE",
        candidate_found=True,
        error_class="search_timeout",
        caveats=(TIMEOUT_NOT_INFEASIBLE,),
    ),
    "RULE_GENERATION_TIMEOUT": _s(
        "UNKNOWN",
        "INCOMPLETE",
        error_class="search_timeout",
        caveats=(TIMEOUT_NOT_INFEASIBLE,),
    ),
    "ROADMAP_NOT_READY": _s("UNKNOWN", "INCOMPLETE", caveats=("roadmap not available for query",)),
    # --- independent reference/validation records ---
    "FREE_REFERENCE_GRID": _s(
        "COMPLETED",
        "PASSED",
        candidate_found=True,
        caveats=("passing scope is the recorded reference grid, not a continuous certificate",),
    ),
    # --- per-state geometry verdicts (not run-level; exposed as-is) ---
    "FREE_NUMERIC": _s("COMPLETED", "PASSED", candidate_found=None),
    "REJECT": _s("COMPLETED", "FAILED", candidate_found=None),
    "UNRESOLVED": _s("UNKNOWN", "UNKNOWN", caveats=("unresolved state is neither pass nor fail",)),
    "UNKNOWN_UNRESOLVED": _s("UNKNOWN", "UNKNOWN"),
    # --- execution-level statuses (batch runs.json / receipts) ---
    "completed": _s("COMPLETED", "UNKNOWN", caveats=("execution-level record; carries no geometry claim",)),
    "failed": _s("FAILED", "UNKNOWN", error_class="execution_failed"),
}

# Status prefixes that mean "gated, never attempted" (registry / decision documents).
NOT_RUN_PREFIXES = ("NOT_RUN", "NOT_AUTHORIZED", "NOT_INVOKED")


def map_raw_status(raw_status: str | None) -> StatusSemantics:
    """Map an upstream raw status string to canonical semantics.

    Unknown strings stay honest: UNKNOWN/UNKNOWN with an ``unclassified`` error class
    and the raw value preserved for the consumer.
    """
    if raw_status is None:
        return _s("UNKNOWN", "UNKNOWN", attempted=False, error_class=None)
    if raw_status in RAW_STATUS_MAP:
        return RAW_STATUS_MAP[raw_status]
    if any(raw_status.startswith(p) for p in NOT_RUN_PREFIXES):
        return _s(
            "PENDING",
            "NOT_VALIDATED",
            attempted=False,
            error_class="not_run_gated",
            caveats=("run was gated out before execution; this is not a result either way",),
        )
    return _s(
        "UNKNOWN",
        "UNKNOWN",
        error_class="unclassified",
        caveats=("raw status not in the mapping table; see raw_status for the upstream value",),
    )
