const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/dr_filters.js'), 'utf8');
const key = 'prevention-tracker.dr.filters.v1';

function run(url, saved, query = '') {
  const events = {};
  const checkbox = { checked: false, addEventListener: (name, fn) => { events[name] = fn; } };
  const search = { value: query, form: { addEventListener: (name, fn) => { events.submit = fn; } },
    addEventListener: (name, fn) => { events.search = fn; } };
  const reset = { addEventListener: (name, fn) => { events.reset = fn; } };
  const storage = new Map(saved ? [[key, JSON.stringify(saved)]] : []);
  const navigation = [];
  const elements = { rememberDrFilters: checkbox, resetDrFilters: reset, q: search, half: { value: 'H2' } };
  vm.runInNewContext(source, {
    URL, console, document: { getElementById: id => elements[id] },
    localStorage: { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    window: { location: { href: url, replace: value => navigation.push(value), assign: value => navigation.push(value) } },
  });
  return { events, checkbox, search, storage, navigation };
}

test('remembers team, submitted search and action together', () => {
  const state = run('https://example.test/dr?half=H2&team=QA&action=evidence', null, '서버');
  state.checkbox.checked = true;
  state.events.change();
  state.search.value = '서버2';
  state.events.submit();
  const filters = JSON.parse(state.storage.get(key)).filters;
  assert.equal(filters.team, 'QA');
  assert.equal(filters.q, '서버2');
  assert.equal(filters.action, 'evidence');
});

test('restores team and search on bare entry without repeated navigation', () => {
  const saved = { enabled: true, filters: { team: 'QA', q: '서버', status: 'pending' } };
  const state = run('https://example.test/dr', saved);
  assert.equal(state.navigation.length, 1);
  const restored = new URL(state.navigation[0], 'https://example.test');
  assert.equal(restored.searchParams.get('team'), 'QA');
  assert.equal(restored.searchParams.get('q'), '서버');
  assert.equal(run(restored.href, saved, '서버').navigation.length, 0);
});

test('explicit URL overrides saved filters and reset clears everything', () => {
  const state = run('https://example.test/dr?team=New&q=new',
    { enabled: true, filters: { team: 'Old', q: 'old' } }, 'new');
  assert.equal(state.navigation.length, 0);
  assert.equal(JSON.parse(state.storage.get(key)).filters.team, 'New');
  state.events.reset();
  assert.equal(state.storage.has(key), false);
  assert.equal(state.navigation[0], '/dr?half=H2');
});
