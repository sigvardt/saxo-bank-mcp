from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

TEMP_ENVIRONMENT_VARIABLES: Final = ("TMPDIR", "TMP", "TEMP")


def preserve_parent_temp_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy an isolated child env and add only missing parent temp variables."""
    child = dict(environment)
    for name in TEMP_ENVIRONMENT_VARIABLES:
        value = os.environ.get(name)
        if value is not None:
            child.setdefault(name, value)
    return child
