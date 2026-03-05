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
    Sliding window counter rate limiter using Redis.
    Uses one key per second to count deliveries.
    Blocks until a delivery slot is available.
    """
    rate_limit = get_rate_limit()
    if rate_limit <= 0:
        return  # no rate limiting

    while True:
        current_time = int(time.time())
        key = f"rate_limit_window:{current_time}"

        # atomically increment and check
        count = r.incr(key)

        # set expiry on first access
        if count == 1:
            r.expire(key, 3)

        if count <= rate_limit:
            # under the limit, proceed
            return
        else:
            # over the limit, undo the increment and wait
            r.decr(key)
            # sleep until the next second
            sleep_until = current_time + 1
            sleep_duration = sleep_until - time.time()
            if sleep_duration > 0:
                time.sleep(sleep_duration)


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


def get_active_users():
    """Get the set of users who have pending deliveries."""
    try:
        users = r.smembers("active_users")
        return [u.decode("utf-8") if isinstance(u, bytes) else u for u in users]
    except Exception:
        return []


def pop_job_from_user(user_id):
    """Pop one job from a specific user's queue."""
    result = r.rpop(f"user_queue:{user_id}")
    if result:
        # check if user queue is now empty, remove from active set
        if r.llen(f"user_queue:{user_id}") == 0:
            r.srem("active_users", user_id)
        return json.loads(result)
    else:
        # queue is empty, remove from active set
        r.srem("active_users", user_id)
        return None


def main():
    """
    Main worker loop with round-robin fair scheduling.
    
    Instead of a single FIFO queue, we use per-user queues and cycle
    through users in round-robin fashion. This ensures that one user
    flooding the system with events doesn't starve other users.
    
    Algorithm:
    1. Get all active users (those with pending deliveries)
    2. For each user, pop one delivery job and process it
    3. Repeat, cycling through all users equally
    """
    print("[WORKER] Delivery worker started with fair scheduling (round-robin)...")

    while True:
        try:
            active_users = get_active_users()

            if not active_users:
                # no pending jobs, wait briefly
                time.sleep(0.1)
                continue

            # round-robin: process one job per user
            delivered_any = False
            for user_id in active_users:
                job = pop_job_from_user(user_id)
                if job:
                    # respect rate limit before delivering
                    wait_for_rate_limit()
                    deliver_webhook(job)
                    delivered_any = True

            if not delivered_any:
                time.sleep(0.1)

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
