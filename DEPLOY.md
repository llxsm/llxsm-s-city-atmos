# 部署与运维手册（Debian 12 · 单机 · IP 访问）

> 目标形态：一台 Debian 12 云服务器，Nginx 监听 80 是唯一对外入口（带密码），
> 背后两个 Python 服务只监听 `127.0.0.1`，共用 `/var/lib/atmos` 下的数据湖与资产目录。
>
> 每一步都写了**验收**：做完能自己确认对不对，这比"照抄到底再一起报错"有效得多。

---

## 0. 为什么是这个形态

三个约束推导出了唯一解：

1. **没有域名**，所以签不了公网证书，只能 HTTP + 密码；
2. **要密码保护**，而浏览器的 HTTP Basic 凭据**只对同源请求自动携带** ——
   前端如果用两个端口（两个 origin）访问两个后端，跨源请求不会带上 `Authorization`
   头，治理平台的页签会整片 401。所以**必须收敛到一个 origin**；
3. **一个 origin 就只能靠路径分流**：`/` → 分析平台，`/gov/` → 治理平台。

由此带来一件必须顺手修掉的事：`index.html` 里的 `portal-config.js` 原本是绝对路径，
在 `/gov/` 下会打到分析平台。

上线前一共改了六处，都已跑过测试（282 → 331 项全绿）：

| 改动 | 位置 | 原因 |
| --- | --- | --- |
| `/portal-config.js` → `portal-config.js` | `web/app/index.html` | 绝对路径在 `/gov/` 下会打到分析平台，落地页认错门户；相对路径才能跟着前缀走 |
| 同步放开该断言的写法 | `tests/test_frontend_contract.py` | 契约测试原本硬编码了带斜杠的写法 |
| 新增 `ATMOS_CORS_ORIGINS` | `atmos/settings.py`、`apps/common/webapp.py` | README 声称 CORS 可配，实际写死 `["*"] if dev else []`，非 dev 下两门户互访全被拦 |
| 新增应用内 HTTP Basic 认证 | `apps/common/auth.py` | 原本无任何鉴权，`/api/docs` 与 `POST /api/ops/collect` 全开放 |
| 新增 `python -m atmos schedule` | `collect/scheduler.py`、`cli.py` | 原本只有配置项、没有调度实现，数据不会自动更新 |
| ECharts 随代码托管 | `web/app/static/vendor/echarts.min.js` | 原走 `cdn.jsdelivr.net`，国内经常 502，图表全空 |

> 其余资源（`/shared/*`、`/static/*`）仍是绝对路径，这是**安全的**：
> 两个服务托管的是同一批文件，内容完全一致，从哪个前缀取都等价。
> 唯一运行期不同的一份就是 `/portal-config.js`，也正是唯一改成相对路径的那个。

因为鉴权已经放进应用，第 8 步的 Nginx **不再需要** `auth_basic`，只做反代。

---

## 1. 服务器基线

**先过这一关：云控制台的安全组。** 阿里云/腾讯云/华为云在系统防火墙之外还有一层
"安全组"（有的叫防火墙），**ufw 只管系统这一层**。只开 ufw 不开安全组，结果就是
`ufw status` 显示 80 已放行、浏览器却一直转圈 —— 这是新手最常卡住的地方。
去控制台的"安全组 / 防火墙"里放行入方向 **TCP 22 与 80**，来源填 `0.0.0.0/0`
（学习阶段够用；以后可以收紧成你的固定 IP）。

同样地，`systemctl status nginx`、`curl 127.0.0.1` 都正常但外网打不开时，
先怀疑安全组，别怀疑 Nginx。

**从 Windows 连上去**：Win10/11 自带 OpenSSH 客户端，不需要 PuTTY。打开
Windows Terminal（或 PowerShell）：

```powershell
ssh root@你的服务器IP
```

首次会问 `Are you sure you want to continue connecting?`，输 `yes` 再输密码。
密码输入时不显示字符，这是正常的，别以为键盘坏了。

以 root 首次登录后执行：

```bash
apt update && apt upgrade -y
apt install -y git nginx apache2-utils ufw curl rsync sqlite3 tmux python3-venv python3-dev

adduser atmos                 # 交互式设密码，其余项（Full Name 等）直接回车

ufw allow OpenSSH             # ⚠️ 必须先放行 SSH
ufw allow 80/tcp
ufw enable
```

