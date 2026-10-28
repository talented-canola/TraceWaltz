"""Timeout values from an application's configuration."""


def parse_timeout(value, default=30):
    return int(value or default)
