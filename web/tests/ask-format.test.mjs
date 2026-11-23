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
  assert.deepEqual(citationParts({ node_id: 'endpoint::sample::catalog::GET:/items', label: 'GET /items', file: '', line: 0 }), { method: 'GET', path: '/items', service: 'catalog', repo: 'sample' });
});
test('interpretation follows explicit entity bindings', () => {
  assert.equal(interpretation([{kind:'service',id:'catalog'},{kind:'service',id:'storage'}]), 'catalog → storage');
});
test('interpretation follows semantic direction for reverse questions', () => {
  assert.equal(interpretation([{kind:'service',id:'directory',role:'target'},{kind:'service',id:'scheduler',role:'source'}]), 'scheduler → directory');
});

test('qualified citations and interpretation preserve repository identity', () => {
  assert.deepEqual(citationParts({node_id:'service::finance::ledger', label:'finance::ledger', file:'', line:0}),
    {method:'',path:'finance::ledger',service:'ledger',repo:'finance'});
  assert.equal(interpretation([{kind:'service',id:'ledger',repo_id:'orders',service_id:'ledger'},
    {kind:'service',id:'ledger',repo_id:'finance',service_id:'ledger',role:'target'}]), 'orders::ledger → finance::ledger');
});

test('external IDs do not invent repository ownership from an HTTP operation', () => {
  assert.deepEqual(citationParts({node_id:'external::GET:/missing',label:'GET /missing',file:'',line:0}),
    {method:'',path:'GET /missing',service:'',repo:''});
});
