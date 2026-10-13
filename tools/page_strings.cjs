#!/usr/bin/env node
// The OpenDots page's English texts that a page catalog (src/web/translations) lacks.
//
//     node tools/page_strings.cjs <OpenDots checkout> [src/web/translations/pt_BR.json]
//
// It reads src/client and src/shared with the TypeScript compiler of the checkout
// (npm ci there first) and prints, file by file, every text of the page that may be shown
// and is not in the catalog: JSX text, strings in JSX and the strings of the code that
// look like words. Accessible labels are left out (focus.js leaves them in English), and
// so are the texts listed below, which the page does not show as they are. A text built
// from parts ({name}) matches the catalog key with the same words around its parts. The
// list is a starting point, not a verdict: a string of the code may never reach the screen.
//
// SPDX-License-Identifier: GPL-3.0-or-later
'use strict';

const fs = require('fs');
const path = require('path');

// Not shown as they are: asked through window.confirm/prompt (native dialogs in Dotdeck),
// sent to the model, product names, key names and a few technical strings.
const IGNORE = new Set([
  'Leave your unsaved page draft?',
  'Repeat interval in minutes (0 removes the schedule)',
  'Remove {} from this Dot?',
  'Load the latest saved page and replace this draft? Download your draft first if you want to keep it.',
  'Delete "{}"? This can\'t be undone. Any subpages will move to this page\'s parent.',
  'Link URL (https:// or an internal page link)',
  'The owner declined this action. Do not perform it or try another way.',
  'The owner declined this draft. Do not save it.',
  'The draft was saved, then the owner deleted the page. Do not link it.',
  'OpenDots', 'DOTS', 'Dot', 'Dots', 'Slack ·', 'Slack:', 'Dot: {}', 'Terminal',
  'Enter', 'Tab', 'Escape', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'GPT Realtime 2.1', 'GPT-4o Realtime Preview', 'OpenRouter', 'Google Gemini', 'Anthropic',
  'Bearer {}', 'Content-Type', 'Start writing, or type / for blocks…', 'Page content',
  'Collapse', 'Expand', '{} computer: {}', 'From [{}](/#/spaces/{}/pages/{}): {}',
  'Compute failed: {}', 'Invalid compute request.',
  // Parts of texts the catalog has whole ("Refreshed {time}. Screen updates…").
  'Refreshed', '. Screen updates while this panel is open.', 'Choose what',
  'and the computer controls can access.', '’s computer · Current browser view', '’s computer',
  'Edited', 'Repeats every {} ·', 'saved runs', 'is thinking',
  // Examples in fields, and a heading of the brief's downloaded file.
  'https://example.com', 'https://example.com/page', 'https://example.com/mcp', 'notes.txt',
  'pwd', 'research-workflow', 'provider/model-name', 'gpt-realtime-2.1', 'Sources',
]);
// Tool descriptions for the model.
const IGNORE_START = ['Ask the owner to approve a connected-service action', 'Present a Markdown draft'];
// Attributes the page shows (focus.js translates placeholder and title; the others are
// props of the page's own components that end up as text).
const SHOWN_ATTRIBUTES = new Set(['placeholder', 'title', 'label', 'description', 'heading', 'text', 'message', 'hint']);

const usage = () => {
  console.error('Use: node tools/page_strings.cjs <OpenDots checkout> [catalog.json]');
  process.exit(2);
};

const root = process.argv[2];
if (!root || !fs.existsSync(path.join(root, 'src', 'client'))) usage();
const catalogPath =
  process.argv[3] || path.join(__dirname, '..', 'src', 'web', 'translations', 'pt_BR.json');
let ts;
for (const where of [path.join(path.resolve(root), 'node_modules', 'typescript'), 'typescript']) {
  try {
    ts = require(where);
    break;
  } catch (_) {
    // The next place.
  }
}
if (!ts) {
  console.error(`No TypeScript in ${root}/node_modules: run npm ci there first.`);
  process.exit(2);
}

// "{name}’s computer" and `${dot.name}’s computer` alike become "{}’s computer".
const shape = (text) => text.replace(/\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/g, '{}');
const fold = (text) => text.replace(/\s+/g, ' ').trim();
const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8')).strings;
const known = new Set(Object.keys(catalog).map(shape));

const found = new Map(); // text -> { where, kinds }
const add = (raw, node, kind) => {
  const text = fold(raw);
  if (!/[A-Za-z]{2}/.test(text)) return;
  const source = node.getSourceFile();
  const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
  const entry = found.get(text) || { where: `${path.relative(root, source.fileName)}:${line}`, kinds: new Set() };
  entry.kinds.add(kind);
  found.set(text, entry);
};

const template = (node) => {
  if (ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  let text = node.head.text;
  for (const span of node.templateSpans) text += `{${span.expression.getText()}}${span.literal.text}`;
  return text;
};

const visit = (node) => {
  if (ts.isJsxText(node)) {
    add(node.text, node, 'jsx');
  } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateExpression(node)) {
    const text = ts.isStringLiteral(node) ? node.text : template(node);
    // A string compared with something (type === 'dot') is not shown.
    const compared = [ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
      ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken];
    if (ts.isBinaryExpression(node.parent) && compared.includes(node.parent.operatorToken.kind)) {
      ts.forEachChild(node, visit);
      return;
    }
    // A string anywhere in an attribute's expression (a ? 'Expand' : 'Collapse') is its value.
    let parent = node.parent;
    while (ts.isConditionalExpression(parent) || ts.isParenthesizedExpression(parent) || ts.isBinaryExpression(parent)) {
      parent = parent.parent;
    }
    if (ts.isJsxExpression(parent)) parent = parent.parent;
    if (ts.isJsxAttribute(parent)) {
      add(text, node, `attr:${parent.name.getText()}`);
    } else if (!ts.isImportDeclaration(parent) && !ts.isExportDeclaration(parent) && !ts.isLiteralTypeNode(parent)) {
      // Code strings that read like words: a capital first, or a sentence.
      const words = fold(text);
      if (/^[A-Z][a-z]/.test(words) || /^[a-z].* .*[.?!:…]$/.test(words)) add(text, node, 'code');
    }
  }
  ts.forEachChild(node, visit);
};

const walk = (dir) => {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(file);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\./.test(entry.name)) {
      const kind = file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
      visit(ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true, kind));
    }
  }
};
walk(path.join(root, 'src', 'client'));
walk(path.join(root, 'src', 'shared'));

const missing = [];
for (const [text, { where, kinds }] of found) {
  const shown = [...kinds].filter((kind) => !kind.startsWith('attr:') || SHOWN_ATTRIBUTES.has(kind.slice(5)));
  if (!shown.length || known.has(shape(text)) || IGNORE.has(shape(text))) continue;
  if (IGNORE_START.some((start) => text.startsWith(start))) continue;
  missing.push(`${where}\t${JSON.stringify(text)}`);
}
missing.sort((a, b) => a.localeCompare(b, 'en', { numeric: true }));
for (const line of missing) console.log(line);
console.error(`${missing.length} text(s) of the page missing from ${path.basename(catalogPath)}`);
process.exitCode = missing.length ? 1 : 0;
