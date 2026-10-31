"""Test-only Provider lease doubles for isolated service-layer verifiers."""


class StaticProviderLease:
    def __init__(self, provider):
        self.provider = provider

    def __enter__(self):
        return self.provider

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    async def __aenter__(self):
        return self.provider

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


def lease_acquirer(getter):
    """Adapt an existing fake Provider getter to acquire_provider's contract."""
    return lambda engine_name: StaticProviderLease(getter(engine_name))
