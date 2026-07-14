/* LitClock Control PWA — service worker registration (M6 D9).
 *
 * Hardware probe 2026-04-30 confirmed iOS Safari at our private-IP origin
 * reports `isSecureContext = false` and `navigator.serviceWorker = undefined`.
 * Chromium-based Android Chrome treats the same origin as trustworthy enough
 * to register a SW. We ship one codebase, branch at runtime via this guard.
 *
 * Anything that fails the guard (iOS, very old Android, browser without SW
 * support) silently falls back to the splash-matrix-only path — no error,
 * no console noise beyond a console.info for diagnostics.
 */
(function () {
  'use strict';

  if (!('serviceWorker' in navigator)) {
    console.info('litclock: service worker unsupported, skipping register');
    return;
  }
  if (!self.isSecureContext) {
    console.info('litclock: not a secure context (iOS Safari at private-IP); skipping SW register');
    return;
  }

  // Register on load so the SW install doesn't compete with first paint.
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js')
      .then(function (reg) {
        console.info('litclock: service worker registered, scope=' + reg.scope);
      })
      .catch(function (err) {
        // Non-fatal — the PWA degrades to network-only, which is the iOS
        // experience anyway. update.sh restart can transiently fail SW
        // install; the next page load retries.
        console.info('litclock: SW register failed: ' + err);
      });
  });
})();
