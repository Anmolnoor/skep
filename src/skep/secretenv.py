"""v135 (ADR 0069 2.5, review S1): the last step inside a strict MCP server's
wall. It reads this launch's own snapshot of the server's listed secrets
(review r2 N2; bound in read-only, past the secret floor's mask, for this
one wall), puts the listed names in the environment, and execs the server.
So the launcher — bubblewrap or sandbox-exec, the netshim, the Landlock
step — never holds a value in its environment or its argv, and nothing
outside the wall can read one there.

Usage: ``python -E -P -m skep.secretenv <snapshot file> NAME... -- argv...``.
It never prints a value.
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--" not in args or args.index("--") == 0:
        print("secretenv: usage: <secrets file> NAME... -- argv...", file=sys.stderr)
        return 2
    split = args.index("--")
    (path, *names), command = args[:split], args[split + 1 :]
    if not command:
        print("secretenv: no server command given", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as handle:
            values = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"secretenv: the secrets file cannot be read in the wall ({exc})", file=sys.stderr)
        return 2
    for name in names:
        value = values.get(name) if isinstance(values, dict) else None
        if not isinstance(value, str) or not value:
            print(f"secretenv: secret {name} has no value set; not starting", file=sys.stderr)
            return 2
        if name in os.environ:  # the wall's own names win (2.5)
            print(f"secretenv: {name} is a name the wall sets; not starting", file=sys.stderr)
            return 2
        os.environ[name] = value
    os.execvp(command[0], command)
    return 127  # unreachable: execvp replaces the process or raises


if __name__ == "__main__":
    sys.exit(main())
