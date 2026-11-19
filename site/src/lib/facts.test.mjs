import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  adrCount,
  contractVersion,
  engineNames,
  homeNumbers,
  invariantCount,
  projectVersion,
  reverifyOutcomes,
  sandboxBackends,
} from './facts.mjs';

test('the project version is read from the [project] table only', () => {
  const pyproject = '[tool.other]\nversion = "0.0.1"\n\n[project]\nname = "skep"\nversion = "9.8.7"\n\n[x]\nversion = "5"\n';
  assert.equal(projectVersion(pyproject), '9.8.7');
  assert.equal(projectVersion('[project]\nversion = "9.8.7"'), '9.8.7');
});

test('a missing version fails the build instead of printing "undefined"', () => {
  assert.throws(() => projectVersion('[tool.x]\nversion = "9.8.7"\n'), /no \[project\] version/);
  assert.throws(() => contractVersion('SUPPORTED = ">=0.1"\n'), /no CONTRACT_VERSION/);
});

test('the worker contract constant is read from its module', () => {
  assert.equal(contractVersion('"""doc"""\n\nCONTRACT_VERSION = "9.9.9"\nOTHER = "1"\n'), '9.9.9');
});

test('engine ids are the keys of CODING_ENGINES, constants resolved, in source order', () => {
  const source = [
    'BUILTIN_ENGINE = "builtin"',
    'CODING_ENGINES: dict[str, CodingEngine] = {',
    '    BUILTIN_ENGINE: CodingEngine(',
    '        name=BUILTIN_ENGINE,',
    '    ),',
    '    "claude_code": CodingEngine(',
    '        name="claude_code",',
    '    ),',
    '}',
    'OTHER = {"x": CodingEngine()}',
  ].join('\n');
  assert.deepEqual(engineNames(source), ['builtin', 'claude_code']);
});

test('an engine registry the reader cannot fully parse fails the build', () => {
  assert.throws(() => engineNames('ENGINES = {}\n'), /no CODING_ENGINES dict/);
  // an entry in a shape the reader does not know must not be silently skipped
  const reshaped = 'CODING_ENGINES = {\n    "a": CodingEngine(\n    ),\n    **extra(CodingEngine()),\n}\n';
  assert.throws(() => engineNames(reshaped), /read 1 engine ids from 2 CodingEngine entries/);
  const noConstant = 'CODING_ENGINES = {\n    BUILTIN_ENGINE: CodingEngine(\n    ),\n}\n';
  assert.throws(() => engineNames(noConstant), /BUILTIN_ENGINE, which is not defined/);
});

test('re-verification outcomes are the distinct literals passed to ReverifyOutcome', () => {
  const source = [
    'class ReverifyOutcome:',
    '    outcome: str',
    'def f() -> ReverifyOutcome:',
    '    return ReverifyOutcome("unavailable", [], [], "x")',
    '    return ReverifyOutcome(',
    '        "passed", commands, codes, "ok"',
    '    )',
    '    return ReverifyOutcome("failed", [], [], "y")',
    '    return ReverifyOutcome("unavailable", [], [], "z")',
  ].join('\n');
  assert.deepEqual(reverifyOutcomes(source), ['unavailable', 'passed', 'failed']);
  assert.throws(() => reverifyOutcomes('x = ReverifyOutcome(kind, [], [], "")'), /0 with a literal outcome/);
  assert.throws(() => reverifyOutcomes('x = ReverifyOutcome("unavailable", [], [], "")'), /no ReverifyOutcome\("passed"/);
});

test('sandbox backends are the ones sandbox.py can report as available', () => {
  const source = [
    'PODMAN = "podman"',
    'return SandboxAvailability(False, "probe_failed", str(exc), backend=PODMAN)',
    'return SandboxAvailability(True, backend="seatbelt")',
    'return SandboxAvailability(True, backend=PODMAN)',
    'return SandboxAvailability(True, backend="seatbelt")',
  ].join('\n');
  assert.deepEqual(sandboxBackends(source), ['seatbelt', 'podman']);
  assert.throws(() => sandboxBackends('x = 1'), /no SandboxAvailability\(True/);
  assert.throws(() => sandboxBackends('SandboxAvailability(True, backend=GONE)'), /GONE is not defined/);
});

test('invariants are counted only when they number 1..N', () => {
  assert.equal(invariantCount('**I1. One.** text\n\nsee **I2** inline\n\n**I2. Two.** text\n'), 2);
  assert.throws(() => invariantCount('**I1. One.**\n\n**I3. Three.**\n'), /read \[1, 3\]/);
  assert.throws(() => invariantCount('# nothing'), /expected I1\.\.IN/);
});

test('ADRs are the NNNN-title.md files, and a shared number fails', () => {
  assert.equal(adrCount(['README.md', '0001-a.md', '0002-b.md', 'notes.txt']), 2);
  assert.throws(() => adrCount(['README.md']), /no NNNN-title\.md/);
  assert.throws(() => adrCount(['0001-a.md', '0001-b.md']), /share a number/);
});

test('the readers parse the repository as it is today', () => {
  // The test file is not bundled, so its URL is where it lives: site/src/lib/.
  const root = new URL('../../../', import.meta.url);
  const numbers = homeNumbers({
    read: (path) => readFileSync(new URL(path, root), 'utf8'),
    list: (dir) => readdirSync(new URL(`${dir}/`, root)),
  });
  // Shapes, not counts: a new engine or record must not need this test edited.
  assert.ok(numbers.engines.includes('builtin') && numbers.engines.includes('claude_code'));
  assert.ok(numbers.outcomes.includes('not_applicable') && numbers.outcomes.includes('unavailable'));
  assert.ok(numbers.backends.includes('bubblewrap') && numbers.backends.includes('seatbelt'));
  assert.ok(numbers.invariants >= 13);
  assert.ok(numbers.adrs >= 50);
});
