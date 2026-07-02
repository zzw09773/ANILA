from datetime import datetime, timedelta, timezone

RANGE_CONFIG = {
    "4h": {"hours": 4, "bucket_seconds": 300},       # 5 min buckets
    "12h": {"hours": 12, "bucket_seconds": 900},      # 15 min buckets
    "24h": {"hours": 24, "bucket_seconds": 1800},     # 30 min buckets
    "7d": {"hours": 168, "bucket_seconds": 21600},    # 6 hr buckets
    "30d": {"hours": 720, "bucket_seconds": 86400},   # 1 day buckets
}


def get_time_range(range_key: str) -> tuple[datetime, int]:
    config = RANGE_CONFIG.get(range_key)
    if not config:
        config = RANGE_CONFIG["24h"]
    start_time = datetime.now(timezone.utc) - timedelta(hours=config["hours"])
    return start_time, config["bucket_seconds"]
