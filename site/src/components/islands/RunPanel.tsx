// RunPanel: the home hero's "claim vs re-verify" moment (plan §3.3, signature moment 1).
//
// It is an ILLUSTRATION of a run, not a capture: a full agent run needs engine
// credentials the site build never has. Every row is the format the code prints;
// the `path:line` it was assembled from is beside it. The example is the task
// examples/skep-demo/README.md suggests, on a project that pins claude_code as
// its engine, with the verify command `skep project setup` printed for it.
//
// M3: the server HTML is the finished panel. M4: every row is always in the DOM
// and the box never changes size. Hydration plays the run once, with motion:
//   - a highlight glides from block to block (one persistent, absolutely
//     positioned element whose box is animated to the active block's box);
//   - the two links between the numbered blocks fill as the run passes them;
//   - the verdict resolves (DecryptedText) and lands with a spring;
//   - the approval card arms with a spring.
// Reduced motion and no-JS keep the finished panel and never play.
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
// "confirmed" resolves one character per 40ms; the spring lands as the last one settles.
const VERDICT_RESOLVE_MS = 400;

const GLIDE = { type: 'spring', visualDuration: 0.5, bounce: 0.16 } as const;
const PLACE = { duration: 0, opacity: { duration: 0.24 } } as const;
const LINK = { duration: 0.48, ease: [0.16, 1, 0.3, 1] } as const;
const LAND = { type: 'spring', visualDuration: 0.4, bounce: 0.55 } as const;

type Phase = 'rest' | 'playing' | 'done';
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

export default function RunPanel() {
  const [scope, animate] = useAnimate<HTMLElement>();
  const body = useRef<HTMLDivElement>(null);
  const blocks = useRef<(HTMLElement | null)[]>([]);
  const placed = useRef(false);
  const [phase, setPhase] = useState<Phase>('rest');
  const [step, setStep] = useState(DISPATCH);
  const [box, setBox] = useState<Box | null>(null);
  const playing = phase === 'playing';

  // Play once, when the panel is on screen (on a phone it sits below the hero text).
  useEffect(() => {
    const node = scope.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
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
  }, [scope]);

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

  const stateOf = (block: number) => (!playing ? undefined : block < step ? 'done' : block === step ? 'active' : 'pending');
  const block = (index: number) => ({
    ref: (node: HTMLElement | null) => {
      blocks.current[index] = node;
    },
    'data-state': stateOf(index),
  });
  const linkScale = (reached: number) => ({ scale: !playing || step >= reached ? 1 : 0 });

  // DecryptedText starts hidden for 'click' and 'view' and plays on 'view': at rest
  // it shows the resolved text, while its row is pending it holds the scramble, and
  // it resolves once when the highlight reaches the verdict.
  const verdictTrigger = phase === 'rest' ? 'hover' : playing && step < VERDICT ? 'click' : 'view';

  return (
    <figure className="panel runpanel" ref={scope} aria-label="Illustration of one skep run">
      <div className="panel-head">
        <div className="panel-title">
          <span className="term-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>claim vs re-verify</span>
        </div>
        <div className="rp-head-end">
          {/* Needs JS, and has nothing to replay under reduced motion: the CSS hides it in both cases. */}
          <button
            type="button"
            className="copy-btn rp-replay no-js-hide"
            onClick={replay}
            disabled={playing}
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
          <span className="chip chip-limit">illustration of a run</span>
        </div>
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
          <section className="rp-col rp-block" {...block(CLAIM)}>
            <h3 className="rp-label t-micro">
              <span className="step-no rp-step-claim">1</span>The worker's claim
            </h3>
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
          </section>

          <section className="rp-col rp-verify">
            {/* The progress line: block 1 to block 2, then block 2 to block 3. Decoration. */}
            <span className="rp-link rp-link-claim" aria-hidden="true">
              <motion.span initial={false} animate={linkScale(RERUN)} transition={LINK} />
            </span>
            <span className="rp-link rp-link-approve" aria-hidden="true">
              <motion.span initial={false} animate={linkScale(APPROVAL)} transition={LINK} />
            </span>
            <h3 className="rp-label rp-block t-micro" data-state={stateOf(RERUN)}>
              <span className="step-no">2</span>skep re-runs your command
            </h3>
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
                  passed [
                  <span className="rp-stamp">
                    <span className="rp-ring" aria-hidden="true" />
                    <DecryptedText
                      text="confirmed"
                      animateOn={verdictTrigger}
                      sequential
                      speed={40}
                      className="rp-verdict"
                      encryptedClassName="rp-verdict rp-verdict-pending"
                    />
                  </span>
                  ] (G10):{' '}
                  <span className="rp-dim">
                    re-ran clean: all exit 0 [command from the project's pinned verify_command]
                  </span>
                </dd>
              </div>
            </dl>
          </section>
        </div>

        <section className="rp-approval rp-block" {...block(APPROVAL)}>
          <h3 className="rp-label t-micro">
            <span className="step-no rp-step-approve">3</span>Held for you
            <span className="chip chip-limit">awaiting approval</span>
          </h3>
          {/* src/skep/supervisor/cli_cmds.py:1370-1371 */}
          <p className="rp-next">
            <span className="rp-dim">next:</span> skep review &lt;task_id&gt; <span className="rp-nb">--approve</span>{' '}
            <span className="rp-dim"># applies the patch on branch skep/&lt;task_id&gt;</span>
          </p>
          <p className="rp-dim caret">main untouched </p>
        </section>

        {/* The highlight. Always in the DOM, invisible at rest, on top and click-through. */}
        <motion.span
          className="rp-glide"
          aria-hidden="true"
          initial={false}
          animate={box ? { ...box, opacity: 1 } : { opacity: 0 }}
          transition={placed.current ? GLIDE : PLACE}
        />
      </div>

      <figcaption className="panel-foot">
        Illustration, not a captured session: the rows are the lines skep prints for a run like this one.
      </figcaption>
    </figure>
  );
}
