# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite，供清障队在网页上查看并安排清障。

## 修复了什么

旧版 Service Worker 有两个缺陷：在线刷新遇到 **503 时错误页会被写入缓存**，
随后断网刷新看到的就是报错页；换版时新旧脚本/页面还可能**跨版本混用**。

现在的离线策略是**整版本原子切换**：

- 页面、脚本、样式、随附样照同属一个版本，Service Worker 在 `install` 阶段
  把这四件**整套并发取回，且只接受 2xx**；任何一个请求报错、返回 503 或文件没取齐，
  本次安装整体失败，不写任何缓存，浏览器继续使用**上一套完整副本**。
- 整套取齐后才落盘为版本化缓存（`forest-patrol-shell-v<N>`），缓存中永远只有完整版本，
  不存在半成品，也不会新旧混搭。
- 新 SW 安装成功后**停在 waiting**，不抢占正在运行的页面；下一次刷新（或用户点
  “刷新启用新版”）才激活，激活时只清理旧版本缓存。IndexedDB 里的待传记录不受影响。
- 运行时对导航和静态资源一律**缓存优先**；仅缓存 2xx 响应，503 等错误永远不入库，
  因此“先在线遇到 503、再断网刷新”拿到的仍是上一套完整可用的采集页。
- 补传接口 `POST /api/records` 完全不变；待传记录（含原照 Blob）存在 IndexedDB，
  只有收到服务器确认才删除，UUID 幂等保证重传不重复入库。

## 启动（Docker Compose）

```bash
docker compose up --build -d
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。
页面顶部显示当前外壳版本徽标。SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。

辅助接口：

- `GET /version` — 当前服务版本与故障注入开关
- `GET /healthz` — 健康检查（compose 健康探测用）

## 浏览器演示步骤

以下步骤均可在 Chrome/Edge 中完成；DevTools 用 F12 打开，网络面板可勾选 **Offline**，
Application → Cache Storage 可直接查看版本化缓存内容。

### 0. 准备：首次在线打开

```bash
docker compose up --build -d          # 默认 APP_VERSION=1
```

打开 `http://localhost:8000`，顶部徽标显示 **外壳版本 v1**。
在 Application → Service Workers 看到 SW 已激活，Cache Storage 里出现
`forest-patrol-shell-v1`，含 `/`、`/static/app.js`、`/static/style.css`、
`/static/sample-photo.png` 四个条目。

### 1. 在线 503 → 再断网刷新，仍是完整采集页（不再出现报错页）