> `adduser` 这一步别跳。跳过它之后所有涉及属主的地方都会失败，而且报错发生在
> **下一步**（`chown: invalid user: 'atmos:atmos'`），很容易误判成命令写错。

**验收**

```bash
python3 --version       # 期望 3.11.x（Debian 12 自带，满足 requires-python >=3.11）
id atmos                # 期望 uid=100?(atmos) gid=100?(atmos) —— 用户和同名组都在
ufw status verbose      # 22 与 80 为 ALLOW，其余 deny
```

**动手前先开一个新终端重连一次 SSH**，确认没把自己关在门外，再关掉旧会话。

---

## 2. 代码上服务器

不要用 `scp` 传整个目录 —— 用 Git，以后升级就是 `git pull`。

```bash
mkdir -p /opt && chown atmos:atmos /opt
su - atmos -c 'git clone <你的仓库地址> /opt/atmos'
```

私有仓库建议用 **Deploy Key**（只读、只对这一个库生效），比账号密码安全：

```bash
su - atmos
ssh-keygen -t ed25519 -C "atmos-deploy" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub      # 粘到仓库 Settings → Deploy keys，不要勾写权限
ssh -T git@github.com          # 验收：出现 successfully authenticated
```

**验收**：`ls /opt/atmos` 能看到 `atmos/`、`web/`、`config/`、`pyproject.toml`。

> **已经 clone 到别处了？**（比如以 root 身份 clone 到 `/root/xxx`）必须搬到 `/opt/atmos`：
> `/root` 的权限是 `drwx------`，`atmos` 用户连目录都进不去，systemd 里
> `User=atmos` + `WorkingDirectory=/opt/atmos` 会直接起不来。搬完还要**重建 venv**
> —— `pip install -e .` 会把手写的绝对路径写进 `.venv` 的 shebang 和 editable 查找器，
> 目录一挪就全部指向旧路径（详见第 3 节末尾）。

---

## 3. Python 环境

