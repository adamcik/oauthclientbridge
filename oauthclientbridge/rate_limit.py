import hashlib
import time

from oauthclientbridge import app, db, stats


def clean():
    now = time.time()
    with db.cursor() as cursor:
        cursor.execute('DELETE FROM buckets WHERE updated < ? AND '
                       'value - (? - updated) * ? <= 0',
                       (now, now, app.config['OAUTH_BUCKET_REFILL_RATE']))
        return cursor.rowcount


def check(key, increment=1):
    """Decide if the given key should be rate limited.

    Calls are allowed whenever the bucket is below capacity. Each hit fills the
    bucket by one. Buckets drain at a configurable rate, though refill only
    happens when the bucket gets a hit. There is a maximum bucket fill to avoid
    callers being locket out for too long.

    Returns number of seconds you should wait before trying again.
    """
    if not app.config['OAUTH_RATE_LIMIT']:
        return False

    now = time.time()
    key = hashlib.sha256(key).hexdigest()
    refill = app.config['OAUTH_BUCKET_REFILL_RATE']
    capacity = app.config['OAUTH_BUCKET_CAPACITY']
    max_hits = app.config['OAUTH_BUCKET_MAX_HITS']

    with db.cursor() as cursor:
        with stats.DBLatencyHistorgram.labels(query='update_limit').time():
            cursor.execute(
                """
                INSERT OR REPLACE INTO buckets (key, updated, value)
                SELECT
                   -- 1. Empty bucket by '(now - updated) * refill_rate'
                   -- 2. Set to zero if we emptied too much.
                   -- 3. Limit bucket fullness to max_hits
                   ?, ?, MIN(? + MAX(0, value - ((? - updated) * ?)), ?)
                FROM (
                  WITH bucket AS (SELECT * FROM buckets WHERE key = ?)
                  SELECT
                    IFNULL((SELECT updated FROM bucket), 0) updated,
                    IFNULL((SELECT value FROM bucket), 0) value
                );
                """, (key, now, increment, now, refill, max_hits, key))

        with stats.DBLatencyHistorgram.labels(query='select_limit').time():
            cursor.execute('SELECT value FROM buckets WHERE key = ?', (key,))
            return max(0, (cursor.fetchone()[0] - capacity) / float(refill))
