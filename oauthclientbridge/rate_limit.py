import hashlib
import time

from oauthclientbridge import app, db


def clean():
    now = time.time()
    with db.cursor() as cursor:
        cursor.execute('DELETE FROM buckets WHERE updated < ? AND '
                       'value - (? - updated) / ? <= 0',
                       (now, now, app.config['OAUTH_BUCKET_REFILL_RATE']))
        return cursor.rowcount


def check(key, increment=1):
    """Decide if the given key should be rate limited.

    Calls are allowed whenever the bucket is below capacity. Each hit fills the
    bucket by one. Buckets drain at a configurable rate, though refill only
    happens when the bucket gets a hit. There is a maximum bucket fill to avoid
    callers being locket out for too long.
    """
    if not app.config['OAUTH_RATE_LIMIT']:
        return False

    now = time.time()
    key = hashlib.sha256(key).hexdigest()

    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT OR REPLACE INTO buckets (key, updated, value)
            SELECT
               -- 1. Empty bucket by '(now - updated) / refill_rate'
               -- 2. Set to zero if we emptied too much.
               -- 3. Limit bucket fullness to max_hits
               ?, ?, MIN(? + MAX(0, value - ((? - updated) / ?)), ?)
            FROM (
              WITH bucket AS (SELECT * FROM buckets WHERE key = ?)
              SELECT
                IFNULL((SELECT updated FROM bucket), 0) updated,
                IFNULL((SELECT value FROM bucket), 0) value
            );
            """, (key, now, increment, now,
                  app.config['OAUTH_BUCKET_REFILL_RATE'],
                  app.config['OAUTH_BUCKET_MAX_HITS'], key))

        cursor.execute('SELECT value FROM buckets WHERE key = ?', (key,))
        return cursor.fetchone()[0] > app.config['OAUTH_BUCKET_CAPACITY']
