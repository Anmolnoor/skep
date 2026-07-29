"""skep worker implementations dispatched under the first-party worker contract.

These are *workers*, not supervisor code: the supervisor spawns them as
subprocesses with a restricted env + sandbox, matching the optional external
worker path. The boundary between worker and supervisor is still the contract.
"""
