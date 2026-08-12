// La Vila Gran — Service Worker (cache-first for app shell)
const CACHE_NAME = 'lavilagran-v1';
const SHELL_URLS = [
    '/worker',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css',
    'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap',
];

self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.addAll(SHELL_URLS);
        })
    );
    self.skipWaiting();
});

self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(
                keys.filter(function(k) { return k !== CACHE_NAME; })
                    .map(function(k) { return caches.delete(k); })
            );
        })
    );
    self.clients.claim();
});

self.addEventListener('fetch', function(event) {
    var url = new URL(event.request.url);

    // API calls: network-only (never cache)
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) {
        return;
    }

    // App shell: cache-first, fallback to network
    event.respondWith(
        caches.match(event.request).then(function(cached) {
            if (cached) {
                // Update cache in background
                fetch(event.request).then(function(response) {
                    if (response && response.status === 200) {
                        caches.open(CACHE_NAME).then(function(cache) {
                            cache.put(event.request, response);
                        });
                    }
                }).catch(function() {});
                return cached;
            }
            return fetch(event.request);
        })
    );
});
