"""
The Langfuse project this process belongs to (``tl.project``). No ``langfuse`` import here, so the logs
and metrics modules can use it in services that don't install LangFuse (CONTRACT.md §8).
"""

import os
import re
import warnings

# Resource attribute the Collector routes on (otel/projects.yaml). See CONTRACT.md §1.
PROJECT_ATTR = "tl.project"
PROJECT_ENV = "LANGFUSE_PROJECT"
_PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def get_project() -> "str | None":
    """Return the Langfuse project slug this process traces into, or None.

    Read from LANGFUSE_PROJECT. The slug must be listed in the platform's
    otel/projects.yaml; an unknown slug is not an error -- the Collector
    routes it to the default project -- so a malformed one only warns.
    One project per process (see docs/MULTI-PROJECT.md for the multi-project
    provision).
    """
    project = os.environ.get(PROJECT_ENV, "").strip()
    if not project:
        return None
    if not _PROJECT_SLUG_RE.match(project):
        warnings.warn(
            f"{PROJECT_ENV}={project!r} is not a valid project slug "
            f"({_PROJECT_SLUG_RE.pattern}); the Collector will route these traces "
            "to the default project.",
            stacklevel=2,
        )
    return project
