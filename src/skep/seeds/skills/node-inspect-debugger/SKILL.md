---
name: node-inspect-debugger
description: debug Node.js via --inspect and scripted DevTools-protocol probes
---

# Node.js debugging (--inspect)

Tools: start_process, read_process_log, run_shell, dispatch_run

1. Launch the app with the inspector: `start_process` with
   `node --inspect=127.0.0.1:9229 app.js` (loopback only — never bind
   the inspector wide). `read_process_log` confirms the ws:// endpoint.
2. The user's editor/Chrome attaches to 9229 for interactive work; for
   scripted probes, `run_shell` a `node -e` snippet speaking the
   inspector protocol, or add temporary `console.trace()` /
   `process.on('unhandledRejection')` instrumentation via a dispatch
   (verify: instrumentation removed, tests green).
3. Crash-on-start bugs: `node --inspect-brk` pauses at line 1 so the
   user can attach before the crash.
4. `stop_process` the inspected server when the session ends.
