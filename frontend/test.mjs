import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const html = await readFile(new URL('./index.html', import.meta.url), 'utf8');
const js = await readFile(new URL('./app.js', import.meta.url), 'utf8');
const css = await readFile(new URL('./styles.css', import.meta.url), 'utf8');

test('all SmartSwap pages are present', () => {
  for (const page of ['monitor', 'experiment', 'evidence', 'reports']) {
    assert.match(html, new RegExp(`data-page="${page}"`));
    assert.match(html, new RegExp(`id="page-${page}"`));
  }
});

test('native experiment and comparison endpoints are wired', () => {
  for (const endpoint of ['/api/metrics', '/api/comparison', '/api/paired-comparison', '/api/cleanup', '/api/study', '/api/live']) assert.ok(js.includes(endpoint), `missing ${endpoint}`);
  assert.match(js, /c\.classification/);
  assert.doesNotMatch(js, /Math\.random/);
  for (const state of ['improved', 'regressed', 'approximately_unchanged', 'insufficient_data']) assert.match(css, new RegExp(state));
});
