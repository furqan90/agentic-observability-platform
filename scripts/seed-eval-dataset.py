"""
Seed LangFuse with a baseline eval dataset so Day-2 evaluation has something
to run against. Idempotent: re-running upserts the same items.

Usage (from repo root, with the stack up):
    pip install langfuse
    export LANGFUSE_PUBLIC_KEY=pk-lf-local
    export LANGFUSE_SECRET_KEY=sk-lf-local
    export LANGFUSE_HOST=http://localhost:3000
    python scripts/seed-eval-dataset.py
"""

import os
import sys

from langfuse import Langfuse

DATASET_NAME = "baseline-agent-eval"

# (input, expected_output) pairs — replace with real client-representative cases.
EXAMPLES = [
    ("What is our PTO policy?", "Employees accrue 1.5 days of PTO per month."),
    ("How do I reset my VPN password?", "Use the self-service portal at vpn.example.com/reset."),
    ("Summarize the Q3 roadmap.", "Three themes: observability, AKS migration, client onboarding."),
    ("Which model should I use for code review?", "Use the configured default coding model via the SDK."),
]


def main() -> int:
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set. See the docstring.", file=sys.stderr)
            return 1

    lf = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
    )

    lf.create_dataset(
        name=DATASET_NAME,
        description="Baseline agent eval cases seeded by scripts/seed-eval-dataset.py",
    )

    for i, (question, expected) in enumerate(EXAMPLES, start=1):
        lf.create_dataset_item(
            dataset_name=DATASET_NAME,
            input={"question": question},
            expected_output={"answer": expected},
            metadata={"seed": True, "case": i},
        )

    lf.flush()
    print(f"Seeded {len(EXAMPLES)} items into dataset '{DATASET_NAME}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
