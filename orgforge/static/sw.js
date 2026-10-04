// Lets browsers install OrgForge as an app. Nothing is cached: the dashboard always shows live data.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
