// ハコペン 通知用（画面のデータは保存しないので、更新はいつもすぐ反映されます）2026/11/12
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'ハコペン', body: e.data && e.data.text() }; }
  // 2026/11/14 アプリを開いていなくても、アイコンにトークの未読数を出す
  if (typeof d.badge === 'number' && self.navigator && self.navigator.setAppBadge) {
    try { d.badge > 0 ? self.navigator.setAppBadge(d.badge) : self.navigator.clearAppBadge(); } catch (_) {}
  }
  e.waitUntil(self.registration.showNotification(d.title || 'ハコペン', {
    body: d.body || '', tag: d.tag || undefined, icon: 'icon-180.png', badge: 'icon-180.png', data: { tab: d.tab || '' } }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const tab = (e.notification.data || {}).tab || '';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cs => {
    for (const c of cs) { c.postMessage({ tab }); return c.focus(); }
    return self.clients.openWindow('./' + (tab ? '#' + tab : ''));
  }));
});
