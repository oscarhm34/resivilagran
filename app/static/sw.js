// La Vila Gran — Service Worker (cache-first for app shell)
const CACHE_NAME = 'lavilagran-v4';
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

function mostrarAviso(data) {
    return self.registration.showNotification(data.title || 'La Vila Gran', {
        body: data.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        data: { url: data.url || '/worker' },
        // El patron del tono que ha elegido ella, que viene en el aviso. Con la
        // aplicacion cerrada el sonido lo elige Android y la web no puede
        // cambiarlo: la vibracion es lo unico que distingue un tono de otro.
        vibrate: data.vibrate || [200, 100, 200],
        tag: data.tag || 'lavilagran',
        renotify: true,
        // El aviso se queda hasta que lo tocan. Sin esto Android lo retira
        // solo a los pocos segundos, y quien esta atendiendo a un residente
        // con las manos ocupadas no llega a tiempo de mirar el movil.
        requireInteraction: true,
    });
}

self.addEventListener('push', function(event) {
    var data = { title: 'La Vila Gran', body: 'Nueva notificación', url: '/worker' };
    try {
        if (event.data) data = event.data.json();
    } catch(e) {}

    // El aviso de prueba sale siempre. Existe justo para comprobar que la
    // notificacion del sistema aparece, asi que esconderla porque la aplicacion
    // esta abierta lo dejaria sin comprobar nada.
    if (data.siempre_visible) {
        event.waitUntil(mostrarAviso(data));
        return;
    }

    // Con la aplicacion delante sonaban las dos cosas: el tono de la propia
    // pagina y ademas la notificacion de Android. Si hay una ventana visible se
    // le pasa el aviso y toca ella el tono elegido; el navegador no penaliza
    // `userVisibleOnly` mientras haya una ventana a la vista.
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then(function(clientList) {
                var visible = clientList.filter(function(c) {
                    return c.visibilityState === 'visible' && c.url.includes('/worker');
                })[0];
                if (!visible) return mostrarAviso(data);
                try {
                    visible.postMessage({ type: 'chat-push', data: data });
                } catch (e) {
                    return mostrarAviso(data);
                }
            })
            .catch(function() { return mostrarAviso(data); })
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
