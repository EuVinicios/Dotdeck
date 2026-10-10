"""A tiny stand-in for an OpenDots server, for tests and screenshots.

It answers the read-only endpoints Dotdeck uses (``/api/state`` and ``/api/workspace``)
with the same status codes as the real server (401 without the owner token, 403 for
unknown hosts when it runs without one) and serves a configurable page at ``/``.

With ``opendots=True`` the page is a small imitation of the OpenDots web client
(src/client/App.tsx upstream): the same DOM structure, classes and accessible labels,
served with the real server's Content-Security-Policy, so focus mode, page commands,
selection sync and pets can be tested without the real server. Add ``?nofiber=1`` to
the page address to leave out the React internals some commands read.

``model_config=`` adds the fork's ``/api/model-config`` endpoints (:class:`FakeModelConfig`):
:data:`MODEL_CONFIG` answers like a server for Dotdeck 0.3.3 (providers, one key per
provider, prices, the call models), :data:`MODEL_CONFIG_032` like one set up with
Dotdeck 0.3.2, and ``None`` like upstream OpenDots (404).
"""

from __future__ import annotations

import copy
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PAGE = "<!doctype html><title>OpenDots</title><h1>OpenDots</h1>"

# The real server's policy (src/server/index.ts upstream).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _png(rgb: tuple[int, int, int], size: int = 64) -> bytes:
    """A round blob of one colour, standing in for the stock mascots."""
    rows = []
    centre, radius = (size - 1) / 2, size * 0.42
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = (x - centre) ** 2 + (y - centre) ** 2 <= radius ** 2
            row += bytes((*rgb, 255 if inside else 0))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + \
            struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + \
        chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


MASCOTS = {name: _png(rgb) for name, rgb in (("blue", (80, 130, 230)), ("mint", (90, 200, 160)),
                                             ("orange", (240, 150, 70)),
                                             ("purple", (150, 110, 220)))}

