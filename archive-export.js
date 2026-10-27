import { gfm } from './vendor/turndown-plugin-gfm.js';

const input = document.getElementById('archive-input');
const includeAnswers = document.getElementById('archive-include-answers');
const button = document.getElementById('archive-export-button');
const status = document.getElementById('archive-export-status');
const errors = document.getElementById('archive-export-errors');
const linkList = document.getElementById('archive-link-list');
const links = document.getElementById('archive-links');
const origin = 'https://l-m-sherlock.github.io';
const pattern = /https?:\/\/l-m-sherlock\.github\.io\/ZhiHuArchive\/(\d+)\.html/gi;
const { records: catalog, answerIndex } = JSON.parse(document.getElementById('catalog-data').textContent);
const catalogById = new Map(catalog.map(record => [record.id, record]));
const originalLinks = new Map([
  ...catalog.map(record => [record.id, record.url]),
  ...Object.values(answerIndex).map(answer => [answer.id, answer.url]),
]);
const pendingLinks = new Map();
let pages = [];
let cache = new Map();
let running = false;

function identify(text) {
  const seen = new Set();
  const result = [];
  for (const match of text.matchAll(pattern)) {
    const id = match[1];
    if (!seen.has(id)) {
      seen.add(id);
      result.push({ id, url: `${origin}/ZhiHuArchive/${id}.html` });
    }
  }
  return result;
}

function exportPlan() {
  const result = [...pages];
  const seen = new Set(result.map(page => page.id));
  let addedAnswers = 0;
  let outsideCatalog = 0;
  if (includeAnswers.checked) {
    for (const page of pages) {
      const article = catalogById.get(page.id);
      if (!article) {
        if (!answerIndex[page.id]) outsideCatalog++;
        continue;
      }
      for (const id of article.cited_by_answer_ids || []) {
        if (!answerIndex[id]) throw new Error(`缺少回答 ${id} 的索引数据`);
        if (seen.has(id)) continue;
        seen.add(id);
        addedAnswers++;
        result.push({ id, url: `${origin}/ZhiHuArchive/${id}.html` });
      }
    }
  }
  return { items: result, addedAnswers, outsideCatalog };
}

function refreshSelection() {
  if (running) return;
  errors.replaceChildren();
  errors.hidden = true;
  links.replaceChildren();
  for (const page of pages) {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = page.url;
    link.textContent = page.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    item.append(link);
    links.append(item);
  }
  linkList.hidden = pages.length === 0;
  button.disabled = pages.length === 0;
  button.textContent = '导出 Markdown';
  if (!pages.length) {
    status.textContent = '未识别到备份链接。';
    return;
  }
  try {
    const plan = exportPlan();
    const extra = includeAnswers.checked ? `将额外导出 ${plan.addedAnswers} 篇引用回答。` : '';
    const outside = plan.outsideCatalog ? `其中 ${plan.outsideCatalog} 个链接不在译文目录中，无法为其查找引用回答。` : '';
    status.textContent = `识别到 ${pages.length} 个不同的备份链接。${extra}${outside}`;
  } catch (error) {
    button.disabled = true;
    status.textContent = '引用数据不完整。';
    errors.textContent = error.message;
    errors.hidden = false;
  }
}

function refreshInput() {
  if (running) return;
  pages = identify(input.value);
  cache = new Map();
  refreshSelection();
}

function absoluteUrl(value, base) {
  if (!value) return '';
  try {
    const url = new URL(value, base);
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

function markdownText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim().replace(/([\\\[\]])/g, '\\$1');
}

function archiveId(value, base) {
  try {
    const url = new URL(value, base);
    if (url.origin.toLowerCase() !== origin) return '';
    return url.pathname.match(/^\/ZhiHuArchive\/(\d+)\.html$/i)?.[1] || '';
  } catch {
    return '';
  }
}

function articleOriginal(doc, id, base) {
  const value = absoluteUrl(doc.querySelector('h1 a[href]')?.getAttribute('href'), base);
  if (!value) throw new Error('备份页面没有知乎原文链接');
  const url = new URL(value);
  const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const article = url.hostname === 'zhuanlan.zhihu.com' && new RegExp(`^/p/${escapedId}/?$`).test(url.pathname);
  const answer = url.hostname === 'www.zhihu.com' && new RegExp(`^/question/\\d+/answer/${escapedId}/?$`).test(url.pathname);
  if (url.protocol !== 'https:' || (!article && !answer)) throw new Error(`无法确认文章 ${id} 的知乎原文地址`);
  return url.href;
}

async function originalFor(id) {
  if (originalLinks.has(id)) return originalLinks.get(id);
  if (pendingLinks.has(id)) return pendingLinks.get(id);
  const promise = (async () => {
    const pageUrl = `${origin}/ZhiHuArchive/${id}.html`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(pageUrl, { signal: controller.signal });
      if (!response.ok) throw new Error(`无法读取引用 ${pageUrl}：HTTP ${response.status}`);
      const finalUrl = new URL(response.url);
      if (finalUrl.origin !== origin || finalUrl.pathname !== new URL(pageUrl).pathname) throw new Error(`引用页面跳转到其他地址：${pageUrl}`);
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const target = articleOriginal(doc, id, pageUrl);
      originalLinks.set(id, target);
      return target;
    } finally {
      clearTimeout(timeout);
    }
  })();
  pendingLinks.set(id, promise);
  try { return await promise; } finally { pendingLinks.delete(id); }
}

