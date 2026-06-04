"""
 LangFuse v3 tracer — single import for all agents.
Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env.

This is the ONLY module in the codebase allowed to import `langfuse`.
If we ever leave LangFuse, we rewrite this one package — not N agents.
"""

import os
from functools import lru_cache

from langfuse import Langfuse, get_client


@lru_cache(maxsize=1)
def get_tracer() -> Langfuse:
    # Constructing Langfuse() registers the global client; get_client()
    # then returns that same instance from anywhere in the process.
    Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        environment=os.environ.get("DEPLOY_ENV", "local"),
    )
    return get_client()