1. DevTools → Network，把节流改为 **Offline 之外**保持在线；也可直接用下面的故障部署
   制造 503：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.fail.yml up -d
   curl -i http://localhost:8000/static/app.js   # 稳定返回 503
   ```
2. 刷新页面：因为 SW 缓存优先，页面照常显示 v1 采集页（503 不会污染缓存，
   Cache Storage 中仍只有完整的 v1 四件套）。
3. DevTools → Network 勾选 **Offline**（或 `docker compose stop`），再次刷新：
   打开的仍是完整采集页，不是报错页。

### 2. 断网录入坐标、保存原照

1. 保持 Offline，刷新确认页面可用。
2. 填写纬度/经度（如 `47.735000` / `128.912000`），点 **🖼️ 使用随附样例照片**
   （样照与页面同版本、已整包缓存，离线可取），再点 **💾 保存到待传记录**。
3. 待传列表出现 1 笔；再刷新一次页面，记录仍在（IndexedDB 持久化，含原照 Blob）。
   此时点 **📤 补传** 会提示失败，但记录一条不丢。

### 3. 换版成功：整套取齐后，下一次刷新才用新版

```bash
# 恢复网络后部署 v2（页面/脚本/样式/样照同属 v2 且全部 200）
docker compose -f docker-compose.yml -f docker-compose.v2.yml up -d --build
```

1. 在线刷新页面：浏览器探测到新 SW，后台把 v2 四件套整套取回并安装；
   安装成功后新 SW 进入 waiting，**当前页面仍是 v1**，顶部出现黄色提示条
   “✅ 新版采集页已完整下载，刷新后启用”。此时 Cache Storage 中 v1、v2 并存，均为完整副本。
2. 点 **刷新启用新版**（或自然再刷新一次）：新 SW 激活、旧 v1 缓存被清理，
   页面徽标变为 **外壳版本 v2**。Application → Cache Storage 只剩 `forest-patrol-shell-v2`。

### 4. 换版失败：下载途中报错 / 文件没取齐，继续用上一套完整副本

```bash
# 部署 v3，但 /static/app.js 稳定 503（“文件没取齐”）
docker compose -f docker-compose.yml -f docker-compose.fail.yml up -d --build
curl -s http://localhost:8000/version     # app_version=3, shell_fail_paths 含 /static/app.js
```

1. 刷新页面并在 DevTools 中观察：新 SW 安装因 503 失败（Console 有
   “外壳资源 … 不可用，放弃本版本”），Cache Storage **不会出现 v3 缓存**，
   页面继续由 v2 完整副本提供。
2. 模拟“下载途中服务换版、随机报错”：在 compose 覆盖文件里改用
   `SHELL_FAIL_RATE: "0.5"`（外壳每个请求 50% 概率 503），重新 `up -d`，
   结果相同——只要四件没整套取齐，就一直用旧的完整副本，采集不中断。
3. 恢复健康部署后（`docker compose -f docker-compose.yml -f docker-compose.v2.yml up -d`），
   下一次在线刷新才会重新下载并切换。

### 5. 回网补传（接口不变，确认才删除，幂等不重复）

1. 取消 Offline（或 `docker compose start`）。待传记录还在，点 **📤 补传全部待传记录**；
   也可什么都不做——浏览器监听到 `online` 事件会自动补传一次。
2. 每笔收到服务器 `{"ok": true}` 确认后才从待传列表移除。
3. 打开 <http://localhost:8000/records>（清障队查看页），可见已入库的坐标与原照。
4. **验证幂等**：若补传途中服务器已写入但确认丢失，该笔仍留在待传；再次补传时
   服务器按 UUID 判重返回 `{"ok": true, "duplicate": true}`，浏览器正常移除待传，
   SQLite 中始终只有一行、照片不会重复落盘。

## 故障注入开关（仅演示用，默认全关）

均为容器环境变量，在 compose 覆盖文件中配置：

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `APP_VERSION` | 当前部署版本，注入页面徽标与 SW 缓存名 | `2` |
| `SHELL_FAIL_PATHS` | 命中即返回 503 的外壳路径（逗号分隔） | `/static/app.js` |
| `SHELL_FAIL_RATE` | 外壳请求随机 503 概率（0~1） | `0.5` |

故障只作用于外壳 GET（`/`、`/static/app.js`、`/static/style.css`、
`/static/sample-photo.png`），不影响补传 API、已入库数据与照片。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data APP_VERSION=1 python3 app/app.py
# 换版/失败演示：
APP_VERSION=2 SHELL_FAIL_PATHS=/static/app.js DATA_DIR=./data python3 app/app.py
```

## 目录结构

```
├── docker-compose.yml        # 一键启动（含版本/故障环境变量与健康检查）
├── docker-compose.v2.yml     # 演示覆盖：换版成功（v2 完整）
├── docker-compose.fail.yml   # 演示覆盖：换版失败（v3 + 503）
├── Dockerfile                # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py                # Flask 路由 + SQLite（uuid 唯一约束幂等）+ 版本/故障注入
│   ├── templates/
│   │   ├── index.html        # 采集页（巡护员，含版本徽标与新版提示条）
│   │   ├── records.html      # 已入库记录页（清障队）
│   │   └── sw.js             # Service Worker 模板（版本号服务端渲染）
│   └── static/
│       ├── app.js            # IndexedDB 待传队列 + 补传 + SW 更新流程
│       ├── style.css
│       └── sample-photo.png  # 随附样例照片（与页面同版本整包缓存）
└── tools/
    └── make_sample_photo.py  # 样例照片生成脚本（纯标准库）
```

## API（补传接口保持不变）

- `GET  /api/records` — 已入库记录 JSON 列表
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `GET  /photos/<file>` — 已入库原照
