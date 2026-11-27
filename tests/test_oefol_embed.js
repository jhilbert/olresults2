const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const site = path.join(__dirname, '../site');
test('ÖFOL embed uses the shared archive, controls, and its own sticky viewport', () => {
  const html = fs.readFileSync(path.join(site,'embed/oefol/index.html'),'utf8');
  const original = fs.readFileSync(path.join(site,'oefol/index.html'),'utf8');
  const css = fs.readFileSync(path.join(site,'embed/oefol/embed.css'),'utf8');
  const js = fs.readFileSync(path.join(site,'oefol/meistertafel.js'),'utf8');
  for (const id of ['data-notice','data-notice-close','year','discipline','format','title','category','club','search','filter-toggle','filter-grid','reset','rows','table-head','error','empty','more','load-sentinel','filter-summary']) {
    assert.match(original, new RegExp(`id="${id}"`));
    assert.match(html, new RegExp(`id="${id}"`));
  }
  for (const mode of ['awards','people','clubs']) assert.match(html, new RegExp(`data-view="${mode}"`));
  assert.match(html, /data-source-base="\.\.\/\.\.\/oefol\/"/);
  assert.match(js, /document\.body\.dataset\.sourceBase/);
  assert.match(js, /querySelector\('\.masthead'\)\?\./);
  assert.doesNotMatch(html, /<header\b|<footer\b/);
  assert.match(css, /\.champions-embed \.sticky-header\s*\{\s*top:\s*0/);
  assert.ok(fs.existsSync(path.join(site,'oefol/meistertafel.json')));
});
