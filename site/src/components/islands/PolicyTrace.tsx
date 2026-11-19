// PolicyTrace: the how-it-works page's "policy trace" (plan §3.3, signature moment 2).
//
// Every row is a field `skep policy why` printed. Captured 2026-11-18 (UTC
// timestamps in the output are 2026-11-19) on Linux, under a throwaway home:
//
//   export SKEP_HOME=$(mktemp -d -p ~/.cache)
//   git init a small repository at $SKEP_HOME/demo-repo (one commit)
//   skep policy approve --template builder --yes
//   skep project setup $SKEP_HOME/demo-repo --pack trusted_local_dev --no-seed-schedules
//   skep policy why demo-repo shell run "git push origin main"
//   skep policy why demo-repo shell run "npm i left-pad"
//   skep policy why demo-repo shell run "cat ~/.ssh/id_ed25519"
//   skep policy why demo-repo shell run "npm install left-pad"   (quoted in the npm note)
//
// `why` prints one JSON object: `baseline`, `floor` (only when a floor rule
// decided: src/skep/supervisor/serve/actions.py:2412), and two faces, `run`
// (the run policy a dispatch in that repo gets) and `operator` (the Queen's
// operator document), each with `decided_by` and `verdict`. The rows below are
// those fields in the order decide() reads them (src/skep/supervisor/policy_v3.py:14).
// The one display edit: the output expands ~ to the home directory; it is shown as ~.
//
// M3: the server HTML holds all three traces, finished. The radios switch
// between them in CSS (:has), so they work without JavaScript. M4: nothing
// mounts or unmounts; the traces share one grid cell, so the panel keeps the
// height of the tallest. Hydration only moves a current-row cursor and lets
// the verdict resolve, once per trace, and never under reduced motion.
import { useEffect, useRef, useState } from 'react';
import DecryptedText from '../reactbits/DecryptedText.tsx';
import './PolicyTrace.css';

type Verdict = 'allow' | 'ask' | 'deny';

interface Row {
  /** The JSON key in the output. */
  key: string;
  /** What skep printed for it. */
  printed: string;
  /** One line of explanation. */
  detail: string;
  chip?: { kind: Verdict | 'ok'; label: string };
}

interface Trace {
  id: string;
  command: string;
  rows: Row[];
  verdict: Verdict;
  /** The word in the JSON: the engine says require_approval, the tables and cards say ask. */
  printedVerdict: string;
  note: string;
}

const BASELINE: Row = {
  key: 'baseline',
  printed: 'builder approved 2026-11-19T00:51:49Z sha256 01db10ac4fc3',
  detail: 'The locked baseline matches its hash. Tampered or missing, every decision here would ask.',
  chip: { kind: 'ok', label: 'locked' },
};

const TRACES: Trace[] = [
  {
    id: 'push',
    command: 'git push origin main',
    rows: [
      {
        key: 'floor',
        printed:
          'remote git commands never run from chat — skep lands patches after approval; use push_branch/open_pr for the governed remote surface',
        detail: 'The floor is read first. No rule, grant, role or approval lifts it.',
        chip: { kind: 'deny', label: 'deny' },
      },
      BASELINE,
      {
        key: 'run',
        printed: 'decided_by: floor/floor.worker-git-hard-denies',
        detail: 'A worker dispatched in this repo. considered: [] (no project rule was consulted).',
        chip: { kind: 'deny', label: 'deny' },
      },
      {
        key: 'operator',
        printed: 'decided_by: floor/floor.worker-git-hard-denies',
        detail: 'The Queen, from chat. The same floor binds it.',
        chip: { kind: 'deny', label: 'deny' },
      },
    ],
    verdict: 'deny',
    printedVerdict: 'deny',
    note: 'A worker and the Queen get the same answer. skep itself pushes a landing branch only when you ask: skep review --approve --pr, skep pr open, or a push_branch card in chat.',
  },
  {
    id: 'npm',
    command: 'npm i left-pad',
    rows: [
      {
        key: 'floor',
        printed: '(no floor key in the output)',
        detail: 'No floor rule matches this command, so the layers below decide.',
      },
      BASELINE,
      {
        key: 'run',
        printed: 'decided_by: default/unmatched',
        detail: 'A worker dispatched in this repo. considered: [] (no rule names this command).',
        chip: { kind: 'ask', label: 'ask' },
      },
      {
        key: 'operator',
        printed: 'decided_by: template:builder/shell-learnable',
        detail: "The Queen, from chat: the builder template's catch-all for shell commands.",
        chip: { kind: 'ask', label: 'ask' },
      },
    ],
    verdict: 'ask',
    printedVerdict: 'require_approval',
    note: 'Unmatched means ask, so a person answers. Rules match the command as written: the builder template allows the prefix "npm install", and the same question for "npm install left-pad" printed allow on the run face, decided by template:builder/shell:npm install.',
  },
  {
    id: 'cat',
    command: 'cat ~/.ssh/id_ed25519',
    rows: [
      {
        key: 'floor',
        printed:
          '~/.ssh/id_ed25519 is under ~/.ssh — credential files are never readable by the Queen or a worker (the secret floor; no rule, grant or card lifts it)',
        detail: 'The floor reads the paths a shell line names, not only file reads.',
        chip: { kind: 'deny', label: 'deny' },
      },
      BASELINE,
      {
        key: 'run',
        printed: 'decided_by: floor/floor.secret-reads',
        detail: 'A worker dispatched in this repo. considered: [].',
        chip: { kind: 'deny', label: 'deny' },
      },
      {
        key: 'operator',
        printed: 'decided_by: floor/floor.secret-reads',
        detail: 'The Queen, from chat.',
        chip: { kind: 'deny', label: 'deny' },
      },
    ],
    verdict: 'deny',
    printedVerdict: 'deny',
    note: 'This is the policy answer to a command line. In a sandbox run the path is also masked on disk, which is what stops an external engine whose commands are never asked about.',
  },
];