function replaceArchiveUrl(value, base) {
  const absolute = absoluteUrl(value, base);
  if (!absolute) return '';
  const id = archiveId(absolute, base);
  if (!id) return absolute;
  const target = originalLinks.get(id);
  if (!target) throw new Error(`无法确认引用 ${id} 的知乎原文地址`);
  return target + new URL(absolute).hash;
}

function replaceArchiveText(value) {
  return String(value || '').replace(pattern, (url, id) => {
    const target = originalLinks.get(id);
    if (!target) throw new Error(`无法确认引用 ${id} 的知乎原文地址`);
    return target;
  });
}

async function convertPage(html, page) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const original = doc.querySelector('article[data-pagefind-body]');
  const title = doc.querySelector('h1')?.textContent?.replace(/\s+/g, ' ').trim();
  if (!original || !title) throw new Error('页面没有可识别的文章标题或正文');
  const sourceUrl = articleOriginal(doc, page.id, page.url);
  originalLinks.set(page.id, sourceUrl);
  const article = original.cloneNode(true);
  const referencedIds = new Set();
  for (const node of article.querySelectorAll('[href], [src], [data-actualsrc], [data-url], [data-text]')) {
    for (const attr of ['href', 'src', 'data-actualsrc', 'data-url', 'data-text']) {
      const value = node.getAttribute(attr);
      if (!value) continue;
      const id = archiveId(value, page.url);
      if (id) referencedIds.add(id);
      for (const match of value.matchAll(pattern)) referencedIds.add(match[1]);
    }
  }
  const walker = doc.createTreeWalker(article, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    textNodes.push(walker.currentNode);
    for (const match of walker.currentNode.nodeValue.matchAll(pattern)) referencedIds.add(match[1]);
  }
  for (const id of referencedIds) await originalFor(id);
  for (const textNode of textNodes) textNode.nodeValue = replaceArchiveText(textNode.nodeValue);
  const nativeNotes = [...article.querySelectorAll('sup[data-draft-type="reference"]')];
  const noteNumbers = new Set(nativeNotes.map(node => node.getAttribute('data-numero')).filter(Boolean));
  const referenceLinks = new Map();
  for (const section of article.querySelectorAll('section[data-pagefind-ignore]')) {
    if (section.querySelector('h2')?.textContent?.trim() !== '参考') continue;
    const links = [...section.querySelectorAll('a[href]')];
    const mapped = [];
    for (const link of links) {
      const match = link.previousSibling?.textContent?.match(/^\s*(\d+)\.\s/);
      if (match) mapped.push([match[1], replaceArchiveUrl(link.getAttribute('href'), page.url)]);
    }
    if (links.length && mapped.length === links.length && mapped.every(([n]) => noteNumbers.has(n))) {
      for (const [number, url] of mapped) if (url) referenceLinks.set(number, url);
      section.remove();
    }
  }

  for (const node of article.querySelectorAll('script, style, noscript, object, embed')) node.remove();
  for (const node of article.querySelectorAll('div.column[data-pagefind-ignore], p[data-pagefind-ignore]')) node.remove();
  for (const node of article.querySelectorAll('*')) {
    for (const attr of [...node.attributes]) {
      if (/^on/i.test(attr.name) || attr.name === 'style') node.removeAttribute(attr.name);
    }
  }
  for (const node of article.querySelectorAll('a[href]')) {
    const originalHref = node.getAttribute('href');
    const url = replaceArchiveUrl(originalHref, page.url);
    if (archiveId(originalHref, page.url) && node.textContent.trim() === originalHref) node.textContent = url;
    if (url) node.setAttribute('href', url); else node.removeAttribute('href');
  }
  for (const node of article.querySelectorAll('img')) {
    const url = replaceArchiveUrl(node.getAttribute('data-actualsrc') || node.getAttribute('src'), page.url);
    if (url) node.setAttribute('src', url); else node.removeAttribute('src');
  }
  for (const node of article.querySelectorAll('iframe, video, audio')) {
    const url = replaceArchiveUrl(node.getAttribute('src') || node.querySelector('source[src]')?.getAttribute('src'), page.url);
    const replacement = doc.createElement(url ? 'a' : 'span');
    replacement.textContent = node.getAttribute('title') || node.textContent?.trim() || '嵌入内容';
    if (url) replacement.setAttribute('href', url);
    node.replaceWith(replacement);
  }

  const notes = [];
  const identities = new Map();
  for (const note of nativeNotes) {
    const number = note.getAttribute('data-numero') || '';
    const text = replaceArchiveText(note.getAttribute('data-text') || note.textContent || '');
    const url = referenceLinks.get(number) || replaceArchiveUrl(note.getAttribute('data-url'), page.url);
    const identity = JSON.stringify([number, text, url]);
    if (!identities.has(identity)) {
      const label = `article-${page.id}-${notes.length + 1}`;
      identities.set(identity, label);
      notes.push({ label, text, url });
    }
    note.setAttribute('data-export-note', identities.get(identity));
  }

  const service = new window.TurndownService({ headingStyle: 'atx', codeBlockStyle: 'fenced', bulletListMarker: '-' });
  service.use(gfm);
  service.addRule('complexTable', {
    filter: node => node.nodeName === 'TABLE' && !!node.querySelector('[rowspan], [colspan], table'),
    replacement: (_content, node) => `\n\n${node.outerHTML}\n\n`,
  });
  service.addRule('zhihuReference', {
    filter: node => node.nodeName === 'SUP' && node.hasAttribute('data-export-note'),
    replacement: (_content, node) => `[^${node.getAttribute('data-export-note')}]`,
  });
  const body = service.turndown(article).trim();
  if (!body) throw new Error('文章正文为空');
  const definitions = notes.map(note => {
    const text = markdownText(note.text);
    const link = note.url ? ` [链接](<${note.url.replace(/[<>\s]/g, encodeURIComponent)}>)` : '';
    return `[^${note.label}]: ${text}${link}`.trimEnd();
  });
  const safeTitle = title.replace(/([\\\[\]<>])/g, '\\$1');
  return [`# ${safeTitle}`, `知乎链接：${sourceUrl}`, body, definitions.join('\n')].filter(Boolean).join('\n\n');
}

