"""The ACP governor (ADR 0054): skep as a stdio proxy between an ACP client and agent.

Modules: ``proxy`` (transparent JSON-RPC line relay with claimable methods),
``mapper`` (ACP frames to policy probes) and ``govern`` (verdicts to ACP answers).
"""
