// Dotdeck: drives the OpenDots page from the native window.
//
// - Focus mode: once the page is recognized as the OpenDots web client, <html> gets
//   data-dotdeck-focus and focus.css hides the page's own rail, sidebar and top bar.
//   A page that is not recognized within the probe time is left exactly as it is.
// - Commands: window.__dotdeck.run(name, args) opens a Dot, a conversation, a Space,
//   starts or ends a call... by pressing the page's own (possibly hidden) buttons, then
//   waits until the page shows the result and answers true or false.
// - Selection sync: what the page shows (Dot and view) and the open Dot's state are
//   posted to Dotdeck whenever they change.
// - Pets: every mascot gets data-dotdeck-pet, which the pets style sheet uses to draw
//   the Dot's pet instead of the stock mascot.
// - Theme: the page follows Dotdeck's colour scheme.
// - Offline: while Dotdeck's banner says the server is out of reach, <html> gets
//   data-dotdeck-offline and focus.css hides the page's own error bar.
//
// The OpenDots client has no ids in its DOM and no routes for Dots or conversations,
// so this relies on its structure and accessible labels (src/client/App.tsx upstream).
// Every class, id, label and word of the page used here is listed in contract.json,
// which tests/test_contract.py keeps in sync with this file and checks against the
// official OpenDots every day (.github/workflows/upstream.yml).
//
// SPDX-License-Identifier: GPL-3.0-or-later
(() => {
  'use strict';
  const config = __DOTDECK_FOCUS_CONFIG__;
  if (window.location.origin !== config.origin || window.__dotdeck) return;

  const FOCUS = 'data-dotdeck-focus';
  const PET = 'data-dotdeck-pet';
  const DOTS = 'nav[aria-label="Dots"]';
  const EXPAND_DOTS = 'button[aria-label="Expand dots list"]';
  const THEME_KEY = 'opendots-theme';
  const WAIT = 5000;

  const post = (message) => {
    try {
      window.webkit.messageHandlers.dotdeck.postMessage(JSON.stringify(message));
    } catch (_) {
      // Dotdeck is gone (page unloading).
    }
  };
  const $ = (selector, scope) => (scope || document).querySelector(selector);
  const $$ = (selector, scope) => Array.from((scope || document).querySelectorAll(selector));
  const text = (node) => ((node && node.textContent) || '').replace(/\s+/g, ' ').trim();
  const html = () => document.documentElement;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // Resolves with the first truthy value of test(), or null after timeout ms.
  const waitFor = (test, timeout = WAIT) =>
    new Promise((resolve) => {
      const attempt = () => {
        try {
          return test();
        } catch (_) {
          return null;
        }
      };
      const first = attempt();
      if (first) {
        resolve(first);
        return;
      }
      let finished = false;
      const finish = (value) => {
        if (finished) return;
        finished = true;
        observer.disconnect();
        clearInterval(poll);
        clearTimeout(timer);
        resolve(value);
      };
      const check = () => {
        const value = attempt();
        if (value) finish(value);
      };
      const observer = new MutationObserver(check);
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
      const poll = setInterval(check, 100);
      const timer = setTimeout(() => finish(null), timeout);
    });

  // -- storage, before any page script runs ---------------------------------------------

  // The fork's collapsed Dots list removes the rows Dotdeck presses.
  try {
    window.localStorage.setItem('opendots_dots_collapsed', 'false');
  } catch (_) {
    // Storage disabled: the list is expanded on demand.
  }

  let scheme = config.theme;
  const applyTheme = (live) => {
    try {
      if (scheme === 'light' || scheme === 'dark') window.localStorage.setItem(THEME_KEY, scheme);
      else window.localStorage.removeItem(THEME_KEY);
    } catch (_) {
      // The page then follows the system for this visit.
    }
    // At document start the page's theme.js reads the key itself; later the theme is
    // applied the way src/client/theme.ts does, on pages that use it.
    const root = html();
    if (!live || !root || !root.hasAttribute('data-theme')) return;
    const dark =
      scheme === 'dark' ||
      (scheme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
    const meta = $('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#1b1b1b' : '#f8f7f4');
  };
  applyTheme(false);

  // -- offline ---------------------------------------------------------------------------

  // While Dotdeck's own banner says the server is out of reach, the page's error bar
  // ("Load failed") would say it again, in English: focus.css hides it under this mark.
  const OFFLINE = 'data-dotdeck-offline';
  let offline = Boolean(config.offline);
  const applyOffline = () => {
    const root = html();
    if (root && offline !== root.hasAttribute(OFFLINE)) {
      if (offline) root.setAttribute(OFFLINE, '');
      else root.removeAttribute(OFFLINE);
    }
  };
  applyOffline();

  // -- focus mode ----------------------------------------------------------------------

  let focusWanted = Boolean(config.focus);
  let recognized = false;
  let settled = false;
  let lossTimer = 0;

  // The OpenDots web client: its rail, its sidebar and the list of Dots.
  const appShown = () =>
    Boolean($('nav.icon-rail') && document.getElementById('workspace-sidebar') && ($(DOTS) || $(EXPAND_DOTS)));
  // Its unlock and loading screens (no token yet, or no Dots yet): there is no
  // navigation to hide, but the page is OpenDots and Dotdeck's own sidebar stays.
  const shellShown = () => Boolean($('main.unlock > span.mascot > img.dot-body'));
  const recognize = () => appShown() || shellShown();

  const applyFocus = () => {
    const root = html();
    const on = focusWanted && recognized;
    if (root && on !== root.hasAttribute(FOCUS)) {
      if (on) root.setAttribute(FOCUS, '');
      else root.removeAttribute(FOCUS);
    }
    return on;
  };
  const reportFocus = (reason) => post({ type: 'focus', active: applyFocus(), recognized, reason });

  const probeTimer = setTimeout(() => {
    if (settled) return;
    settled = true;
    reportFocus('probe');
  }, config.probeTimeout || 8000);

  // Runs inside the MutationObserver callback, before the page paints, so the page's
  // own navigation never flashes once the app renders.
  const checkRecognized = () => {
    if (recognize()) {
      if (lossTimer) {
        clearTimeout(lossTimer);
        lossTimer = 0;
      }
      if (!recognized || !settled) {
        const first = !settled;
        recognized = true;
        settled = true;
        clearTimeout(probeTimer);
        reportFocus(first ? 'probe' : 'change');
      }
    } else if (recognized && !lossTimer) {
      // The page is no longer OpenDots as Dotdeck knows it (another layout took over).
      lossTimer = setTimeout(() => {
        lossTimer = 0;
        if (recognize()) return;
        recognized = false;
        reportFocus('change');
      }, 1000);
    }
  };

  // -- what the page shows -------------------------------------------------------------

  const breadcrumb = () => text($('.template-app header.topbar .breadcrumbs strong'));
  const dotRows = () => $$(`${DOTS} .dot-nav-row`);
  const rowButton = (row) => $('button.dot-nav', row);
  const rowName = (row) => text($('button.dot-nav > span:last-of-type', row));
  const activeDotName = () => text($(`${DOTS} button.dot-nav.active > span:last-of-type`));
  const BOTTOM_VIEWS = ['tasks', 'memories'];
  // The page's breadcrumb words (quoted: every word of the page used here is a string,
  // which tests/test_contract.py finds and checks against contract.json).
  const CRUMB_VIEWS = { 'Activity': 'tasks', 'Memories': 'memories', 'Pages': 'space' };

  const currentSelection = () => {
    if (!$('.template-app')) return null;
    const chat = $('.template-app .chat-workspace');
    if (chat) {
      const persona = $('.chat-persona strong', chat) || $('.empty-chat-persona h2', chat);
      const dot = breadcrumb() || text(persona) || activeDotName();
      return { dot, view: $('.new-conversation', chat) ? 'home' : 'chat' };
    }
    const bottom = $$('.template-app .sidebar-bottom > button.nav-item');
    const active = bottom.findIndex((button) => button.classList.contains('active'));
    if (BOTTOM_VIEWS[active]) return { dot: '', view: BOTTOM_VIEWS[active] };
    if (/^#\/spaces\//.test(window.location.hash)) return { dot: '', view: 'space' };
    const view = CRUMB_VIEWS[breadcrumb()];
    return view ? { dot: '', view } : null;
  };

  const openDotState = () => {
    const mascot =
      $('.template-app .chat-workspace .chat-persona .mascot') ||
      $('.template-app .chat-workspace .empty-chat-persona .mascot');
    if (!mascot) return '';
    return ['working', 'paused'].find((state) => mascot.classList.contains(state)) || 'idle';
  };

  let lastSelection = '';
  let lastState = '';
  let selectionTimer = 0;
  const reportSelection = () => {
    if (selectionTimer) clearTimeout(selectionTimer);
    selectionTimer = 0;
    const selection = currentSelection();
    if (!selection) return;
    const key = `${selection.view}\n${selection.dot}`;
    if (key !== lastSelection) {
      lastSelection = key;
      post({ type: 'selection', dot: selection.dot, view: selection.view });
    }
    if (selection.view !== 'chat' && selection.view !== 'home') return;
    const state = openDotState();
    const stateKey = `${selection.dot}\n${state}`;
    if (state && stateKey !== lastState) {
      lastState = stateKey;
      post({ type: 'dot-state', dot: selection.dot, state });
    }
  };
  const scheduleSelection = () => {
    if (!selectionTimer) selectionTimer = setTimeout(reportSelection, 150);
  };

  // -- pets ------------------------------------------------------------------------------

  let pets = new Map(Object.entries(config.pets || {}));
  let petsOn = Boolean(config.petsEnabled);
  let stamped = false;
  const brandPet = config.brandPet || 'sky';
  // Mascots that name the Dot around them; the others say "<Name> is <state>" in alt.
  const PERSONAS = [
    ['.chat-persona', 'strong'],
    ['.empty-chat-persona', 'h2'],
    ['.call-persona', 'h2'],
  ];

  const petOf = (mascot) => {
    const row = mascot.closest('.dot-nav-row');
    if (row) return pets.get(rowName(row));
    for (const [persona, title] of PERSONAS) {
      const scope = mascot.closest(persona);
      if (scope) return pets.get(text($(title, scope)));
    }
    // The loading and unlock screens and empty states show a mascot of no Dot.
    if (mascot.closest('main.unlock, .large-empty')) return brandPet;
    const image = $('img.dot-body', mascot);
    const alt = (image && image.getAttribute('alt')) || '';
    const cut = alt.lastIndexOf(' is ');
    const name = cut > 0 ? alt.slice(0, cut) : '';
    if (pets.has(name)) return pets.get(name);
    return name === 'Dot' ? brandPet : undefined;
  };

  const stampPets = () => {
    for (const mascot of $$('span.mascot')) {
      const pet = petsOn ? petOf(mascot) : undefined;
      if (pet) {
        if (mascot.getAttribute(PET) !== pet) mascot.setAttribute(PET, pet);
      } else if (mascot.hasAttribute(PET)) {
        mascot.removeAttribute(PET);
      }
    }
    stamped = petsOn;
  };

  // -- the Dot's job line ----------------------------------------------------------------

  // The owner's Dots start their instructions with "# Name — job"; the home card shows
  // that job (focus.css) instead of the raw instructions.
  const SUMMARY = 'data-dotdeck-summary';
  const HEADER = /^#*\s*(.+?)\s+[\u2014\u2013-]\s+(.+)$/;
  const jobOf = (instructions, name) => {
    const first = instructions.split('\n').map((line) => line.trim()).find(Boolean) || '';
    const match = first.startsWith('#') ? HEADER.exec(first) : null;
    if (!match || match[1].trim().toLocaleLowerCase() !== name.toLocaleLowerCase()) return '';
    return match[2].trim();
  };
  const stampSummaries = () => {
    for (const card of $$('.template-app .empty-chat-persona')) {
      const paragraph = $(':scope > p', card);
      if (!paragraph) continue;
      const job = jobOf(paragraph.textContent || '', text($(':scope > h2', card)));
      if (job) {
        if (paragraph.getAttribute(SUMMARY) !== job) paragraph.setAttribute(SUMMARY, job);
      } else if (paragraph.hasAttribute(SUMMARY)) {
        paragraph.removeAttribute(SUMMARY);
      }
    }
  };

  // -- the message boxes -----------------------------------------------------------------

  // The page's message boxes keep the height of one line (the chat's) or of two (a new
  // chat's) whatever is typed. In focus mode they grow with the text, line by line, up to
  // the cap focus.css sets on the boxes marked here, and scroll past it, as chat apps do.
  // The conversation above keeps its place: at its end it stays at its end.
  const GROW = 'data-dotdeck-grow';
  const COMPOSERS =
    '.template-app textarea[aria-label="Message your Dot"], .template-app textarea[aria-label="Start a conversation"]';
  const boxes = new Map(); // box -> { value, width } it was last fitted to

  const fitBox = (box) => {
    const transcript = $('.chat-transcript', box.closest('.live-chat') || box.parentNode);
    const top = transcript ? transcript.scrollTop : 0;
    const atEnd = transcript && transcript.scrollHeight - top - transcript.clientHeight < 4;
    const style = window.getComputedStyle(box);
    const cap = parseFloat(style.maxHeight) || Infinity;
    const borders = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
    box.style.height = 'auto';
    const wanted = box.scrollHeight + borders;
    box.style.height = `${Math.min(wanted, cap)}px`;
    const mode = wanted > cap + 1 ? 'scroll' : '';
    if (box.getAttribute(GROW) !== mode) box.setAttribute(GROW, mode);
    if (transcript) transcript.scrollTop = atEnd ? transcript.scrollHeight : top;
    boxes.set(box, { value: box.value, width: box.clientWidth });
  };

  // Another width wraps the text again (window resized, computer shown or hidden).
  const boxResized = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const last = boxes.get(entry.target);
      if (last && entry.target.clientWidth !== last.width) fitBox(entry.target);
    }
  });

  const stampBoxes = () => {
    const on = html().hasAttribute(FOCUS);
    const shown = on ? new Set($$(COMPOSERS)) : new Set();
    for (const box of boxes.keys()) {
      if (shown.has(box)) continue;
      boxResized.unobserve(box);
      boxes.delete(box);
      box.removeAttribute(GROW);
      box.style.height = '';
    }
    for (const box of shown) {
      if (!boxes.has(box)) {
        box.setAttribute(GROW, ''); // Measured under focus.css's cap from the start.
        boxResized.observe(box);
      }
      // The page sets the value itself too: emptied once sent, a draft brought back.
      const last = boxes.get(box);
      if (!last || last.value !== box.value) fitBox(box);
    }
  };
  document.addEventListener(
    'input',
    (event) => {
      if (boxes.has(event.target)) fitBox(event.target);
    },
    true,
  );

  // -- watching the page -----------------------------------------------------------------

  const observer = new MutationObserver(() => {
    checkRecognized();
    if (petsOn || stamped) stampPets();
    if (recognized) stampSummaries();
    if (recognized || boxes.size) stampBoxes();
    scheduleSelection();
  });
  observer.observe(document, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['class', 'alt', 'aria-current', 'aria-label', 'disabled'],
  });
  window.addEventListener('hashchange', scheduleSelection);

  // -- commands --------------------------------------------------------------------------

  const appReady = () => waitFor(() => $('.template-app'), 10000);

  const dotsList = async () => {
    if (!(await appReady())) return null;
    if (!$(DOTS)) {
      const expand = $(EXPAND_DOTS);
      if (expand) expand.click();
    }
    return waitFor(() => (dotRows().length ? dotRows() : null));
  };

  const findRow = (rows, index, name) => {
    if (Number.isInteger(index) && index >= 0 && index < rows.length) {
      if (!name || rowName(rows[index]) === name) return rows[index];
    }
    if (!name) return null;
    const folded = name.toLocaleLowerCase();
    return (
      rows.find((row) => rowName(row) === name) ||
      rows.find((row) => rowName(row).toLocaleLowerCase() === folded) ||
      null
    );
  };

  const showingDot = (name) => {
    const selection = currentSelection();
    return Boolean(selection && (selection.view === 'chat' || selection.view === 'home') && selection.dot === name);
  };
  // The row at this place (names can repeat) is the open Dot, and the page shows it.
  const showingRow = (position, name) => {
    const row = dotRows()[position];
    const button = row && rowButton(row);
    return Boolean(button && button.classList.contains('active') && rowName(row) === name && showingDot(name));
  };
  const inCall = () => Boolean($('.template-app section.call-view'));

  const openDot = async (index, name) => {
    const rows = await dotsList();
    if (!rows) return false;
    const row = findRow(rows, index, name);
    if (!row) return false;
    const target = rowName(row);
    const position = rows.indexOf(row);
    // Pressing the open Dot again would switch to its latest conversation, and leaving
    // the conversation of a call ends the call.
    if (inCall() && showingRow(position, target)) return true;
    rowButton(row).click();
    // The same row (by place, as names can repeat) is the active one, and the page shows it.
    return Boolean(await waitFor(() => showingRow(position, target)));
  };

  // React keeps each component's fiber on its DOM nodes. ThreadList's props hold
  // onSelect(id), and every thread button's fiber carries the conversation id as key.
  const fiberOf = (node) => {
    const key = node && Object.keys(node).find((name) => name.startsWith('__reactFiber$'));
    return key ? node[key] : null;
  };
  const threadListProps = (list) => {
    let fiber = fiberOf(list);
    for (let depth = 0; fiber && depth < 8; depth += 1, fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (props && typeof props.onSelect === 'function' && Array.isArray(props.local)) return props;
    }
    return null;
  };
  const threadButtons = (list) => (list ? $$(':scope > button.nav-item', list) : []);
  const openThreadId = () => {
    const list = $('.template-app section.thread-list');
    const button = list && $(':scope > button.nav-item.active', list);
    const fiber = fiberOf(button);
    return fiber && fiber.key != null ? String(fiber.key) : '';
  };

  const workspaceConversations = async () => {
    try {
      const token = window.sessionStorage.getItem(config.storageKey);
      const response = await fetch('/api/workspace', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        cache: 'no-store',
      });
      if (!response.ok) return null;
      const workspace = await response.json();
      return Array.isArray(workspace.conversations) ? workspace.conversations : null;
    } catch (_) {
      return null;
    }
  };

  // Elements are looked up again at every step: the page may render them anew.
  const threadList = () => $('.template-app section.thread-list');
  const liveChat = () => $('.template-app .chat-workspace .live-chat');

  // The page lists every conversation (of all Dots) and picks the Dot by itself, so
  // the thread is selected directly: no detour through the Dot's latest conversation.
  const selectThread = async (id) => {
    if (!(await waitFor(threadList))) return false;
    const props = threadListProps(threadList());
    if (props) {
      const known = () => {
        const current = threadListProps(threadList());
        return current && current.local.some((item) => item && item.id === id) ? current : null;
      };
      let current = known();
      if (!current) {
        // A conversation created a moment ago appears at the page's next refresh (3 s);
        // one the server does not have never will.
        const conversations = await workspaceConversations();
        if (conversations && !conversations.some((item) => item && item.id === id)) return false;
        current = await waitFor(known, 8000);
      }
      if (!current) return false;
      current.onSelect(id);
      return Boolean(await waitFor(() => openThreadId() === id && liveChat()));
    }

    // No React internals to read: click the thread at its place in the workspace list,
    // which the page renders in the server's order.
    const conversations = await workspaceConversations();
    const position = conversations ? conversations.findIndex((item) => item && item.id === id) : -1;
    if (position < 0) return false;
    const buttons = await waitFor(() => {
      const found = threadButtons(threadList());
      return found.length === conversations.length ? found : null;
    }, 8000);
    if (!buttons) return false;
    buttons[position].click();
    return Boolean(
      await waitFor(() => {
        const button = threadButtons(threadList())[position];
        return button && button.classList.contains('active') && liveChat();
      }),
    );
  };

  const openConversation = async (id, index, name) => {
    if (!id || !(await appReady())) return false;
    if (openThreadId() === id && liveChat()) return true;
    if (await selectThread(id)) return true;
    // Unknown to the page: at least show its Dot, which the native side selected.
    if (name || Number.isInteger(index)) await openDot(index, name);
    return false;
  };

  const newChat = async (index, name) => {
    const rows = await dotsList();
    const row = rows && findRow(rows, index, name);
    if (!row) return false;
    const target = rowName(row);
    // The open Dot goes straight to its empty composer, without loading its latest chat.
    if (!showingRow(rows.indexOf(row), target) && !(await openDot(index, name))) return false;
    const home = $('.template-app nav.icon-rail button.rail-brand') || $('#workspace-sidebar button.wordmark');
    if (!home) return false;
    home.click();
    const ready = await waitFor(() => showingDot(target) && $('.template-app .chat-workspace .new-conversation'));
    const composer = ready && $('textarea[aria-label="Start a conversation"]', ready);
    if (composer && !composer.disabled) composer.focus();
    return Boolean(ready);
  };

  const showing = (view) => {
    const selection = currentSelection();
    return Boolean(selection && selection.view === view);
  };

  const openSpace = async (spaceId, pageId) => {
    const valid = (value) => typeof value === 'string' && value && !/[/#?]/.test(value);
    if (!valid(spaceId) || (pageId && !valid(pageId)) || !(await appReady())) return false;
    const hash = `#/spaces/${spaceId}${pageId ? `/pages/${pageId}` : ''}`;
    // The way src/client/page-navigation.ts opens a page link.
    if (window.location.hash === hash) window.dispatchEvent(new HashChangeEvent('hashchange'));
    else window.location.hash = hash;
    return Boolean(await waitFor(() => window.location.hash === hash && showing('space')));
  };

  const bottomButton = (label, position) => {
    const buttons = $$('.template-app .sidebar-bottom > button.nav-item');
    return buttons.find((button) => text($(':scope > span', button)) === label) || buttons[position];
  };

  const openActivity = async () => {
    if (!(await appReady())) return false;
    const button = $('.template-app nav.icon-rail button[aria-label="Open activity"]') || bottomButton('Scheduled & activity', 0);
    if (!button) return false;
    button.click();
    return Boolean(await waitFor(() => showing('tasks')));
  };

  const openMemories = async () => {
    if (!(await appReady())) return false;
    const button = bottomButton('Memories', 1);
    if (!button) return false;
    button.click();
    return Boolean(await waitFor(() => showing('memories')));
  };

  const dialogOpen = () => $('.template-app section.modal[role="dialog"], section.modal[role="dialog"]');

  const openWebSettings = async () => {
    if (!(await appReady())) return false;
    const button = $('.template-app button.rail-settings') || $('button[aria-label="Open settings"]');
    if (!button) return false;
    button.click();
    return Boolean(await waitFor(dialogOpen));
  };

  const editDot = async (name) => {
    const rows = await dotsList();
    if (!rows || !name) return false;
    const label = `Edit ${name} settings`;
    const button = $$(`${DOTS} button.dot-settings`).find((item) => item.getAttribute('aria-label') === label);
    if (!button) return false;
    button.click();
    return Boolean(await waitFor(dialogOpen));
  };

  const COMPUTER = '.template-app button[aria-label="Show computer"], .template-app button[aria-label="Hide computer"]';
  const computerShown = () => {
    const button = $(COMPUTER);
    return button ? button.getAttribute('aria-expanded') : null;
  };

  const toggleComputer = async () => {
    if (!(await appReady())) return false;
    const before = computerShown();
    if (before === null) return false;
    $(COMPUTER).click();
    return Boolean(await waitFor(() => computerShown() !== null && computerShown() !== before));
  };

  const CALL_BUTTON = '.template-app .chat-workspace .chat-persona-actions button';
  const endButton = () =>
    $('.template-app section.call-view .call-end') || $(`${CALL_BUTTON}[aria-label="End voice call"]`);

  const startCall = async (conversationId, index, name) => {
    const opened = conversationId ? await openConversation(conversationId, index, name) : await openDot(index, name);
    if (!opened) return false;
    // A Dot without conversations shows its empty composer, which has no call button.
    const shown = await waitFor(() => liveChat() || $('.template-app .chat-workspace .new-conversation'));
    if (!shown || !shown.classList.contains('live-chat')) return false;
    if (endButton()) return true;
    // Enabled once voice is set up, the Dots are not paused and the chat has loaded.
    const start = await waitFor(() => {
      const button = $(`${CALL_BUTTON}[aria-label="Start voice call"]`);
      return button && !button.disabled ? button : null;
    }, 10000);
    if (!start) return false;
    start.click();
    return Boolean(await waitFor(() => endButton() || $('.template-app section.call-view'), 3000));
  };

  const endCall = async () => {
    const button = endButton();
    if (!button) return false;
    if (button.disabled) return true; // Already hanging up.
    button.click();
    return Boolean(
      await waitFor(() => {
        const current = endButton();
        return !current || current.disabled || !$('.template-app section.call-view');
      }, 3000),
    );
  };

  const commands = {
    openDot,
    openConversation,
    newChat,
    openSpace,
    openActivity,
    openMemories,
    openWebSettings,
    editDot,
    toggleComputer,
    startCall,
    endCall,
  };

  // One command at a time, in the order Dotdeck sent them.
  let queue = Promise.resolve(false);
  const run = (name, args) => {
    const command = Object.prototype.hasOwnProperty.call(commands, name) ? commands[name] : null;
    if (!command) return Promise.resolve(false);
    const result = queue
      .then(() => command(...(Array.isArray(args) ? args : [])))
      .then(
        (value) => Boolean(value),
        () => false,
      )
      .then(async (value) => {
        await sleep(0);
        reportSelection();
        return value;
      });
    queue = result;
    return result;
  };

  Object.defineProperty(window, '__dotdeck', {
    value: Object.freeze({
      run,
      setFocus(enabled) {
        focusWanted = Boolean(enabled);
        const wasSettled = settled;
        checkRecognized();
        // Before the probe settles the answer is not known yet: the probe gives it.
        if (wasSettled) reportFocus('set');
        const active = applyFocus();
        stampBoxes();
        return { active, recognized, settled };
      },
      setTheme(value) {
        scheme = value;
        applyTheme(true);
        return true;
      },
      setOffline(value) {
        offline = Boolean(value);
        applyOffline();
        return true;
      },
      setPets(map, enabled) {
        pets = new Map(Object.entries(map || {}));
        petsOn = Boolean(enabled);
        stampPets();
        return true;
      },
      selection() {
        lastSelection = '';
        lastState = '';
        reportSelection();
        return currentSelection();
      },
    }),
  });
})();
