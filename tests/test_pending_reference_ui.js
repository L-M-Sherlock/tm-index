'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('index.html', 'utf8');
const start = html.indexOf('const relationViaLabel =');
const end = html.indexOf('\n', html.indexOf('const relationList =', start));
assert.ok(start >= 0 && end > start, 'reference list renderer exists');
const code = html.slice(start, end);
const context = vm.createContext({encodeURIComponent});
vm.runInContext(`const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));\n${code}\nglobalThis.relationList = relationList;`, context);

const {relationList} = context;
const items = [
  {id: '1', title: '<待核对文章>', via: ['original'], pending_review: true},
  {id: '2', title: '其他引用', via: ['zhihu']},
  {id: '3', title: '非布尔值', via: ['original'], pending_review: 'true'},
];
const outgoing = relationList(items, '引用', true);
assert.equal((outgoing.match(/>待核对<\/span>/g) || []).length, 1);
assert.ok(outgoing.includes('&lt;待核对文章&gt;'));
assert.ok(outgoing.includes('data-related-id="1"'));
assert.ok(!relationList(items, '反向引用').includes('>待核对</span>'));
assert.ok(html.includes("relationList(outgoing,'引用',true)"));
assert.ok(html.includes("relationList(incoming,'反向引用')"));
console.log('pending reference UI checks passed');
