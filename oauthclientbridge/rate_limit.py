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

    with db.cursor(name='rate_limit') as cursor:
        # 1. Empty buckets by 'time-since-last-update x refill-rate'.
        cursor.execute(
            'UPDATE buckets SET value = MAX(0, value - (? - updated) * ?), '
            'updated = ? WHERE key = ?', (now, refill, now, key))
        # 2. Increment value up to the max-hits level.
        cursor.execute(
            'INSERT OR REPLACE INTO buckets (key, updated, value) VALUES '
            '(?, ?, MIN((SELECT value FROM buckets WHERE key = ?) + ?, ?))',
            (key, now, key, increment, max_hits))
        # 3. Check what the value actually is now.
        cursor.execute('SELECT value FROM buckets WHERE key = ?', (key,))

        # 4. Calculate how many seconds until the bucket would be below capacity.
        return max(0, (cursor.fetchone()[0] - capacity) / float(refill))