async function readPage(page) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(page.url, { signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const finalUrl = new URL(response.url);
    if (finalUrl.origin !== origin || finalUrl.pathname !== new URL(page.url).pathname) throw new Error('页面跳转到其他地址');
    return await convertPage(await response.text(), page);
  } finally {
    clearTimeout(timeout);
  }
}

async function exportPages() {
  if (running || pages.length === 0) return;
  let plan;
  try { plan = exportPlan(); } catch (error) {
    errors.textContent = error.message;
    errors.hidden = false;
    return;
  }
  const selectedPages = plan.items;
  running = true;
  input.disabled = includeAnswers.disabled = button.disabled = true;
  errors.hidden = true;
  errors.replaceChildren();
  const failures = [];
  let next = 0;
  let finished = 0;
  status.textContent = `正在读取 0 / ${selectedPages.length} 篇……`;
  async function worker() {
    while (next < selectedPages.length) {
      const index = next++;
      const page = selectedPages[index];
      try {
        if (!cache.has(page.id)) cache.set(page.id, await readPage(page));
      } catch (error) {
        failures.push({ index, page, reason: error.name === 'AbortError' ? '请求超时' : (error.message || '读取失败') });
      }
      finished++;
      status.textContent = `正在读取 ${finished} / ${selectedPages.length} 篇……`;
    }
  }
  try {
    await Promise.all(Array.from({ length: Math.min(4, selectedPages.length) }, worker));
    if (failures.length) {
      failures.sort((a, b) => a.index - b.index);
      const heading = document.createElement('p');
      heading.textContent = `有 ${failures.length} 篇未能读取；未生成不完整文件。点击按钮可重试。`;
      errors.append(heading);
      const list = document.createElement('ul');
      for (const failure of failures) {
        const item = document.createElement('li');
        item.textContent = `${failure.page.url}：${failure.reason}`;
        list.append(item);
      }
      errors.append(list);
      errors.hidden = false;
      status.textContent = `已读取 ${selectedPages.length - failures.length} / ${selectedPages.length} 篇。`;
      button.textContent = '重试并导出';
      return;
    }
    const text = selectedPages.map(page => cache.get(page.id)).join('\n\n---\n\n') + '\n';
    const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = '备份文章汇编.md';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const extra = includeAnswers.checked ? `（含自动追加的 ${plan.addedAnswers} 篇引用回答）` : '';
    status.textContent = `已导出 ${selectedPages.length} 篇${extra}。`;
  } catch (error) {
    errors.textContent = `导出失败：${error.message || '未知错误'}`;
    errors.hidden = false;
    status.textContent = '未生成文件。';
  } finally {
    running = false;
    input.disabled = includeAnswers.disabled = false;
    button.disabled = pages.length === 0;
  }
}

input.addEventListener('input', refreshInput);
includeAnswers.addEventListener('change', refreshSelection);
button.addEventListener('click', exportPages);
