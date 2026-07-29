---
name: python-debugpy
description: interactive Python debugging via pdb/debugpy in a worker run
---

# Python debugging (pdb/debugpy)

Tools: run_code, dispatch_run, read_file

skep runs are non-interactive, so "attach a debugger" becomes "make the
program report its own state":

1. Cheap and often enough: `run_code` the failing snippet with
   strategic prints / `breakpoint()` replaced by
   `import traceback; traceback.print_stack()` at the suspect line.
2. Real codebase: `dispatch_run` briefed to run the failing test under
   `pytest --pdb -x` piped through `python -m pdb -c 'commands...'`
   style scripted commands, or to insert temporary instrumentation and
   REMOVE it before the patch (verify step: the instrumentation is gone
   and the suite passes).
3. debugpy specifically is for the user's OWN editor attach — give them
   the two lines (`import debugpy; debugpy.listen(5678)`) and which
   process to attach to (a `start_process`-launched server works).
