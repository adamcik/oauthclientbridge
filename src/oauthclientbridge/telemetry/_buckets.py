"""Shared histogram buckets inspired by https://linuxczar.net/blog/2016/12/31/prometheus-histograms/."""

TIME = (
    0.0001,
    0.00055,
    0.001,
    0.0028,
    0.0046,
    0.0064,
    0.0082,
    0.01,
    0.028,
    0.046,
    0.064,
    0.082,
    0.1,
    0.4,
    0.7,
    1.0,
    4.0,
    7.0,
    10.0,
    float("inf"),
)

BYTES = (
    8,
    22,
    36,
    50,
    64,
    176,
    288,
    400,
    512,
    1408,
    2304,
    3200,
    4096,
    11264,
    18432,
    25600,
    32768,
    90112,
    147456,
    204800,
    float("inf"),
)

TOKEN_GRANT_AGE = (
    3600,  # 1 hour
    21600,  # 6 hours
    86400,  # 1 day
    259200,  # 3 days
    604800,  # 7 days
    1209600,  # 14 days
    2592000,  # 30 days
    5184000,  # 60 days
    7776000,  # 90 days
    10368000,  # 120 days
    12960000,  # 150 days
    15552000,  # 180 days
    18144000,  # 210 days
    31536000,  # 365 days
    float("inf"),
)
