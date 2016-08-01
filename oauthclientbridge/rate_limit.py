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


def check(key):
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
            'SELECT updated, value FROM buckets WHERE key = ?', (key,))
        row = cursor.fetchone()

        if row:
            updated, value = row
        else:
            updated, value = now, 0

        # TODO: add a penalty for being over cap?
        # TODO: this is probably racy.

        # 1. Reduce by amount we should have refilled since last update.
        value -= float(now - updated) / app.config['OAUTH_BUCKET_REFILL_RATE']
        # 2. Update to 0 if bucket is "full" or value + 1 to account for hit.
        value = max(0, value + 1)
        # 3. Limit how much over you can go.
        value = min(value, app.config['OAUTH_BUCKET_MAX_HITS'])

        cursor.execute(  # Insert/replace the bucket we just hit.
            'INSERT OR REPLACE INTO buckets '
            '(key, updated, value) VALUES (?, ?, ?)',
            (key, now, value))

    return value > app.config['OAUTH_BUCKET_CAPACITY']
