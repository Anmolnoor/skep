// RunPanel: the home hero's "claim vs re-verify" moment (plan §3.3, signature moment 1).
//
// It is an ILLUSTRATION of a run, not a capture: a full agent run needs engine
// credentials the site build never has. Every row is the format the code prints;
// the `path:line` it was assembled from is beside it. The example is the task
// examples/skep-demo/README.md suggests, on a project that pins claude_code as
// its engine, with the verify command `skep project setup` printed for it.
//
// M3: the server HTML is the finished panel. M4: every row is always in the DOM
// and the box never changes size. The run plays once, and only ever forwards:
// nothing on screen goes from a later state back to an earlier one.
//   - `wait` is what the server renders. Without JavaScript or under reduced
//     motion the CSS does nothing with it: the finished panel. With `html.js`
//     the CSS paints the steps the run has not reached as pending from the
//     first frame, and releases them by itself after WAIT_MS (the fallback for
//     an island that never hydrates). See RunPanel.css.
//   - hydration reads which of the two is on screen: still pending -> `armed`
//     (the same look, held by plain rules, no timer) and then `playing` once the
//     panel is in view; already released, or reduced motion -> `done`, no play.
//   - playing: a highlight glides from block to block (one persistent,
//     absolutely positioned element whose box is animated to the active block's
//     box), the status marks and the two links between the numbered steps
//     follow, the verdict resolves (DecryptedText) and lands with a spring, and
//     the approval card arms with a spring.
import { motion, useAnimate } from 'motion/react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import DecryptedText from '../reactbits/DecryptedText.tsx';
import './RunPanel.css';

// A UUIDv7, the shape src/skep/supervisor/ids.py:10 mints.
const TASK_ID = '01a1f0f8-11e8-7e3d-933f-58b406b184a9';
const VERIFY_COMMAND = 'python -m unittest discover -s tests';
// Rendered flag by flag so a line never breaks inside "--execution-mode".
const [RUN_COMMAND, ...RUN_FLAGS] =
  'skep run ~/code/skep-demo "add a /health route" --execution-mode sandbox'.split(' --');

// The highlight visits these blocks in order, one --dur-4 (900ms) each.
const DISPATCH = 0;
const CLAIM = 1;
const RERUN = 2;
const VERDICT = 3;
const APPROVAL = 4;
const STEP_MS = 900;
// "confirmed" resolves one character per VERDICT_CHAR_MS; the spring lands as the last one settles.
const VERDICT_CHAR_MS = 30;
const VERDICT_RESOLVE_MS = 300;
// The CSS fallback's delay: `5s` in RunPanel.css. Keep the two in step.
const WAIT_MS = 5000;

const GLIDE = { type: 'spring', visualDuration: 0.5, bounce: 0.16 } as const;
const PLACE = { duration: 0, opacity: { duration: 0.24 } } as const;
const LAND = { type: 'spring', visualDuration: 0.4, bounce: 0.55 } as const;

type Phase = 'wait' | 'armed' | 'playing' | 'done';
type State = 'pending' | 'active' | 'done';
type Box = { x: number; y: number; width: number; height: number };

// The block's box inside `root` (which is positioned, so it ends the offsetParent
// chain). Offsets ignore transforms, so a card mid-spring still measures true.
function boxIn(node: HTMLElement, root: HTMLElement): Box {
  let x = 0;
  let y = 0;
  for (let at: HTMLElement | null = node; at && at !== root; at = at.offsetParent as HTMLElement | null) {
    x += at.offsetLeft;
    y += at.offsetTop;
  }
  return { x, y, width: node.offsetWidth, height: node.offsetHeight };
}

// The status mark of a numbered step: pending, running, then done (or held, for
// the step that waits on the reader). All three are always in the DOM, stacked;
// the CSS shows one. Lucide, the kit's icon set (circle-dashed, circle-dot,
// circle-check, circle-pause), copied from @lucide/astro because an Astro
// component cannot render inside a React island.
const MARKS = {
  pending: (
    <>
      <path d="M10.1 2.182a10 10 0 0 1 3.8 0" />
      <path d="M13.9 21.818a10 10 0 0 1-3.8 0" />
      <path d="M17.609 3.721a10 10 0 0 1 2.69 2.7" />
      <path d="M2.182 13.9a10 10 0 0 1 0-3.8" />
      <path d="M20.279 17.609a10 10 0 0 1-2.7 2.69" />
      <path d="M21.818 10.1a10 10 0 0 1 0 3.8" />
      <path d="M3.721 6.391a10 10 0 0 1 2.7-2.69" />
      <path d="M6.391 20.279a10 10 0 0 1-2.69-2.7" />
    </>
  ),
  active: (
    <>
      <circle cx="12" cy="12" r="1" />
      <circle cx="12" cy="12" r="10" />
    </>
  ),
  done: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="m16 9-5.5 5.5L8 12" />
    </>
  ),
  held: (
    <>
      <circle cx="12" cy="12" r="10" />
      <line x1="10" x2="10" y1="15" y2="9" />
      <line x1="14" x2="14" y1="15" y2="9" />
    </>
  ),
};

