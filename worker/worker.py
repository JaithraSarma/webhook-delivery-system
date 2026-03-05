import json
import time
import os
import redis
import requests
import psycopg2
from datetime import datetime


REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@db:5432/webhooks")
DELIVERY_TIMEOUT = int(os.environ.get("DELIVERY_TIMEOUT", "5"))
DEFAULT_RATE_LIMIT = int(os.environ.get("DEFAULT_RATE_LIMIT", "0"))  # 0 means unlimited

r = redis.Redis.from_url(REDIS_URL)


def get_rate_limit():
    """Get the current global rate limit from Redis. Returns 0 if unlimited."""
    try:
        limit = r.get("rate_limit")
        if limit is not None:
            return int(limit)
    except Exception:
        pass
    return DEFAULT_RATE_LIMIT


def wait_for_rate_limit():
    """
    Token bucket style rate limiter using Redis.
    Uses a sliding window counter to enforce the global rate limit.
    """
    rate_limit = get_rate_limit()
    if rate_limit <= 0:
        return  # no rate limiting

    while True:
        current_time = int(time.time())
        key = f"rate_limit_window:{current_time}"

        # get current count for this second
        current_count = r.get(key)
        if current_count is None:
            current_count = 0
        else:
            current_count = int(current_count)

        if current_count < rate_limit:
            # increment and set expiry
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, 2)  # expire after 2 seconds
            pipe.execute()
            return
        else:
            # wait a bit and try again
            sleep_time = 1.0 / rate_limit if rate_limit > 0 else 0.1
            time.sleep(sleep_time)


def get_db_connection():
    """Create a new database connection."""
    return psycopg2.connect(DATABASE_URL)


def log_delivery(webhook_id, event_type, payload, status_code, success, error_message=None):
    """Log a delivery attempt to the database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO delivery_logs 
            (webhook_id, event_type, payload, status_code, success, error_message, delivered_at) 
            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (webhook_id, event_type, json.dumps(payload), status_code, success, error_message, datetime.utcnow())
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[WORKER] Failed to log delivery: {e}")


def deliver_webhook(job):
    """Attempt to deliver a webhook payload to the target URL."""
    webhook_id = job["webhook_id"]
    url = job["url"]
    event_type = job["event_type"]
    payload = job["payload"]
    user_id = job.get("user_id", "unknown")

    print(f"[WORKER] Delivering to {url} | event={event_type} | webhook_id={webhook_id} | user={user_id}")

    try:
        response = requests.post(
            url,
            json={
                "event_type": event_type,
                "payload": payload,
                "webhook_id": webhook_id,
                "timestamp": datetime.utcnow().isoformat(),
            },
            headers={"Content-Type": "application/json"},
            timeout=DELIVERY_TIMEOUT,
        )

        success = 200 <= response.status_code < 300
        log_delivery(webhook_id, event_type, payload, response.status_code, success)

        if success:
            print(f"[WORKER] SUCCESS: {url} responded with {response.status_code}")
        else:
            print(f"[WORKER] FAILED: {url} responded with {response.status_code}")

    except requests.exceptions.Timeout:
        print(f"[WORKER] TIMEOUT: {url}")
        log_delivery(webhook_id, event_type, payload, None, False, "Request timed out")
    except requests.exceptions.ConnectionError as e:
        print(f"[WORKER] CONNECTION ERROR: {url} - {e}")
        log_delivery(webhook_id, event_type, payload, None, False, str(e))
    except Exception as e:
        print(f"[WORKER] ERROR: {url} - {e}")
        log_delivery(webhook_id, event_type, payload, None, False, str(e))


def main():
    """Main worker loop: pop jobs from Redis queue and deliver."""
    print("[WORKER] Delivery worker started, waiting for jobs...")

    while True:
        try:
            # blocking pop with 1 second timeout
            result = r.brpop("delivery_queue", timeout=1)
            if result:
                _, job_data = result
                job = json.loads(job_data)

                # respect rate limit before delivering
                wait_for_rate_limit()

                deliver_webhook(job)
        except redis.exceptions.ConnectionError:
            print("[WORKER] Redis connection error, retrying in 2s...")
            time.sleep(2)
        except Exception as e:
            print(f"[WORKER] Unexpected error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    # wait for dependencies to be ready
    print("[WORKER] Waiting for services to start...")
    time.sleep(5)
    main()
