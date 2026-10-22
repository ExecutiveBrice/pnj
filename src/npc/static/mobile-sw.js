'use strict';

const SHELL_CACHE = 'echo-shell-v1';
const SHELL = ['/', '/mobile-offline.js'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(
    keys.filter(key => key.startsWith('echo-shell-') && key !== SHELL_CACHE).map(key => caches.delete(key))
  )).then(() => self.clients.claim()));
});

// Only the public UI is cached. WAVs are saved explicitly in IndexedDB; no
// WebSocket events, secrets, error responses or partial audio enter this cache.
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.search || !SHELL.includes(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(SHELL_CACHE);
    try {
      const response = await fetch(event.request);
      if (response.status === 200) await cache.put(event.request, response.clone()).catch(() => {});
      return response;
    } catch (error) {
      const saved = await cache.match(url.pathname);
      if (saved) return saved;
      throw error;
    }
  })());
});
