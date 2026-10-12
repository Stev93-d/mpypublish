"""Deploy MicroPython apps into two slots on the board, with trial boot and rollback."""

__version__ = "0.1.0"


class Error(Exception):
    """The command cannot go on: the message says why"""
