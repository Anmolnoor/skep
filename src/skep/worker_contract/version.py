"""Contract versioning and skew detection for first-party Skep workers."""

from __future__ import annotations

CONTRACT_VERSION = "0.3.5"

# v39-F3: the one declaration of what this tree's supervisor and first-party
# workers accept. Six modules used to carry their own copy of this literal;
# a range that drifts per-file is a skew bug waiting for a release to happen.
SUPPORTED_CONTRACT_RANGE = ">=0.1,<0.4"

_OPS = ("<=", ">=", "==", "<", ">")


class ContractSkewError(Exception):
    """Raised when a contract version falls outside a supported range."""

    def __init__(self, version: str, supported_range: str) -> None:
        self.version = version
        self.supported_range = supported_range
        super().__init__(
            f"contract version skew: got {version!r}, supported range is {supported_range!r}. "
            "Remediation: upgrade the Skep worker or supervisor so their supported "
            "contract ranges overlap, then re-dispatch the task."
        )


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.strip().split(".")
    if not parts or len(parts) > 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid semver string: {version!r}")
    nums = [int(part) for part in parts]
    nums += [0] * (3 - len(nums))
    return (nums[0], nums[1], nums[2])


def _holds(version: tuple[int, int, int], op: str, bound: tuple[int, int, int]) -> bool:
    if op == ">=":
        return version >= bound
    if op == "<=":
        return version <= bound
    if op == "==":
        return version == bound
    if op == ">":
        return version > bound
    return version < bound


def check_supported(version: str, supported_range: str) -> ContractSkewError | None:
    """Return None when `version` satisfies `supported_range`, else skew details."""

    parsed = _parse_version(version)
    constraints = [
        constraint.strip() for constraint in supported_range.split(",") if constraint.strip()
    ]
    if not constraints:
        raise ValueError(f"empty supported range: {supported_range!r}")
    for constraint in constraints:
        for op in _OPS:
            if constraint.startswith(op):
                bound = _parse_version(constraint[len(op) :])
                if not _holds(parsed, op, bound):
                    return ContractSkewError(version, supported_range)
                break
        else:
            raise ValueError(f"invalid range constraint: {constraint!r}")
    return None
