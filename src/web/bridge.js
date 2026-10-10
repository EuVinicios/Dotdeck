// Dotdeck: hands the owner token from the keyring to the OpenDots web client.
//
// The web client reads its token from sessionStorage when it loads
// (src/client/api.ts upstream), so it is placed there before any page script runs.
// When the user unlocks the page with a different token, Dotdeck is told so it can
// verify it and keep it in the keyring.
//
// SPDX-License-Identifier: GPL-3.0-or-later
(() => {
  'use strict';
  const config = __DOTDECK_CONFIG__;
  if (window.location.origin !== config.origin) return;
  const post = (message) => {
    try {
      window.webkit.messageHandlers.dotdeck.postMessage(JSON.stringify(message));
    } catch (_) {
      // Dotdeck is gone (page unloading).
    }
  };
  try {
    if (config.token) window.sessionStorage.setItem(config.storageKey, config.token);
  } catch (_) {
    // Storage disabled: the page will ask for the token itself.
  }
  try {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      setItem.call(this, key, value);
      if (this === window.sessionStorage && key === config.storageKey && value && value !== config.token) {
        config.token = String(value);
        post({ type: 'token', token: config.token });
      }
    };
  } catch (_) {
    // Not fatal: the token simply is not remembered.
  }
})();
