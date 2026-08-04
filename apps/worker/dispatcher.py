import time

from mr_review_platform.config import get_settings
from mr_review_platform.dispatcher import dispatch_outbox_once
from mr_review_platform.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    while True:
        dispatch_outbox_once(settings)
        time.sleep(settings.dispatch_interval_seconds)


if __name__ == "__main__":
    main()
