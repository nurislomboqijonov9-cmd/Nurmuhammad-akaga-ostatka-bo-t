const CACHE = 'ombor-v1';
const SHELL = ['/', '/index.html', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API — hech qachon keshlanmaydi (jonli ma'lumot)
  if (url.pathname.startsWith('/api/')) return;
  // Shell — avval kesh, keyin tarmoq
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
