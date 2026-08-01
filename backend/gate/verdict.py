"""Gate verdict — runs every rule and reports all of them.

Deliberately does not short-circuit. A proposal that breaks three rules should say
so: the ledger records the full result set, and the UI names every failed check.
Stopping at the first failure would make the evidence weaker and the demo vaguer.
"""

from backend.gate.rules import RULES


def evaluate(mandate, proposal, now):
    """Return {verdict, results, ...}. PASS iff every rule passes."""
    results = [rule(mandate, proposal, now) for rule in RULES]
    passed = all(result["pass"] for result in results)

    return {
        "verdict": "PASS" if passed else "FAIL",
        "results": results,
        "failed_rule_ids": [r["rule_id"] for r in results if not r["pass"]],
        "mandate_id": mandate["mandate_id"],
        "proposal_id": proposal["proposal_id"],
        "evaluated_at": now.isoformat(),
    }