function Mark({ rest = 'done' }: { rest?: 'done' | 'held' }) {
  return (
    <span className="rp-mark" aria-hidden="true">
      {(['pending', 'active', rest] as const).map((name) => (
        <svg
          key={name}
          className={`rp-mark-${name}`}
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          {MARKS[name]}
        </svg>
      ))}
    </span>
  );
}

export default function RunPanel() {
  const [scope, animate] = useAnimate<HTMLElement>();
  const body = useRef<HTMLDivElement>(null);
  const blocks = useRef<(HTMLElement | null)[]>([]);
  const placed = useRef(false);
  const [phase, setPhase] = useState<Phase>('wait');
  const [step, setStep] = useState(DISPATCH);
  const [box, setBox] = useState<Box | null>(null);
  const playing = phase === 'playing';

  // Hydration, before the next paint: take over from the CSS without changing a
  // pixel. While the fallback is still counting down, the pending steps are what
  // is on screen, so the run may start from them. Otherwise the finished panel
  // is on screen (reduced motion, or the fallback already released it) and stays.
  useLayoutEffect(() => {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const waiting =
      !reduced &&
      typeof IntersectionObserver !== 'undefined' &&
      scope.current
        ?.getAnimations?.({ subtree: true })
        .some(
          (animation) =>
            (animation as CSSAnimation).animationName?.startsWith('rp-wait') &&
            Number(animation.currentTime ?? 0) < WAIT_MS,
        );
    setPhase(waiting ? 'armed' : 'done');
  }, [scope]);

  // Play once, when the panel is on screen (on a phone it sits below the hero text).
  useEffect(() => {
    const node = scope.current;
    if (phase !== 'armed' || !node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        setPhase('playing');
      },
      { threshold: 0.35 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [phase, scope]);

  useEffect(() => {
    if (!playing) return;
    const timer = setTimeout(() => {
      if (step === APPROVAL) setPhase('done');
      else setStep(step + 1);
    }, STEP_MS);
    return () => clearTimeout(timer);
  }, [playing, step]);

  // The highlight follows the active block, and a resize while it plays.
  useLayoutEffect(() => {
    const root = body.current;
    if (!playing || !root) {
      setBox(null);
      placed.current = false;
      return;
    }
    const measure = () => {
      const block = blocks.current[step];
      if (block) setBox(boxIn(block, root));
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(root);
    return () => observer?.disconnect();
  }, [playing, step]);
  useEffect(() => {
    if (box) placed.current = true;
  }, [box]);

  // The two springs: the verdict lands, the approval card arms.
  useEffect(() => {
    if (!playing) return;
    if (step === VERDICT) {
      const timer = setTimeout(() => {
        animate('.rp-stamp', { scale: [1.4, 1] }, LAND);
        animate('.rp-ring', { opacity: [0.7, 0], scale: [0.6, 1.9] }, { duration: 0.6, ease: 'easeOut' });
      }, VERDICT_RESOLVE_MS);
      return () => clearTimeout(timer);
    }
    if (step === APPROVAL) {
      animate('.rp-approval', { scale: [0.97, 1] }, LAND);
      animate('.rp-approval .chip', { scale: [0.5, 1] }, { ...LAND, delay: 0.12 });
    }
  }, [playing, step, animate]);

  const replay = () => {
    setStep(DISPATCH);
    setPhase('playing');
  };

  // Before the run starts only the command is there; the rest is still to come.
  const stateOf = (index: number): State =>
    phase === 'done'
      ? 'done'
      : !playing
        ? index === DISPATCH
          ? 'done'
          : 'pending'
        : index < step
          ? 'done'
          : index === step
            ? 'active'
            : 'pending';
  // A numbered step that spans more than one block.
  const stepOf = (first: number, last = first): State =>
    stateOf(last) === 'done' ? 'done' : stateOf(first) === 'pending' ? 'pending' : 'active';
  const block = (index: number) => ({
    ref: (node: HTMLElement | null) => {
      blocks.current[index] = node;
    },
    'data-state': stateOf(index),
  });

  // DecryptedText shows the resolved text for 'hover' and plays once for 'view'.
  // Until the run reaches the verdict its chip is an empty slot (CSS), so the
  // only scramble a reader sees is the short one that resolves it.
  const verdictTrigger = playing && step >= VERDICT ? 'view' : 'hover';

  return (
    <figure
      className="panel runpanel"
      ref={scope}
      data-run={phase}
      aria-label="Illustration of one skep run"
      data-pagefind-ignore
    >
      <div className="panel-head">
        <div className="panel-title">
          <span className="term-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>claim vs re-verify</span>
        </div>
        <span className="chip chip-limit">illustration of a run</span>
      </div>

      <div className="rp-body" ref={body}>
        {/* src/skep/supervisor/cli_cmds.py:971, :1013 */}
        <div className="rp-block rp-dispatch" {...block(DISPATCH)}>
          <p className="rp-cmd">
            {RUN_COMMAND}
            {RUN_FLAGS.map((flag) => (
              <span key={flag} className="rp-nb">
                {' '}
                --{flag}
              </span>
            ))}
          </p>
          <p className="rp-dim">task {TASK_ID}</p>
        </div>

        <div className="rp-cols">
          <div className="rp-col rp-step rp-block" data-step={stepOf(CLAIM)} {...block(CLAIM)}>
            <p className="rp-label">
              <span className="step-no rp-step-claim">1</span>
              <span>The worker's claim</span>
              <Mark />
            </p>
            <dl className="rp-rows">
              {/* src/skep/supervisor/cli_cmds.py:1304 */}
              <div>
                <dt>state:</dt>
                <dd>completed</dd>
              </div>
              {/* src/skep/supervisor/cli_cmds.py:1305-1306; the detail is src/skep/workers/cli_adapter.py:349 */}
              <div>
                <dt>verification:</dt>
                <dd>
                  passed <span className="rp-dim">(git diff <span className="rp-nb">--check</span> passed)</span>
                </dd>
              </div>
              {/* src/skep/supervisor/cli_cmds.py:1351; the text is src/skep/workers/cli_adapter.py:350 */}
              <div>
                <dt>summary:</dt>
                <dd>claude-code produced a verified patch.</dd>
              </div>
            </dl>
          </div>

          <div className="rp-col rp-step rp-verify" data-step={stepOf(RERUN, VERDICT)}>
            {/* The progress line: step 1 to step 2, then step 2 to step 3. Decoration. */}
            <span className="rp-link rp-link-claim" data-state={stateOf(RERUN)} aria-hidden="true">
              <span />
            </span>
            <span className="rp-link rp-link-approve" data-state={stateOf(APPROVAL)} aria-hidden="true">
              <span />
            </span>
            <p className="rp-label rp-block" data-state={stateOf(RERUN)}>
              <span className="step-no">2</span>
              <span>skep re-runs your command</span>
              <Mark />
            </p>
            <dl className="rp-rows">
              {/* src/skep/supervisor/cli_cmds.py:1341 */}
              <div className="rp-block rp-row" {...block(RERUN)}>
                <dt>re-ran</dt>
                <dd>['{VERIFY_COMMAND}'] → exit [0]</dd>
              </div>
              {/* src/skep/supervisor/cli_cmds.py:1337; the detail is src/skep/supervisor/reverify.py:372 and :491-499 */}
              <div className="rp-block rp-row" {...block(VERDICT)}>
                <dt>re-verify:</dt>
                <dd>
                  passed{' '}
                  <span className="rp-stamp">
                    <span className="rp-ring" aria-hidden="true" />
                    {/* The kit's Chip (`ok`), as classes: an Astro component cannot render in the island. */}
                    <span className="chip chip-ok rp-verdict">
                      [
                      <DecryptedText text="confirmed" animateOn={verdictTrigger} sequential speed={VERDICT_CHAR_MS} />]
                    </span>
                  </span>{' '}
                  (G10):{' '}
                  <span className="rp-dim">
                    re-ran clean: all exit 0 [command from the project's pinned verify_command]
                  </span>
                </dd>
              </div>
            </dl>
          </div>
        </div>

        <div className="rp-approval rp-step rp-block" data-step={stepOf(APPROVAL)} {...block(APPROVAL)}>
          <p className="rp-label">
            <span className="step-no rp-step-approve">3</span>
            <span>Held for you</span>
            <span className="chip chip-limit">awaiting approval</span>
            <Mark rest="held" />
          </p>
          {/* src/skep/supervisor/cli_cmds.py:1370-1371 */}
          <p className="rp-next">
            <span className="rp-dim">next:</span> skep review &lt;task_id&gt; <span className="rp-nb">--approve</span>{' '}
            <span className="rp-dim"># applies the patch on branch skep/&lt;task_id&gt;</span>
          </p>
          <p className="rp-dim caret">main untouched </p>
        </div>

        {/* The highlight. Always in the DOM, invisible at rest, on top and click-through. */}
        <motion.span
          className="rp-glide"
          aria-hidden="true"
          initial={false}
          animate={box ? { ...box, opacity: 1 } : { opacity: 0 }}
          transition={placed.current ? GLIDE : PLACE}
        />
      </div>

      <figcaption className="panel-foot rp-foot">
        <span>Illustration, not a captured session: the rows are the lines skep prints for a run like this one.</span>
        {/* Needs JS, and has nothing to replay under reduced motion: the CSS hides it in both cases. */}
        <button
          type="button"
          className="copy-btn rp-replay no-js-hide"
          onClick={replay}
          disabled={phase !== 'done'}
          aria-label="Replay the illustration"
        >
          <svg
            aria-hidden="true"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
            <path d="M3 3v5h5" />
          </svg>
          replay
        </button>
      </figcaption>
    </figure>
  );
}
