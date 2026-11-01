import test from 'node:test';
import assert from 'node:assert/strict';
import { toFacts, formatDuration, citationParts, interpretation } from '../src/lib/ask-format.ts';

test('summary keeps file dots and paths intact', () => {
  assert.deepEqual(toFacts('2 APIs call a service. See app.py:14 for evidence.'), { headline: '2 APIs call a service.', bullets: ['See app.py:14 for evidence.'] });
});
test('duration displays elapsed query time', () => {
  assert.equal(formatDuration(.042), '42 ms');
  assert.equal(formatDuration(1.25), '1.3 s');
});
test('endpoint labels retain method and service', () => {
  assert.deepEqual(citationParts({ node_id: 'endpoint::sample::catalog::GET:/items', label: 'GET /items', file: '', line: 0 }), { method: 'GET', path: '/items', service: 'catalog' });
});
test('interpretation follows explicit entity bindings', () => {
  assert.equal(interpretation([{kind:'service',id:'catalog'},{kind:'service',id:'storage'}]), 'catalog → storage');
});
test('interpretation follows semantic direction for reverse questions', () => {
  assert.equal(interpretation([{kind:'service',id:'directory',role:'target'},{kind:'service',id:'scheduler',role:'source'}]), 'scheduler → directory');
});
