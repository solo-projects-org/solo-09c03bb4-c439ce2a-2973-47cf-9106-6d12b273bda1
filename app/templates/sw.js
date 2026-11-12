/* Service Worker：离线采集外壳的“整版本原子切换”
 *
 * 设计要点（对应修复需求）：
 * 1. 同一版本的页面 / 脚本 / 样式 / 随附样照组成一个“完整副本”，
 *    install 阶段一次性全部取回，且只接受 2xx 响应；
 *    任一文件缺失、请求报错或服务换版途中返回 5xx，整体安装即失败，
 *    浏览器丢弃本 SW，旧 SW 与旧缓存（上一套完整副本）继续服务。
 * 2. 所有响应先在内存中取回并校验通过，才创建版本化正式缓存并整包写入，
 *    因此任何时刻 Cache Storage 里只有“完整的版本”，绝不出现新旧混搭。
 * 3. 不调用 skipWaiting()：新 SW 安装成功后进入 waiting，
 *    等下一次刷新（旧客户端释放）才 activate；activate 只删除本前缀的旧缓存。
 *    IndexedDB 待传记录不在 Cache Storage 内，换版不会丢。
 * 4. fetch 一律“缓存优先”，网络回退拿到的响应只在 2xx 时才补缓存——
 *    503 等错误页永远不会污染缓存，断网刷新拿到的始终是上一套完整副本。
 *
 * 本文件由 Flask 以 Jinja 模板渲染，APP_VERSION 与页面版本同源，
 * 部署换版时缓存名随之改变，浏览器据此判定有新 SW。
 */
"use strict";

const APP_VERSION = "{{ app_version }}";
const CACHE_PREFIX = "forest-patrol-shell-v";
const CACHE_NAME = CACHE_PREFIX + APP_VERSION;
/* 兼容清理旧实现遗留的缓存名 */
const LEGACY_CACHE = "forest-patrol-v1";

/* 同属一版的完整外壳：页面、脚本、样式、随附样照，缺一不可 */
const SHELL = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/sample-photo.png",
];

/* 只有 2xx 的正常响应才算“取到且可用”；opaque/错误响应一律拒收 */
function isCacheable(resp) {
  return !!resp && resp.ok && resp.type !== "opaque";
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      /* 第一步：并发拉取整套外壳并校验。
       * 任何一个请求网络报错或返回非 2xx，Promise.all reject，
       * 本次安装失败——不写任何正式缓存，旧完整副本原样保留。 */
      const entries = await Promise.all(
        SHELL.map(async (url) => {
          const resp = await fetch(url, { cache: "no-store" });
          if (!isCacheable(resp)) {
            throw new Error(
              "外壳资源 " + url + " 不可用（HTTP " + resp.status + "），放弃本版本"
            );
          }
          return [url, resp];
        })
      );

      /* 第二步：整套已验证完整，才落盘为版本化缓存（先清同名残留再写） */
      if (await caches.has(CACHE_NAME)) await caches.delete(CACHE_NAME);
      const cache = await caches.open(CACHE_NAME);
      await Promise.all(entries.map(([url, resp]) => cache.put(url, resp)));
    })()
    /* 故意不 skipWaiting：安装成功也停在 waiting，下一次刷新才接管 */
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      /* 只清理本应用的旧版本缓存；其他存储（IndexedDB 待传记录等）一概不动 */
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter(
            (k) =>
              k !== CACHE_NAME &&
              (k.startsWith(CACHE_PREFIX) || k === LEGACY_CACHE)
          )
          .map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

/* 页面在用户点“刷新启用新版”后可放行 waiting 的新 SW */
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  /* 只接管本站 GET；POST（补传接口）直接走网络，离线时由页面保留待传 */
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  /* 已入库数据与原照必须实时，不进缓存 */
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/photos/")) return;

  /* 导航与静态资源统一“缓存优先”：
   * - 命中缓存：永远是某一整套完整副本中的同版本文件，绝不混版
   * - 未命中（如首次访问即断网）：回退网络，且仅 2xx 才补缓存，
   *   503 / 断网错误不会污染或覆盖旧副本，错误页只在本次请求中出现 */
  event.respondWith(
    caches.match(req, { cacheName: CACHE_NAME }).then(
      (hit) =>
        hit ||
        fetch(req).then((resp) => {
          if (isCacheable(resp) && req.mode === "navigate") {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          }
          return resp;
        })
    )
  );
});
