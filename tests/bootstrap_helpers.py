"""Explicit readiness stand-in for existing synthetic storage protocol fixtures.

Those tests use Python executables and fabricated mathematical receipts. They do
not claim that Lean is ready. Real bootstrap and API admission tests must never
use this helper; they exercise the unchanged production readiness validator.
"""


def synthetic_execution(hub):
    hub._execution_environment = lambda connection: None
    return hub