OPENDOTS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f7f4">
<title>OpenDots</title>
<script src="/theme.js"></script>
<link rel="stylesheet" href="/style.css">
<script type="module" src="/app.js"></script>
</head><body><div id="root"></div></body></html>"""

# public/theme.js upstream: the saved theme before the first paint.
THEME_JS = """(function () {
  var preference = 'system';
  try { preference = localStorage.getItem('opendots-theme') || 'system'; } catch (error) {}
  var dark = preference === 'dark' ||
    (preference !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  window.__themeAtStart = preference;
})();"""

# The layout rules of src/client/style.css that focus mode overrides.
STYLE_CSS = """
body { margin: 0; font: 14px/1.5 sans-serif; background: #fff; color: #242424; }
:root[data-theme='dark'] body { background: #1b1b1b; color: #c5c5c5; }
button { font: inherit; }
.mascot { display: inline-flex; align-items: center; justify-content: center; }
.mascot > img { display: block; width: 100%; height: 100%; object-fit: contain; }
.icon-rail { position: fixed; left: 0; top: 0; bottom: 0; z-index: 30; width: 48px;
  display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 16px 5px;
  border-right: 1px solid #e7e7e7; background: #f5f5f5; box-sizing: border-box; }
.icon-rail button { width: 36px; height: 36px; border: 0; background: transparent; }
.icon-rail .rail-settings { margin-top: auto; }
.template-app.app .sidebar { position: fixed; top: 0; bottom: 0; left: 48px; width: 220px;
  background: #f9f9f9; padding: 23px 12px 12px; border-right: 1px solid #e7e7e7;
  overflow-y: auto; box-sizing: border-box; display: flex; flex-direction: column; gap: 4px; }
.template-app.app .workspace { margin-left: 268px; background: #fff; }
:root[data-theme='dark'] .template-app.app .workspace { background: #1b1b1b; }
.template-app .topbar { height: 64px; padding: 0 22px; display: flex; align-items: center;
  gap: 16px; border-bottom: 1px solid #e7e7e7; box-sizing: border-box; }
.template-app .breadcrumbs { flex: 1; }
.mobile-menu, .nav-scrim { display: none; }
.dot-nav-row { display: flex; }
.dot-nav { flex: 1; display: flex; align-items: center; gap: 8px; min-height: 42px;
  border: 0; background: transparent; text-align: left; }
.dot-nav.active { background: #ececec; border-radius: 8px; }
.template-app .dot-nav > .mascot.small { width: 30px; height: 30px; }
.nav-item { display: flex; gap: 8px; border: 0; background: transparent; text-align: left; }
.nav-item.active { font-weight: 600; }
.thread-summary { display: flex; flex-direction: column; }
.template-app .chat-workspace { height: calc(100dvh - 64px); display: flex; min-height: 0; }
.chat-column { flex: 1; min-width: 0; }
.live-chat { display: flex; flex-direction: column; height: 100%; padding: 18px 35px 22px;
  max-width: 850px; margin: 0 auto; box-sizing: border-box; }
.chat-persona { display: flex; align-items: center; gap: 6px; position: relative;
  justify-content: center; min-height: 94px; }
.chat-persona > .mascot { width: 50px; height: 50px; }
.chat-persona .chat-persona-actions { position: absolute; right: 0; top: 0; display: flex; }
.chat-transcript { flex: 1; overflow: auto; }
.chat-welcome { text-align: center; margin: 60px auto; max-width: 440px; }
.chat-welcome p { font-size: 13px; color: #777; }
.new-conversation { display: flex; flex-direction: column; height: 100%; max-width: 800px;
  margin: 0 auto; padding: 25px 38px 28px; box-sizing: border-box; }
.empty-chat-persona { text-align: center; margin-bottom: 25px; }
.empty-chat-persona .mascot { width: 68px; height: 68px; margin: 0 auto 4px; }
.empty-chat-persona p { font-size: 13px; color: #777; max-width: 420px; margin: 0 auto 8px; }
.composer { margin-top: auto; border: 1px solid #dedede; border-radius: 22px; padding: 12px; }
.composer textarea, .chat-composer textarea { width: 100%; border: 0; resize: none; }
.chat-workspace > .result-pane { width: 400px; background: #fafafa; flex-shrink: 0; }
.chat-workspace.split .chat-column { flex: 1; }
.computer-overlay { position: fixed; top: 64px; right: 0; bottom: 0; width: 460px;
  background: #fdfcfb; }
.call-view { position: fixed; z-index: 60;
  inset: 20px 20px 20px max(290px, calc((100vw - 1040px) / 2));
  border-radius: 24px; background: #1c544c; color: #fff; display: flex; flex-direction: column;
  align-items: center; padding: 24px 32px; }
.call-persona { display: flex; flex-direction: column; align-items: center; }
.call-persona .mascot { width: 154px; height: 154px; }
.unlock { display: flex; flex-direction: column; align-items: center; padding-top: 80px; }
.unlock .mascot { width: 180px; height: 180px; }
.large-empty { text-align: center; }
.large-empty .mascot { width: 140px; height: 140px; }
.notice { padding: 12px 40px; font-size: 11px; background: #f3eee4; }
@media (max-width: 1100px) {
  .chat-workspace.split { height: auto; display: block; }
  .chat-workspace.split .live-chat { min-height: 650px; height: calc(100dvh - 83px); }
  .chat-workspace.split > .result-pane { position: fixed; top: 64px; right: 0; bottom: 0;
    width: min(460px, 100vw); }
}
.modal-backdrop { position: fixed; inset: 0; background: #0006; display: grid;
  place-items: center; z-index: 80; }
.modal { background: #fff; padding: 24px; border-radius: 16px; min-width: 320px; }
"""

# A plain-JavaScript imitation of the OpenDots client: same structure, classes and
# accessible labels as src/client/App.tsx, Chat.tsx, CallView.tsx and ThreadList.tsx.
APP_JS = r"""
const FIBER = '__reactFiber$fake';
const withFiber = !new URLSearchParams(location.search).has('nofiber');
const S = {
  state: null, workspace: null, needsAuth: false, selectedDot: '', selectedThread: undefined,
  view: 'chat', spaceId: '', pageId: undefined, dialog: null, pane: false, running: false,
  dotsCollapsed: localStorage.getItem('opendots_dots_collapsed') === 'true',
  voice: 'idle', voiceReadyAt: 0, dirty: false, prompts: [], chosen: 0, callThread: undefined,
};
window.__fake = S;
window.__fakeRender = () => render();
const token = () => sessionStorage.getItem('opendots-token') || '';
const api = async (path) => {
  const response = await fetch('/api' + path, { headers: { Authorization: 'Bearer ' + token() } });
  if (response.status === 401) { const e = new Error('401'); e.status = 401; throw e; }
  return response.json();
};
const MASCOTS = ['blue', 'mint', 'orange', 'purple'];
const character = (identity) => {
  if (!identity) return MASCOTS[0];
  let hash = 0;
  for (const c of identity) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return MASCOTS[hash % MASCOTS.length];
};
const h = (tag, attrs, ...children) => {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'className') el.className = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
};
const mascot = ({ state = 'idle', small = false, identity, name = 'Dot', decorative = false } = {}) =>
  h('span', { className: `mascot ${state} ${small ? 'small' : ''}` },
    h('img', { className: 'dot-body', src: `/dots/${character(identity)}.png`,
      alt: decorative ? '' : `${name} is ${state}`, width: 512, height: 512, draggable: 'false' }));

const setView = (next) => {
  if (S.dirty && !window.confirm('Leave your unsaved page draft?')) return false;
  S.dirty = false;
  if (next !== 'space') history.replaceState(null, '', location.pathname + location.search);
  S.view = next;
  return true;
};
const update = (changes) => { Object.assign(S, changes); render(); };
const dot = () => S.workspace.dots.find((d) => d.id === S.selectedDot) || S.workspace.dots[0];
const chooseDot = (next) => {
  if (!setView('chat')) return render();
  S.chosen += 1;
  S.selectedDot = next.id;
  const latest = S.workspace.conversations.find((c) => c.dotId === next.id);
  S.selectedThread = latest && latest.id;
  S.voiceReadyAt = Date.now() + 700;
  render();
};
const selectThread = (id) => {
  const conversation = S.workspace.conversations.find((c) => c.id === id);
  if (!setView('chat')) return render();
  if (conversation) S.selectedDot = conversation.dotId;
  S.selectedThread = id;
  S.voiceReadyAt = Date.now() + 700;
  render();
};
const goHome = () => { if (setView('chat')) S.selectedThread = undefined; render(); };
window.addEventListener('hashchange', () => {
  const match = location.hash.match(/^#\/spaces\/([^/]+)(?:\/pages\/([^/]+))?$/);
  if (!match) return;
  if (S.dirty && !window.confirm('Leave your unsaved page draft?')) return;
  S.dirty = false;
  update({ spaceId: match[1], pageId: match[2], view: 'space' });
});

const refresh = async () => {
  try {
    const [state, workspace] = await Promise.all([api('/state'), api('/workspace')]);
    const changed = JSON.stringify([state, workspace]) !== JSON.stringify([S.state, S.workspace]);
    S.state = state; S.workspace = workspace; S.needsAuth = false;
    S.selectedDot = S.selectedDot || (workspace.dots[0] && workspace.dots[0].id) || '';
    if (changed) render();
  } catch (error) {
    if (error.status === 401 && !S.needsAuth) { S.needsAuth = true; render(); }
  }
};

const sidebar = () => {
  const ws = S.workspace, current = dot();
  const rows = ws.dots.map((item) => {
    const active = current.id === item.id && S.view === 'chat';
    return h('div', { className: 'dot-nav-row' },
      h('button', { className: `dot-nav ${active ? 'active' : ''}`, 'aria-current': active ? 'page' : null,
        onclick: () => chooseDot(item) },
        mascot({ identity: item.id, name: item.name, small: true, decorative: true }),
        h('span', null, item.name)),
      h('button', { className: 'icon-button dot-settings', 'aria-label': `Edit ${item.name} settings`,
        onclick: () => update({ dialog: { type: 'dot', name: item.name } }) }, '…'));
  });
  const list = h('section', { className: 'thread-list' },
    h('div', { className: 'nav-label' }, 'RECENT CHATS',
      h('button', { className: 'icon-button', 'aria-label': 'New conversation' }, '+')),
    ws.conversations.map((thread) => {
      const button = h('button', { className: `nav-item ${S.view === 'chat' && S.selectedThread === thread.id ? 'active' : ''}`,
        onclick: () => selectThread(thread.id) },
        h('span', { className: 'thread-summary' }, h('span', null, thread.title),
          h('small', null, (ws.dots.find((d) => d.id === thread.dotId) || {}).name)));
      if (withFiber) button[FIBER] = { key: thread.id, memoizedProps: { className: button.className } };
      return button;
    }));
  if (withFiber) {
    list[FIBER] = { memoizedProps: { className: 'thread-list' }, return: {
      memoizedProps: { dots: ws.dots, dotId: current.id, local: ws.conversations,
        selected: S.view === 'chat' ? S.selectedThread : undefined, onSelect: selectThread, onNew: () => {} },
      return: null } };
  }
  return h('aside', { id: 'workspace-sidebar', className: 'sidebar' },
    h('button', { className: 'wordmark', onclick: goHome }, 'OpenDots'),
    h('button', { className: 'new-chat nav-item' }, h('span', null, 'New chat')),
    h('div', { className: 'spaces-heading nav-label' },
      h('button', { className: 'dots-collapse-button', 'aria-label': S.dotsCollapsed ? 'Expand dots list' : 'Collapse dots list',
        onclick: () => { localStorage.setItem('opendots_dots_collapsed', String(!S.dotsCollapsed)); update({ dotsCollapsed: !S.dotsCollapsed }); } },
        h('span', null, 'DOTS'), h('span', { className: 'dots-count' }, `(${ws.dots.length})`)),
      h('button', { className: 'icon-button', 'aria-label': 'Create Dot' }, '+')),
    S.dotsCollapsed ? null : h('nav', { className: 'dots-nav', 'aria-label': 'Dots' }, rows),
    h('div', { className: 'spaces-heading nav-label' }, 'SPACES',
      h('button', { className: 'icon-button', 'aria-label': 'Create Space' }, '+')),
    h('nav', { className: 'spaces-nav', 'aria-label': 'Spaces' }, ws.spaces.map((space) =>
      h('div', { className: 'space-nav-group' }, h('div', { className: 'space-nav-row' },
        h('button', { className: `nav-item ${S.view === 'space' && S.spaceId === space.id ? 'active' : ''}`,
          onclick: () => { location.hash = `#/spaces/${space.id}`; } }, space.name))))),
    list,
    h('div', { className: 'sidebar-bottom' },
      h('button', { className: `nav-item ${S.view === 'tasks' ? 'active' : ''}`, onclick: () => { setView('tasks'); render(); } },
        h('span', null, 'Scheduled & activity')),
      h('button', { className: `nav-item ${S.view === 'memories' ? 'active' : ''}`, onclick: () => { setView('memories'); render(); } },
        h('span', null, 'Memories')),
      h('button', { className: 'nav-item', onclick: () => update({ dialog: { type: 'settings' } }) },
        h('span', null, 'Settings & setup')),
      h('a', { className: 'nav-item', href: 'https://github.com/CopilotKit/OpenDots', target: '_blank' }, 'Make it your own')));
};

const callView = (current) => {
  if (S.voice === 'idle') return null;
  return h('section', { className: 'call-view', 'aria-label': `Voice call with ${current.name}` },
    h('div', { className: 'call-heading' }, h('span', null, 'Voice call')),
    h('div', { className: 'call-persona listening' }, mascot({ identity: current.id, name: current.name }),
      h('h2', null, current.name), h('p', { role: 'status' }, S.voice === 'ending' ? 'Saving call…' : 'Listening')),
    h('div', { className: 'call-controls' },
      h('button', { className: 'call-end', 'aria-label': 'End voice call', disabled: S.voice === 'ending',
        onclick: endCall }, 'End')));
};
const startCall = () => {
  update({ voice: 'connecting', callThread: S.selectedThread });
  setTimeout(() => S.voice === 'connecting' && update({ voice: 'active' }), 300);
};
const endCall = () => {
  update({ voice: 'ending' });
  setTimeout(() => update({ voice: 'idle' }), 500);
};

const chat = (current, thread) => {
  const voiceEnabled = S.workspace.setup.voice && !S.state.settings.paused && Date.now() >= S.voiceReadyAt;
  if (!voiceEnabled && S.workspace.setup.voice) setTimeout(render, Math.max(50, S.voiceReadyAt - Date.now() + 20));
  return h('div', { className: 'live-chat' },
    h('header', { className: 'chat-persona' },
      mascot({ identity: current.id, name: current.name, small: true, state: S.running ? 'working' : S.state.settings.paused ? 'paused' : 'idle' }),
      h('div', null, h('strong', null, current.name), h('span', null, S.running ? 'Thinking…' : 'Here with you')),
      h('div', { className: 'chat-persona-actions' },
        h('button', { className: 'icon-button', 'aria-label': 'Save conversation as page', disabled: S.running,
          onclick: () => { S.prompts.push(window.prompt('Page title', thread.title)); } }, '▤'),
        h('button', { className: 'icon-button', 'aria-label': 'Schedule a task in this conversation',
          onclick: () => update({ dialog: { type: 'schedule' } }) }, '◷'),
        h('button', { className: `icon-button ${S.voice === 'active' ? 'on-call' : ''}`,
          'aria-label': S.voice === 'idle' ? 'Start voice call' : 'End voice call',
          disabled: !voiceEnabled, onclick: () => (S.voice === 'idle' ? startCall() : endCall()) }, '☎'))),
    h('div', { className: 'chat-transcript' },
      h('div', { className: 'chat-welcome' }, h('span', { className: 'eyebrow' }, 'A LITTLE SPACE TO THINK'),
        h('h1', null, 'What’s on your mind?'), h('p', null, current.instructions),
        h('p', { className: 'muted' }, 'Your conversation stays with this Dot, across text and calls.'))),
    callView(current),
    h('form', { className: 'chat-composer' }, h('textarea', { 'aria-label': 'Message your Dot', placeholder: `Message ${current.name}…`, rows: 1 })));
};

const emptyChat = (current) => h('div', { className: 'new-conversation' },
  h('div', { className: 'empty-chat-persona' },
    mascot({ identity: current.id, name: current.name, state: S.state.settings.paused ? 'paused' : 'idle' }),
    h('h2', null, current.name), h('p', null, current.instructions),
    h('button', { className: 'text-button', onclick: () => update({ dialog: { type: 'dot', name: current.name } }) }, 'Edit specialist')),
  h('form', { className: 'composer' },
    h('textarea', { 'aria-label': 'Start a conversation', placeholder: `Message ${current.name}…` }),
    h('div', { className: 'composer-bottom' }, h('button', { className: 'send-button', 'aria-label': 'Start conversation', disabled: true }, '↑'))));

const main = (current) => {
  if (S.view === 'space') {
    const space = S.workspace.spaces.find((s) => s.id === S.spaceId) || S.workspace.spaces[0] || { name: '' };
    return h('div', { className: `spaces-surface ${S.pageId ? 'writing' : 'library'}` },
      h('div', { className: 'space-library' }, h('h1', null, space.name), S.pageId ? h('p', null, `Page ${S.pageId}`) : null));
  }
  if (S.view === 'chat') {
    const thread = S.workspace.conversations.find((c) => c.id === S.selectedThread && c.dotId === current.id);
    return h('div', { className: `chat-workspace ${S.pane ? 'split' : ''}` },
      h('div', { className: 'chat-column' }, thread ? chat(current, thread) : emptyChat(current)),
      S.pane ? h('aside', { className: 'result-pane' }, h('div', { className: 'pane-empty' },
        mascot({ identity: current.id, name: current.name }), h('h3', null, 'A little space for your findings.'))) : null);
  }
  return h('main', { className: 'main-content' },
    h('div', { className: 'page-heading' }, h('h1', null, S.view === 'memories' ? 'Memories' : 'A little follow-through.')),
    S.view === 'memories' ? h('div', { className: 'large-empty' }, mascot(), h('h2', null, 'A little context goes a long way.')) : null);
};

function render() {
  const root = document.getElementById('root');
  // Upstream the call lives in its conversation's Chat: leaving that chat ends the call.
  if (S.voice !== 'idle' && (S.view !== 'chat' || S.selectedThread !== S.callThread)) {
    S.voice = 'idle';
  }
  if (S.needsAuth) {
    root.replaceChildren(h('main', { className: 'unlock' }, mascot(), h('h1', null, 'Your own little corner.'),
      h('form', null, h('input', { type: 'password', 'aria-label': 'Owner access token' }), h('button', { className: 'primary' }, 'Unlock OpenDots'))));
    return;
  }
  if (!S.state || !S.workspace || !S.workspace.dots.length) {
    root.replaceChildren(h('main', { className: 'unlock' }, mascot({ state: 'working' }), h('h1', null, 'Finding your dots…')));
    return;
  }
  const current = dot();
  const crumb = S.view === 'chat' ? current.name : S.view === 'tasks' ? 'Activity' : S.view === 'space' ? 'Pages' : 'Memories';
  const app = h('div', { className: 'app template-app' },
    h('nav', { className: 'icon-rail', 'aria-label': 'Workspace navigation' },
      h('button', { className: 'rail-brand', 'aria-label': 'OpenDots home', onclick: goHome }, 'o', h('span', null, '·')),
      h('button', { 'aria-label': 'Collapse sidebar' }, '⫶'),
      h('button', { 'aria-label': 'New chat' }, '+'),
      h('button', { 'aria-label': 'Open Spaces', onclick: () => { location.hash = `#/spaces/${S.workspace.spaces[0].id}`; } }, '▢'),
      h('button', { 'aria-label': 'Open activity', onclick: () => { setView('tasks'); render(); } }, '◷'),
      h('button', { className: 'rail-settings', 'aria-label': 'Open settings', onclick: () => update({ dialog: { type: 'settings' } }) }, '⚙')),
    h('button', { className: 'mobile-menu icon-button', 'aria-label': 'Open navigation' }, '≡'),
    sidebar(),
    h('div', { className: 'workspace' },
      h('header', { className: 'topbar' },
        h('button', { className: 'desktop-nav-toggle document-icon', 'aria-label': 'Hide navigation' }, '⫶'),
        h('div', { className: 'breadcrumbs' },
          h('span', null, S.view === 'space' ? ((S.workspace.spaces.find((s) => s.id === S.spaceId) || {}).name || '') : 'Dots'),
          h('span', null, '/'), h('strong', null, crumb)),
        h('div', { className: 'top-actions' },
          h('button', { className: 'pause-button', 'aria-label': 'Pause all Dots' }, 'Pause'),
          h('button', { className: 'icon-button', 'aria-label': S.pane ? 'Hide computer' : 'Show computer',
            'aria-expanded': S.pane ? 'true' : 'false', onclick: () => update({ pane: !S.pane }) }, '▭'))),
      S.state.settings.paused ? h('div', { className: 'notice' },
        'All Dots are paused. Active compute stops and scheduled tasks wait.') : null,
      main(current),
      S.pane && S.view !== 'chat' ? h('div', { className: 'computer-overlay' }, h('aside', { className: 'result-pane' })) : null),
    S.dialog ? h('div', { className: 'modal-backdrop' },
      h('section', { className: 'modal', role: 'dialog', 'aria-modal': 'true' },
        h('button', { className: 'modal-close icon-button', 'aria-label': 'Close dialog', onclick: () => update({ dialog: null }) }, '×'),
        h('h2', { id: 'dialog-title' }, S.dialog.type === 'dot' ? `Edit ${S.dialog.name}`
          : S.dialog.type === 'schedule' ? 'Schedule a task' : 'Your workspace, your rules.'))) : null);
  root.replaceChildren(app);
}

render();
refresh();
setInterval(refresh, 1000);
"""


# GET /api/model-config of a server set up with Dotdeck 0.3.2: one key, a provider guessed
# from the address, presets without prices, nothing about transcription.
MODEL_CONFIG_032 = {
    "model": "deepseek/deepseek-v4-flash-0731", "baseUrl": "https://openrouter.ai/api/v1",
    "provider": "openrouter", "hasApiKey": True, "voiceModel": "openai/gpt-audio-mini",
    "voiceName": "marin", "hasVoiceKey": False, "voiceReady": True, "voiceGateway": True,
    "presets": [
        {"id": "deepseek/deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash (fast, low cost)",
         "provider": "openrouter"},
        {"id": "anthropic/claude-sonnet-5.5", "name": "Claude Sonnet 5.5 (Anthropic)",
         "provider": "openrouter"},
        {"id": "google/gemini-3.8-flash", "name": "Gemini 3.8 Flash (Google)",
         "provider": "openrouter"},
    ],
}

OPENROUTER_URL = "https://openrouter.ai/api/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/"
PROVIDER_NAMES = {"openrouter": "OpenRouter", "anthropic": "Anthropic",
                  "openai-compatible": "OpenAI-compatible"}


def _preset(model_id, name, provider, price, speed, note=""):
    return {"id": model_id, "name": name, "provider": provider,
            "price": {"input": price[0], "output": price[1]} if price else None,
            "speed": speed, "note": note}


# The same server for Dotdeck 0.3.3 (prices here are test values, not a price list).
MODEL_CONFIG = {
    "model": "anthropic/claude-haiku-5.5", "baseUrl": OPENROUTER_URL, "provider": "openrouter",
    "configured": True, "modelName": "Claude Haiku 5.5",
    "price": {"input": 0.10, "output": 0.50}, "hasApiKey": True,
    "keys": {"openrouter": True, "anthropic": False, "openai-compatible": False},
    "providers": [
        {"id": "openrouter", "name": "OpenRouter", "hasKey": True, "baseUrl": OPENROUTER_URL},
        {"id": "anthropic", "name": "Anthropic", "hasKey": False, "baseUrl": ANTHROPIC_URL},
        {"id": "openai-compatible", "name": "OpenAI-compatible", "hasKey": False,
         "baseUrl": None},
    ],
    "voiceModel": "openai/gpt-audio-mini", "voiceName": "marin", "hasVoiceKey": False,
    "voiceReady": True, "voiceGateway": True,
    "presets": [
        _preset("deepseek/deepseek-v4-flash-0731", "DeepSeek V4 Flash", "openrouter",
                (0.0046, 1.28), "fast", "lowest cost"),
        _preset("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", "openrouter", (0.10, 0.50),
                "fast"),
        _preset("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5", "openrouter", (1.0, 5.0),
                "balanced"),
        _preset("claude-haiku-5-5", "Claude Haiku 5.5", "anthropic", (0.10, 0.50), "fast"),
        _preset("claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", (1.0, 5.0), "balanced"),
        _preset("claude-opus-5-5", "Claude Opus 5.5", "anthropic", None, "deep"),
    ],
    "call": {"voiceModel": "openai/gpt-audio-mini",
             "transcribeModel": "google/gemini-2.5-flash-lite",
             "thinkingModel": "anthropic/claude-haiku-5.5", "gateway": True,
             "voiceName": "marin"},
}

MODEL_KEYS = {"provider", "model", "baseUrl", "apiKey", "voiceModel", "voiceKey", "voiceName"}


class FakeModelConfig:
    """``/api/model-config`` (GET, POST) and ``/api/model-config/test`` of the fork.

    Checks what is sent like the server does: a 0.3.2-era server drops keys it does not
    know (zod strips them), a 0.3.3 one refuses them and checks the model id against the
    provider. Every POST body is kept in ``posts`` and every test body in ``tests``.
    """

    def __init__(self, data: dict | None):
        self.data = copy.deepcopy(data) if data is not None else None
        self.posts: list[dict] = []
        self.tests: list[dict] = []
        self.test_result: dict = {"ok": True, "ms": 1234}
        self.persisted = True              # False: the .env could not be written

    @property
    def modern(self) -> bool:
        return self.data is not None and isinstance(self.data.get("keys"), dict)

    def get(self) -> tuple[int, dict]:
        if self.data is None:
            return 404, {"error": "Not found."}
        return 200, copy.deepcopy(self.data)

    def post(self, body) -> tuple[int, dict]:
        if self.data is None:
            return 404, {"error": "Not found."}
        self.posts.append(body)
        if not isinstance(body, dict) or not all(isinstance(v, str) and v.strip()
                                                 for v in body.values()):
            return 400, {"error": "Invalid model or voice settings."}
        if any("\n" in v or "\r" in v for v in body.values()):
            return 400, {"error": "Values must be on one line."}
        data = self.data
        if not self.modern:
            body = {k: v for k, v in body.items() if k in MODEL_KEYS - {"provider"}}
        elif set(body) - MODEL_KEYS:
            return 400, {"error": "Invalid model or voice settings."}
        target = body.get("provider", data["provider"])
        if self.modern:
            if target not in PROVIDER_NAMES:
                return 400, {"error": "Unknown provider."}
            model = body.get("model", data.get("model") or "")
            if model and target == "openrouter" and "/" not in model:
                return 400, {"error": "OpenRouter model ids look like vendor/model."}
            if model and target == "anthropic" and "/" in model:
                return 400, {"error": "Anthropic model ids have no vendor prefix."}
            if body.get("apiKey"):
                data["keys"][target] = True
            if target != data["provider"]:
                data["provider"] = target
                data["baseUrl"] = body.get("baseUrl") or {"openrouter": OPENROUTER_URL,
                                                          "anthropic": ANTHROPIC_URL}.get(target)
            elif body.get("baseUrl"):
                data["baseUrl"] = body["baseUrl"]
            for item in data["providers"]:
                item["hasKey"] = data["keys"][item["id"]]
                if item["id"] == target and body.get("baseUrl"):
                    item["baseUrl"] = body["baseUrl"]
            data["hasApiKey"] = data["keys"][data["provider"]]
            data["configured"] = bool(data.get("model") or body.get("model")) and \
                data["hasApiKey"]
        elif body.get("apiKey"):
            data["hasApiKey"] = True
        if body.get("model"):
            data["model"] = body["model"]
            if self.modern:
                found = next((p for p in data["presets"] if p["id"] == body["model"]), None)
                data["modelName"] = found["name"] if found else None
                data["price"] = found["price"] if found else None
                data["call"]["thinkingModel"] = body["model"]
        if body.get("voiceModel"):
            data["voiceModel"] = body["voiceModel"]
            if self.modern:
                data["call"]["voiceModel"] = body["voiceModel"]
        if body.get("voiceName"):
            data["voiceName"] = body["voiceName"]
        if body.get("voiceKey"):
            data["hasVoiceKey"] = True
        answer = {"ok": True, "persisted": self.persisted}
        for key in ("model", "baseUrl", "voiceModel", "voiceName", "hasVoiceKey", "voiceReady",
                    "voiceGateway") + (("provider",) if self.modern else ()):
            answer[key] = data.get(key)
        return 200, answer

    def test(self, body) -> tuple[int, dict]:
        if not self.modern:
            return 404, {"error": "Not found."}
        self.tests.append(body)
        return 200, dict(self.test_result)

    def workspace_fields(self) -> dict:
        """What the fork adds to ``/api/workspace`` (nothing upstream)."""
        if self.data is None:
            return {}
        return {"activeModel": self.data.get("model") or "",
                "activeProvider": self.data.get("provider") or ""}


class FakeOpenDots:
    def __init__(self, token: str | None = None, local_only: bool = False,
                 name: str = "OpenDots Test", dots: list[str] | None = None,
                 page: str = DEFAULT_PAGE, opendots: bool = False,
                 instructions: dict[str, str] | None = None,
                 conversations: list[dict] | None = None,
                 spaces: list[dict] | None = None, voice: bool = True,
                 model_config: dict | None = None):
        self.token = token
        self.model = FakeModelConfig(model_config)
        self.local_only = local_only
        self.name = name
        self.dots = dots if dots is not None else ["Chief", "Researcher"]
        self.instructions = instructions or {}
        self.conversations = conversations or []
        self.spaces = spaces if spaces is not None else [
            {"id": "s0", "name": "Home", "description": "", "createdAt": 1}]
        self.voice = voice
        self.paused = False
        self.tasks: list[dict] = []
        self.calls: list[dict] = []
        self.page = OPENDOTS_PAGE if opendots else page
        self.opendots = opendots
        self.requests: list[tuple[str, str | None]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: bytes, kind: str, csp: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                if csp:
                    self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def do_GET(self):  # noqa: N802 (http.server naming)
                auth = self.headers.get("Authorization")
                fake.requests.append((self.path, auth))
                path = self.path.split("?", 1)[0]
                if not path.startswith("/api/"):
                    assets = {
                        "/app.js": (APP_JS, "text/javascript; charset=utf-8"),
                        "/theme.js": (THEME_JS, "text/javascript; charset=utf-8"),
                        "/style.css": (STYLE_CSS, "text/css; charset=utf-8"),
                    }
                    mascot = path[len("/dots/"):].removesuffix(".png")
                    if fake.opendots and path in assets:
                        body, kind = assets[path]
                        self._send(200, body.encode(), kind, csp=True)
                    elif fake.opendots and path.startswith("/dots/") and mascot in MASCOTS:
                        self._send(200, MASCOTS[mascot], "image/png", csp=True)
                    else:
                        self._send(200, fake.page.encode(), "text/html; charset=utf-8",
                                   csp=fake.opendots)
                    return
                if fake.local_only:
                    self._json(403, {"error": "Unrecognized host."})
                    return
                if fake.token and auth != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token to unlock OpenDots."})
                    return
                if path == "/api/state":
                    self._json(200, {"settings": {"name": fake.name, "paused": fake.paused,
                                                  "researchAllowed": True, "memoryAllowed": True},
                                     "tasks": fake.tasks, "memories": [], "mode": "live",
                                     "configured": True})
                elif path == "/api/workspace":
                    self._json(200, fake.workspace())
                elif path == "/api/model-config":
                    self._json(*fake.model.get())
                else:
                    self._json(404, {"error": "Not found."})

            def do_POST(self):  # noqa: N802
                auth = self.headers.get("Authorization")
                fake.requests.append((self.path, auth))
                size = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(size).decode() or "null")
                except ValueError:
                    body = None
                if fake.token and auth != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token to unlock OpenDots."})
                elif self.path == "/api/model-config":
                    self._json(*fake.model.post(body))
                elif self.path == "/api/model-config/test":
                    self._json(*fake.model.test(body))
                else:
                    self._json(404, {"error": "Not found."})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def workspace(self) -> dict:
        """The ``/api/workspace`` answer: Dots get ids d0, d1… in list order."""
        space = self.spaces[0]["id"] if self.spaces else ""
        return {"spaces": self.spaces, "conversations": self.conversations, "calls": self.calls,
                "dots": [{"id": f"d{i}", "name": n, "spaceId": space,
                          "spaceIds": [space] if space else [],
                          "instructions": self.instructions.get(n, f"# {n} — helps with {n}."),
                          "researchAllowed": True, "memoryAllowed": True, "createdAt": i + 1}
                         for i, n in enumerate(self.dots)],
                "setup": {"intelligence": True, "model": True, "browser": False,
                          "voice": self.voice, "slack": "not_configured", "missing": []},
                **self.model.workspace_fields()}

    def __enter__(self) -> FakeOpenDots:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def start(self) -> FakeOpenDots:
        return self.__enter__()

    def stop(self) -> None:
        self.__exit__()


class FakeUpdater:
    """Stand-in for server/updater: two official changes, then an update that finishes."""

    def __init__(self, token: str, behind: int = 2):
        self.token = token
        self.behind = behind
        self.polls_until_done = 2
        self.updates_started = 0
        self.job = {"state": "idle", "log": []}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status: int, data) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _ok(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._json(401, {"error": "Enter your owner access token."})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if self._ok():
                    if fake.job["state"] == "running":
                        fake.polls_until_done -= 1
                        if fake.polls_until_done <= 0:
                            fake.job = {"state": "done", "message": "Updated to abc1234.",
                                        "finished_at": "2026-10-10T01:00:00+00:00"}
                            fake.behind = 0
                    self._json(200, fake.status())

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self._ok():
                    return
                if self.path == "/update":
                    fake.updates_started += 1
                    fake.job = {"state": "running", "step": "build", "message": "npm run build"}
                    self._json(202, fake.status())
                else:
                    self._json(200, fake.status())

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def status(self) -> dict:
        news = [{"short": "c0ffee1", "subject": "feat: dark mode", "date": "2026-10-09T12:00:00+00:00"},
                {"short": "c0ffee2", "subject": "fix: voice receipts", "date": "2026-10-08T12:00:00+00:00"}]
        return {"version": 1, "behind": self.behind, "ahead": 5, "dirty": False,
                "news": news[:self.behind],
                "current": {"short": "083a6b4", "subject": "fix(telegram): tokens",
                            "date": "2026-10-09T22:00:00+00:00"},
                "upstream": {"short": "c0ffee1", "subject": "feat: dark mode",
                             "date": "2026-10-09T12:00:00+00:00"},
                "job": self.job}

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