```bash
su - atmos
cd /opt/atmos
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 装的是 `-e .` 而不是 `-e ".[dev]"`：服务器不需要 pytest。
> 国内机器加清华源，否则 pandas/pyarrow 那几个大包容易卡住。

**验收**

```bash
.venv/bin/python -c "import fastapi, duckdb, pyarrow, pandas; print('deps ok')"
.venv/bin/python -c "import atmos; print(atmos.__file__)"   # 必须指向 /opt/atmos/atmos
du -sh .venv      # 约 500MB 上下，正常
```

> **venv 不能跟着目录一起搬。** `pip install -e .` 把绝对路径写进了两处：
> `.venv/bin/*` 脚本第一行的 shebang，以及 site-packages 里 editable 安装的查找器。
> 仓库目录一改名或一挪位置，`import atmos` 仍会去找旧路径，报
> `ModuleNotFoundError` 或悄悄加载到另一份代码。遇到这种情况：删掉 `.venv` 重建，
> 别去改那些文件。

---

## 4. 数据目录放到仓库外面

```bash
sudo mkdir -p /var/lib/atmos
sudo chown -R atmos:atmos /var/lib/atmos
```

**为什么要挪**：默认路径是项目内的 `data/`，虽然已被 gitignore，但一旦有人在服务器上
`git clean -xdf` 或重新 clone，数据就没了。放仓库外 + 固定绝对路径，升级永远不会碰到数据。

---

## 5. 生产配置 `.env`

```bash
cd /opt/atmos
cp .env.example .env
chmod 600 .env
nano .env
```

只改这几项（其余保持默认）：

```ini
ATMOS_ENV=prod
ATMOS_HOST=127.0.0.1
ATMOS_OPEN_BROWSER=false
ATMOS_ANALYSIS_PUBLIC_URL=http://你的服务器IP
ATMOS_GOVERNANCE_PUBLIC_URL=http://你的服务器IP/gov
ATMOS_LAKE_DIR=/var/lib/atmos/lake
ATMOS_CATALOG_DB=/var/lib/atmos/catalog.db
ATMOS_EXPORT_DIR=/var/lib/atmos/exports

# 访问控制：两项必须同时填写，少写一项就等于没有认证
ATMOS_AUTH_USER=你的用户名
ATMOS_AUTH_PASSWORD=一个足够长的密码
```

四个容易踩的点：

- `ATMOS_OPEN_BROWSER=false` 必须显式写。默认是 `true`，服务器上会起一个线程
  轮询 30 秒去尝试调用浏览器，纯属浪费；
- `ATMOS_HOST=127.0.0.1` 而不是 `0.0.0.0`：服务不对公网直接暴露，只有一个入口；
- 两个 `PUBLIC_URL` 决定了前端往哪发请求，**末尾不要带斜杠**；
- `ATMOS_CORS_ORIGINS` 保持留空。全站同源，留空即"不发 CORS 头"，正是需要的。

> **不装 Nginx、改用 SSH 隧道 + 双端口访问时，上面最后一条要反过来。**
> 那时页面在 `127.0.0.1:8000`、API 在 `127.0.0.1:8001`，**两个端口是两个源**，
> 再加上 `ATMOS_ENV=prod` 时白名单为空等于不发 CORS 头，浏览器会把跨源请求全拦掉
> —— 症状是"顶栏点「数据治理」切不过去，但页面本身能打开"。此时必须显式填写，
> 两个源都要写（同一个配置项被两个服务共用）：
>
> ```ini
> ATMOS_CORS_ORIGINS=http://127.0.0.1:8000,http://127.0.0.1:8001
> ```
>
> 验收不用开浏览器：
> `curl -s -o /dev/null -D - -H "Origin: http://127.0.0.1:8000" http://127.0.0.1:8001/api/health | grep -i access-control-allow-origin`
> 应回显 `access-control-allow-origin: http://127.0.0.1:8000`。
> 走 Nginx 路径分流后记得清空这一项。

改完 `.env` 记得 `chmod 600`，密码就在里面。

**验收**

```bash
.venv/bin/python -c "
from atmos.settings import get_settings
s = get_settings()
print(s.env, s.host, s.analysis_url, s.governance_url)
print(s.lake_path, s.catalog_db_path)
"
# 期望：prod 127.0.0.1 http://IP http://IP/gov 以及 /var/lib/atmos 下的路径
```

---

## 6. 首次初始化（10 分钟量级，务必在 tmux 里跑）

```bash
tmux new -s init
cd /opt/atmos
.venv/bin/python -m atmos init
```

首次要回补 **15 城市 × 180 天 ERA5 归档 + 92 天空气质量**，SSH 一断进程就死。
中途可以 `Ctrl+B` 再按 `D` 脱离，回来用 `tmux attach -t init`。

如果只想先看效果不想等，加 `--skip-archive`（跳过归档，快很多，但趋势/同比类分析会缺数据）。

> **tmux 里那个窗口正跑着前台进程时，敲什么都进不去 shell。** 你在里面输入命令，
> 字符是交给 uvicorn 的 stdin 的，屏幕上只会看到 `^B^B` 这类回显（tmux 前缀被按下，
> 但没有跟一个动作键）。三种出路：
>
> | 想干什么 | 操作 |
> | --- | --- |
> | 另开一个窗口执行命令 | `Ctrl+B` **松开**，再按 `c`；切回用 `Ctrl+B` 松开再按 `0`/`1` |
> | 回本地开新连接 | VS Code 终端面板点 `+`，重新 `ssh root@IP` |
> | 只脱离、不杀进程 | `Ctrl+B` **松开**，再按 `d`（回来 `tmux attach -t 会话名`） |
>
> 前缀键的关键是**松开再按下一个键**，连按两次 `Ctrl+B` 不产生任何效果。

**验收**：最后会打印 7 个资产的质量评分表，等级多在 A/B。

```bash
ls /var/lib/atmos/lake          # 应有 7 个资产目录
du -sh /var/lib/atmos           # 十几 MB 起
```

---

## 7. systemd 托管两个服务

拆成两个 unit，而不是一个进程跑 `--portal both`：一个挂掉不影响另一个，也能单独重启。

```bash
sudo tee /etc/systemd/system/atmos-analysis.service > /dev/null <<'EOF'
[Unit]
Description=atmos 分析平台 (:8000)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=atmos
Group=atmos
WorkingDirectory=/opt/atmos
EnvironmentFile=/opt/atmos/.env
ExecStart=/opt/atmos/.venv/bin/python -m atmos serve --portal analysis --no-open
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo sed -e 's/分析平台 (:8000)/治理平台 (:8001)/' \
         -e 's/--portal analysis/--portal governance/' \
         /etc/systemd/system/atmos-analysis.service \
  | sudo tee /etc/systemd/system/atmos-governance.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now atmos-analysis atmos-governance
```

**验收**

```bash
systemctl is-active atmos-analysis atmos-governance     # 两个 active
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8001/api/health
journalctl -u atmos-analysis -n 20 --no-pager           # 应看到"启动 分析平台（portal=analysis..."
```

> **不要改多 worker。** 后台任务（`/api/ops/tasks` 轮询的那个）实时状态存在**进程内存**里，
> 多进程会让轮询打到没有该任务的进程。这个数据量单进程完全够。

---

## 8. Nginx：唯一对外入口

鉴权已经做在应用里（`.env` 的 `ATMOS_AUTH_*`），所以 Nginx **不需要** `auth_basic`。
两层都配会变成两个登录框，反而更难排查。

```bash
sudo tee /etc/nginx/sites-available/atmos > /dev/null <<'EOF'
server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 8m;

    location = /gov { return 301 /gov/; }

    location /gov/ {
        proxy_pass http://127.0.0.1:8001/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/atmos /etc/nginx/sites-enabled/atmos
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

注意 `/gov/` 的 `proxy_pass` **末尾有斜杠**（剥掉前缀），`/` 的那个**没有**（原样透传）。
这两处不一致是刻意为之，写反了会 404。

**验收（四条都要过）**

```bash
curl -sI http://127.0.0.1/                     # 401 Unauthorized（认证在应用层）
curl -s  -u 你的用户名:密码 http://127.0.0.1/api/health
curl -s  -u 你的用户名:密码 http://127.0.0.1/portal-config.js | grep current
#   → "current": "analysis"
curl -s  -u 你的用户名:密码 http://127.0.0.1/gov/portal-config.js | grep current
#   → "current": "governance"    ← 这条过了，说明前缀分流和相对路径都对了
```

第一个 401 来自应用而不是 Nginx，判断依据是响应体是 JSON（Nginx 的 401 是 HTML 页面）：

```bash
curl -s http://127.0.0.1/api/meta          # {"ok":false,"error":"Unauthorized",...}
```

浏览器打开 `http://你的IP/`，输一次账号密码。顶栏点「数据治理」应当整站切换、无红线报错。

---

## 9. 定时刷新

调度是一个**独立的常驻进程**（`python -m atmos schedule`），不是 Web 服务内嵌的
定时器 —— 两个门户都会起 Web 服务，内嵌会让同一批数据被采集两遍。

先用 `--list` 确认 cron 解析正确、下次执行时间符合预期：

```bash
su - atmos -c 'cd /opt/atmos && .venv/bin/python -m atmos schedule --list'
```

```
  作业      cron            下次执行              说明
  ----------------------------------------------------------------------------
  collect 15 * * * *      2026-10-27 13:15  增量采集（跳过 ERA5 归档）
  quality 30 * * * *      2026-10-27 13:30  五维质量评测
  archive 20 4 * * *      2026-10-28 04:20  完整采集（含 ERA5 归档回补）
  prune   30 3 * * *      2026-10-28 03:30  清理过期运行记录与导出文件（不含 parquet）
```

四个作业的分工是刻意的：`collect` 每小时只拉天气与空气质量、**跳过归档**；
ERA5 有约 5 天发布延迟，每小时重拉 15 城市 × 180 天窗口纯属浪费带宽和上游额度，
交给每天凌晨的 `archive` 一次做完。`prune` 只删元数据与导出文件，
**永远不碰 Parquet**（那删了要重新采集才能回来）。要改时间就改 `.env` 里四条 cron，
留空即停用。

> 想先看 `prune` 会删多少，别等凌晨：`python -m atmos prune` 默认**干跑**，
> 只报数不落刀；确认后加 `--apply`。详见 README 的「保留策略与清理」。

交给 systemd 常驻：

```bash
sudo tee /etc/systemd/system/atmos-scheduler.service > /dev/null <<'EOF'
[Unit]
Description=atmos 调度进程（采集 / 评测 / 归档）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=atmos
Group=atmos
WorkingDirectory=/opt/atmos
EnvironmentFile=/opt/atmos/.env
ExecStart=/opt/atmos/.venv/bin/python -m atmos schedule
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now atmos-scheduler
```

注意是 `Type=simple`（常驻），不是 timer。`Restart=always` 让它在异常退出后自愈 ——
调度进程死掉时不会有任何报错，只是数据悄悄停止更新，这是最难发现的一类故障。

**验收**

```bash
systemctl is-active atmos-scheduler
journalctl -u atmos-scheduler -n 20 --no-pager     # 应看到"调度启动"与三行作业登记
su - atmos -c 'cd /opt/atmos && .venv/bin/python -m atmos schedule --once'   # 三个作业各跑一次
journalctl -u atmos-scheduler -f                   # 常驻时观察实际触发
```

> **调度不补跑**：进程重启后从"现在"重新算下一次，不重放停机期间错过的时次。
> 补跑一小时前的天气采集没有业务意义，而归档由每日作业天然覆盖。

---

## 10. 备份

```bash
sudo mkdir -p /var/backups/atmos
sudo tee /usr/local/bin/atmos-backup > /dev/null <<'EOF'
#!/bin/bash
set -euo pipefail
D=/var/lib/atmos
B=/var/backups/atmos/$(date +%F)
mkdir -p "$B"
sqlite3 "$D/catalog.db" ".backup '$B/catalog.db'"
tar czf "$B/lake-$(date +%F).tgz" -C "$D" lake
find /var/backups/atmos -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
EOF
sudo chmod +x /usr/local/bin/atmos-backup

echo '30 3 * * * root /usr/local/bin/atmos-backup' | sudo tee /etc/cron.d/atmos-backup
```

**为什么 SQLite 不能直接 `cp`**：服务正在写时拷出来的是撕裂的文件，恢复时会报
`database disk image is malformed`。`.backup` 走的是 SQLite 的在线备份 API，能拿到一致快照。

**验收**：`sudo /usr/local/bin/atmos-backup && sudo ls -lh /var/backups/atmos/$(date +%F)/`

---

## 11. 日常运维手册

**每天可能用到的**

| 目的 | 命令 |
| --- | --- |
| 看某服务日志 | `journalctl -u atmos-analysis -f` |
| 看调度与采集 | `journalctl -u atmos-scheduler -n 50` |
| 服务活着吗 | `curl -s localhost:8000/api/health`（免认证，这是探针专用端点） |
| 重启 Web | `sudo systemctl restart atmos-analysis atmos-governance` |
| 重启调度 | `sudo systemctl restart atmos-scheduler` |
| 磁盘 | `df -h /var/lib/atmos`、`du -sh /var/lib/atmos/*` |

**出问题时的排查顺序**（不要跳步）

1. `systemctl status atmos-analysis` —— 进程活着吗？起不来看 `journalctl -u` 最后的 Traceback；
2. `curl -s 127.0.0.1:8000/api/health` —— 进程活着但接口不通？看是不是 `.env` 路径写错；
3. `curl -s -u 用户名:密码 http://IP/api/health` —— 本机通、外层 401/502？401 是应用拒绝
   （密码写错了，注意改 `.env` 后必须重启），502 才是 Nginx 到后端的链路问题，
   看 `sudo nginx -t` 与 `/var/log/nginx/error.log`；
4. 页面能开但图表空白 —— 按 F12 看 Network 里 `/static/vendor/echarts.min.js` 是否 200；
5. 数据不动了 —— `journalctl -u atmos-scheduler` 看是没触发还是采集失败
   （上游限流、机器出网故障）。**先确认时间是新的**：`timedatectl` 时区错了，
   cron 会在你完全没预期的时间跑。

**升级**

```bash
su - atmos
cd /opt/atmos
git pull
.venv/bin/pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
exit
sudo systemctl restart atmos-analysis atmos-governance atmos-scheduler
curl -s http://127.0.0.1:8000/api/health
```

**回滚**：先 `git log --oneline` 找到上一个可用提交，`git checkout <commit>`，重装依赖、重启。
所以养成习惯：每次升级前先打 tag（`git tag -a v1.1 -m "上线"` 并 `git push --tags`）。

**改配置**（只动 `.env` 时）：

```bash
sudo systemctl restart atmos-analysis atmos-governance atmos-scheduler
```

`PUBLIC_URL` 改了要重启 Web（前端拿到的是注入值）；cron 或密码改了要连调度一起重启。

**按改动范围决定动作**（比背命令有用）

| 改了什么 | 服务器上要做 |
| --- | --- |
| `web/` 下任意文件（html/js/css） | `git pull` 即生效，**无需重启** |
| `atmos/` 下 `.py` | `git pull` + 重启对应服务 |
| `pyproject.toml` 的依赖 | `git pull` + `pip install -e .` + 重启 |
| `config/*.yaml` | `git pull` + 重启（启动时投影进资产目录） |
| `.env` | 不用 pull，直接重启 |

第一行为什么不用重启：静态资源是 `StaticFiles` 挂载 + `FileResponse`，**每次请求都从磁盘读**，
不是启动时读进内存；浏览器侧也有 `Cache-Control: no-cache`（带 ETag 校验），不会拿旧模块。
Python 模块则只在启动时导入一次，不重启等于没改。

> 改了前端却发现"像没生效"：先按 F12 看 Network 里那个文件是不是 200/304 且内容已是新的；
> 仍然怀疑时 `sudo systemctl restart atmos-analysis` 一次，排除变量。

**改前端请在你自己的电脑上改，不要 SSH 上去 `nano`**

不是洁癖问题。服务器上直接改出来的文件是**未提交的工作区改动**，下一次 `git pull`
会报 `Your local changes would be overwritten`，而你得在一个没有编辑器、没有图形界面、
还跑着线上服务的地方解冲突。

本地循环（改 → 看 → 提交 → 上线）：

```bash
# 1. 本地起服务看效果（无构建步骤，保存后刷新即可，不需要编译）
python -m atmos serve --portal both --no-open

# 2. 跑前端契约与语法测试（会检查 index.html 的资源引用、视图模块路径、JS 语法）
python -m pytest tests/test_frontend_contract.py tests/test_frontend_syntax.py -q

# 3. 提交推送
git add -A && git commit -m "fix: ..." && git push

# 4. 服务器拉取
su - atmos && cd /opt/atmos && git pull && exit
#    只有前端改动 → 到此生效，浏览器刷新即可
```

---

## 12. 可选的加固与优化

**SSH 加固**（改完先别关当前会话，另开一个验证）

```bash
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/'      /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

**换成域名拿到 HTTPS 后**（约 30 分钟）：把 `PUBLIC_URL` 改成
`https://atmos.example.com` 与 `https://gov.example.com`，用 `certbot` 签证书，
并给 Nginx 加两个 `server_name`。

此时两个子域是**跨源**，必须显式配上白名单（这个配置项现在有了）：

```ini
ATMOS_CORS_ORIGINS=https://atmos.example.com,https://gov.example.com
```

**但更简单的做法是继续保持单 origin**：域名指向同一个主机，用
`https://atmos.example.com/` 与 `https://atmos.example.com/gov/`，
那么 `.env` 和 Nginx 的位置规则都不用动，只需要加证书 —— 跨源会消失，
Basic 凭据照常自动携带。

---

## 13. 当前方案的边界（诚实说明）

- **密码是明文的**：HTTP 下 Basic 认证只是 base64，不加密。抓包可见。
  这段只适合学习与小范围使用，别放任何敏感数据（本项目本身也没有）；
- **只有一个账号**：能挡住匿名访问，但所有人看到的东西一样，没有角色区分，
  也没有操作审计（谁点了"触发采集"查不到人）；
- **调度不补跑**：进程重启后从"现在"重新算下一次，停机期间错过的时次不重放；
- **单机单点**：机器挂了就全挂，没有高可用。想练这块是下一个话题（双机 + 共享存储）；
- **SQLite 单写者**：两个服务同时写靠 `busy_timeout` 串行化，量再大要换 PostgreSQL；
- **IP 变了要改配置**：`.env` 里两行 `PUBLIC_URL` 改完重启即可。
