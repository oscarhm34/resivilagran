// La Vila Gran — Service Worker (cache-first for app shell)
const CACHE_NAME = 'lavilagran-v3';
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

// ── Push Notifications ──
self.addEventListener('push', function(event) {
    var data = { title: 'La Vila Gran', body: 'Nueva notificación', url: '/worker' };
    try {
        if (event.data) data = event.data.json();
    } catch(e) {}

    event.waitUntil(
        self.registration.showNotification(data.title || 'La Vila Gran', {
            body: data.body || '',
            icon: '/static/icon-192.png',
            badge: '/static/icon-192.png',
            data: { url: data.url || '/worker' },
            vibrate: [200, 100, 200],
            tag: data.tag || 'lavilagran',
            renotify: true,
        })
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    var url = (event.notification.data && event.notification.data.url) || '/worker';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (var c of clientList) {
                if (c.url.includes('/worker') && 'focus' in c) {
                    // La ventana ya esta abierta: el service worker no puede
                    // navegarla, asi que le pasa el destino y lo abre la app.
                    if ('postMessage' in c) c.postMessage({ type: 'deeplink', url: url });
                    return c.focus();
                }
            }
            return clients.openWindow(url);
        })
    );
});

self.addEventListener('fetch', function(event) {
    var url = new URL(event.request.url);

    // Peticiones a la API: siempre de red, nunca de cache.
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) {
        return;
    }

    // La pagina de la webapp va de RED PRIMERO. Servirla de cache dejaba a las
    // trabajadoras ejecutando la version anterior despues de cada despliegue,
    // con botones que no existian todavia, y la copia solo se refrescaba para
    // la carga siguiente: habia que abrir la app dos veces para ver un cambio.
    // La cache se conserva como respaldo para cuando no hay cobertura.
    var esLaPagina = event.request.mode === 'navigate'
        || url.pathname === '/worker' || url.pathname === '/';
    if (esLaPagina) {
        event.respondWith(
            fetch(event.request).then(function(response) {
                if (response && response.status === 200) {
                    var copia = response.clone();
                    caches.open(CACHE_NAME).then(function(cache) {
                        cache.put(event.request, copia);
                    });
                }
                return response;
            }).catch(function() {
                return caches.match(event.request).then(function(cached) {
                    return cached || caches.match('/worker');
                });
            })
        );
        return;
    }

    // El resto (tipografias y CSS de CDN, que no cambian): cache primero.
    event.respondWith(
        caches.match(event.request).then(function(cached) {
            if (cached) {
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
