"""Run a demo scenario end to end. One command per scenario — SPEC §11.

    .venv/bin/python scripts/run_demo.py happy      # Act 1: purchase completes
    .venv/bin/python scripts/run_demo.py blocked    # Act 2: gate blocks it

Both write an evidence packet to evidence/demo/ and persist the ledger to
evidence/ledgers/ so the ledger browser can render it.

Everything runs against fake_prava; no Prava sandbox quota is spent. Add
--no-llm to skip the live gpt-5-mini calls (agent rationale + export narrative).
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from backend.demo import run_scenario  # noqa: E402

DEFAULT_OUT = os.path.join(REPO_ROOT, "evidence", "demo")
DEFAULT_LEDGERS = os.path.join(REPO_ROOT, "evidence", "ledgers")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["happy", "blocked"])
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--ledgers", default=DEFAULT_LEDGERS)
    parser.add_argument(
        "--no-llm", action="store_true", help="skip live gpt-5-mini calls"
    )
    args = parser.parse_args()

    print("=" * 72)
    outcome = run_scenario(
        args.scenario,
        out_dir=args.out,
        live_llm=not args.no_llm,
        ledger_dir=args.ledgers,
    )
    print("=" * 72)

    if outcome["blocked"]:
        print(f"RESULT: BLOCKED at the gate — {outcome['blocked']['failed_rule_ids']}")
        print("No payment session was created. The blocked run is itself the evidence.")
        return 0

    print(f"RESULT: {outcome['result']['visa_confirmation']} — order "
          f"{outcome['result']['order_number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
