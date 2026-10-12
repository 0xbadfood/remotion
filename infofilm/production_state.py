"""Intentional, resumable production pauses are distinct from failures."""

class ProductionPaused(RuntimeError):
    def __init__(self, status, message, details=None):
        if status not in {'waiting_for_assets', 'waiting_for_capabilities'}:
            raise ValueError('Unknown production pause')
        self.status, self.details = status, details or {}
        super().__init__(message)
