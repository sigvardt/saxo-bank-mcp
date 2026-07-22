from __future__ import annotations

import json
import os
import sys
from typing import Any


def main() -> int:
    read_fd = int(sys.argv[1])
    write_fd = int(sys.argv[2])
    with os.fdopen(read_fd, encoding="utf-8") as source:
        events: list[Any] = [json.loads(line) for line in source if line.strip()]
    with os.fdopen(write_fd, "w", encoding="utf-8") as destination:
        json.dump(events, destination, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