const STEP_MS = 480; // --dur-3
const pick = (id: string) => `pt-pick-${id}`;

export default function PolicyTrace() {
  const root = useRef<HTMLElement>(null);
  const [active, setActive] = useState(0);
  // The cursor: a row index, rows.length for the verdict, null when nothing plays.
  const [step, setStep] = useState<number | null>(null);
  const [played, setPlayed] = useState<boolean[]>(() => TRACES.map(() => false));
  const playedRef = useRef(played);
  playedRef.current = played;

  // A trace plays the first time it is shown, and never under reduced motion.
  const play = (index: number) => {
    setActive(index);
    if (playedRef.current[index] || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setStep(null);
      return;
    }
    setPlayed((before) => before.map((done, at) => done || at === index));
    setStep(0);
  };

  // First play: when the panel is on screen, for whichever command is selected
  // (the reader may have picked one before hydration).
  useEffect(() => {
    const node = root.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        const checked = node.querySelector<HTMLInputElement>('input[name="policy-trace"]:checked');
        play(Math.max(0, TRACES.findIndex((trace) => trace.id === checked?.value)));
      },
      { threshold: 0.35 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (step === null) return;
    const last = TRACES[active].rows.length;
    const timer = setTimeout(() => setStep(step === last ? null : step + 1), STEP_MS);
    return () => clearTimeout(timer);
  }, [step, active]);

  const stateOf = (trace: number, row: number) =>
    trace !== active || step === null ? undefined : row < step ? 'done' : row === step ? 'active' : 'pending';

  // DecryptedText holds the scramble for 'click', resolves once for 'view' and
  // shows the final text for 'hover'. A trace that has not played rests on 'hover'.
  const trigger = (trace: number) =>
    trace === active && step !== null && step < TRACES[trace].rows.length
      ? 'click'
      : played[trace]
        ? 'view'
        : 'hover';

  return (
    <figure className="panel pt" ref={root} data-pagefind-ignore>
      <div className="panel-head">
        <div className="panel-title">
          <span>skep policy why</span>
        </div>
        <span className="chip chip-ok">captured output</span>
      </div>

      <div className="pt-pick" role="radiogroup" aria-labelledby="pt-pick-label">
        <p className="t-micro pt-pick-label" id="pt-pick-label">
          Pick a command
        </p>
        {TRACES.map((trace, index) => (
          <label className="pt-option" key={trace.id}>
            <input
              type="radio"
              name="policy-trace"
              id={pick(trace.id)}
              value={trace.id}
              defaultChecked={index === 0}
              onChange={() => play(index)}
            />
            <span>{trace.command}</span>
          </label>
        ))}
      </div>

      <div className="pt-stack">
        {TRACES.map((trace, index) => (
          <section className="pt-trace" data-trace={trace.id} key={trace.id} aria-label={`Decision for ${trace.command}`}>
            <p className="pt-cmd">skep policy why demo-repo shell run "{trace.command}"</p>
            <ol className="ledger-rows pt-rows">
              {trace.rows.map((row, at) => (
                <li key={row.key} data-state={stateOf(index, at)}>
                  <span className="ledger-when">{row.key}</span>
                  <span className="ledger-what">
                    {row.printed}
                    <small>{row.detail}</small>
                  </span>
                  {row.chip ? <span className={`chip chip-${row.chip.kind}`}>{row.chip.label}</span> : <span />}
                </li>
              ))}
            </ol>
            <p className="pt-verdict" data-state={stateOf(index, trace.rows.length)}>
              <span className="t-micro">verdict</span>
              <span className={`chip chip-${trace.verdict}`}>
                <DecryptedText
                  text={trace.verdict}
                  animateOn={trigger(index)}
                  sequential
                  speed={60}
                  characters="#*+=/<>"
                />
              </span>
              <code>"verdict": "{trace.printedVerdict}"</code>
            </p>
            <p className="pt-note">{trace.note}</p>
          </section>
        ))}
      </div>

      <figcaption className="panel-foot">
        captured: skep policy why, 2026-11-18, throwaway SKEP_HOME, builder baseline approved, project demo-repo; home
        directory shown as ~
      </figcaption>
    </figure>
  );
}
