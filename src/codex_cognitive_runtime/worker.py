from __future__ import annotations

import sys

from .config import load_config
from . import logger
from .service import MemoryService


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        return 2
    event_id = argv[0]
    logger.info("worker started", event_id=event_id)
    service = MemoryService(load_config())
    try:
        try:
            result = service.process_event_id(event_id)
            logger.info("worker finished", event_id=event_id, result=result)
        except Exception as exc:
            service.ledger.mark_event_failed(event_id, str(exc))
            logger.error("worker failed", event_id=event_id, error=str(exc))
            return 1
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
