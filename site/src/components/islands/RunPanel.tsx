// RunPanel: the home hero's "claim vs re-verify" moment (plan §3.3, signature moment 1).
//
// It is an ILLUSTRATION of a run, not a capture: a full agent run needs engine
// credentials the site build never has. Every row is the format the code prints;
// the `path:line` it was assembled from is beside it. The example is the task
// examples/skep-demo/README.md suggests, on a project that pins claude_code as
// its engine, with the verify command `skep project setup` printed for it.
//
// M3: the server HTML is the finished panel. M4: every row is always in the DOM;
// hydration only moves a current-step cursor (`data-state`) and lets the one
// verdict resolve. Reduced motion and no-JS keep the finished panel.
import { useEffect, useRef, useState } from 'react';
import DecryptedText from '../reactbits/DecryptedText.tsx';
import './RunPanel.css';

// A UUIDv7, the shape src/skep/supervisor/ids.py:10 mints.
const TASK_ID = '01a1f0f8-11e8-7e3d-933f-58b406b184a9';
const VERIFY_COMMAND = 'python -m unittest discover -s tests';
// Rendered flag by flag so a line never breaks inside "--execution-mode".
const [RUN_COMMAND, ...RUN_FLAGS] =
  'skep run ~/code/skep-demo "add a /health route" --execution-mode sandbox'.split(' --');

// The cursor visits these blocks in order, one --dur-4 (900ms) each.
const DISPATCH = 0;
const CLAIM = 1;
const RERUN = 2;
const VERDICT = 3;
const APPROVAL = 4;
const STEP_MS = 900;

type Phase = 'rest' | 'playing' | 'done';

export default function RunPanel() {
  const root = useRef<HTMLElement>(null);
  const [phase, setPhase] = useState<Phase>('rest');
  const [step, setStep] = useState(DISPATCH);

  // Play once, when the panel is on screen (on a phone it sits below the hero text).
  useEffect(() => {
    const node = root.current;
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
  }, []);

  useEffect(() => {
    if (phase !== 'playing') return;
    const timer = setTimeout(() => {
      if (step === APPROVAL) setPhase('done');
      else setStep(step + 1);
    }, STEP_MS);
    return () => clearTimeout(timer);
  }, [phase, step]);

  const stateOf = (block: number) =>
    phase !== 'playing' ? undefined : block < step ? 'done' : block === step ? 'active' : 'pending';

  // DecryptedText starts hidden for 'click' and 'view' and plays on 'view': at rest
  // it shows the resolved text, while its row is pending it holds the scramble, and
  // it resolves once when the cursor reaches the verdict.
  const verdictTrigger = phase === 'rest' ? 'hover' : phase === 'playing' && step < VERDICT ? 'click' : 'view';

  return (
    <figure className="panel runpanel" ref={root} aria-label="Illustration of one skep run">
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

      <div className="rp-body">
        {/* src/skep/supervisor/cli_cmds.py:971, :1013 */}
        <div className="rp-block" data-state={stateOf(DISPATCH)}>
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
          <section className="rp-col rp-block" data-state={stateOf(CLAIM)}>
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
            <h3 className="rp-label t-micro">
              <span className="step-no">2</span>skep re-runs your command
            </h3>
            <dl className="rp-rows">
              {/* src/skep/supervisor/cli_cmds.py:1341 */}
              <div className="rp-block" data-state={stateOf(RERUN)}>
                <dt>re-ran</dt>
                <dd>['{VERIFY_COMMAND}'] → exit [0]</dd>
              </div>
              {/* src/skep/supervisor/cli_cmds.py:1337; the detail is src/skep/supervisor/reverify.py:372 and :491-499 */}
              <div className="rp-block" data-state={stateOf(VERDICT)}>
                <dt>re-verify:</dt>
                <dd>
                  passed [
                  <DecryptedText
                    text="confirmed"
                    animateOn={verdictTrigger}
                    sequential
                    speed={40}
                    className="rp-verdict"
                    encryptedClassName="rp-verdict rp-verdict-pending"
                  />
                  ] (G10):{' '}
                  <span className="rp-dim">
                    re-ran clean: all exit 0 [command from the project's pinned verify_command]
                  </span>
                </dd>
              </div>
            </dl>
          </section>
        </div>

        <section className="rp-approval rp-block" data-state={stateOf(APPROVAL)}>
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
      </div>

      <figcaption className="panel-foot">
        Illustration, not a captured session: the rows are the lines skep prints for a run like this one.
      </figcaption>
    </figure>
  );
}
