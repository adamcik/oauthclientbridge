from oauthclientbridge.utils.bucket import Bucket


def test_bucket_consumes_token_on_admission() -> None:
    bucket = Bucket(capacity=1, refill_amount=0.25)

    assert bucket.consume() is True
    assert bucket.consume() is False


def test_bucket_add_refills_capacity() -> None:
    bucket = Bucket(capacity=1, refill_amount=0.25)

    assert bucket.consume() is True
    bucket.add(0.25)
    bucket.add(0.25)
    bucket.add(0.25)
    assert bucket.consume() is False

    bucket.add(0.25)
    assert bucket.consume() is True
