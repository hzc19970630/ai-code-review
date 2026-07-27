import time

from mr_review_platform.config import get_settings
from mr_review_platform.logging import configure_logging
from mr_review_platform.worker import reconcile_once


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    while True:
        reconcile_once(settings)
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    main()
