import assert from 'node:assert/strict';
import { test } from 'node:test';
import { contractVersion, projectVersion } from './facts.mjs';

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
