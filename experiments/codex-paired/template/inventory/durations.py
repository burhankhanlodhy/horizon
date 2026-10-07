"""Parse human-friendly durations such as "1h30m", "45s" or "2d"."""


def parse_duration(text: str) -> int:
    """Return the number of seconds in a duration string.

    Units: d (days), h (hours), m (minutes), s (seconds). Units may be combined
    in that order ("1d2h", "1h30m15s"). Whitespace around the text is ignored.
    Raise ValueError for empty or malformed input.
    """
    raise NotImplementedError
