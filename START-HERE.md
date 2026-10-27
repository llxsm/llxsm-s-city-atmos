# 从零开始部署（线性清单 · 只到"网页能打开"）

> 这份清单只做一件事：**在 15 分钟内让网页能打开**。
> 系统服务、鉴权、反向代理、定时调度——全部等你看到页面之后再说。
> 完整手册见 `DEPLOY.md`，但别一开始就读它。

---

## 开始前，记住三条

1. **先看提示符再粘贴。** `C:\...>` 是 cmd，`PS C:\...>` 是 PowerShell，`root@xxx:~#` 是服务器。粘错地方就会报"命令不存在"。
2. **每步都有一个「看到这个才往下走」。** 没看到就停下，别硬着头皮往下。
3. **最后会有两个窗口永远不许关**：服务器上的服务、你电脑上的隧道。两个都活着，网页才打得开。

---

## 第 0 步：清场（在服务器上）

```bash
ls -la /root/city-atmos          # 先看一眼有什么
rm -rf /root/city-atmos          # 确认里面只有 .venv 再执行
python3 -V
```

**看到这个才往下走**：版本号 ≥ 3.11。如果是 3.10（Ubuntu 22.04/CentOS 常见），先装 3.11，别往下走。

---

## 第 1 步：把代码放到服务器上

### 路线 A：仓库是公开的（一条命令，先试这个）

```bash
apt install -y git
git clone https://github.com/llxsm/llxsm-s-city-atmos.git /root/city-atmos
ls /root/city-atmos
```

### 路线 B：仓库是私有的（提示要密码/报 404 就走这条）

**在你的 Windows 上**（先 `cd` 到项目目录）：

```powershell
cd "C:\Users\llxsm\Documents\llxsm's city atmos"
git archive --format=tar.gz -o ..\city-atmos.tar.gz HEAD
scp ..\city-atmos.tar.gz root@你的服务器IP:/root/
```

**回到服务器**：

```bash
mkdir -p /root/city-atmos
tar -xzf /root/city-atmos.tar.gz -C /root/city-atmos
ls /root/city-atmos
```

**看到这个才往下走**：`atmos  config  pyproject.toml  web  tests  README.md  .env.example`

---

## 第 2 步：装依赖（在服务器上）

```bash
cd /root/city-atmos
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
.venv/bin/python -c "import atmos, pandas, duckdb, pyarrow; print('OK')"
```

**看到这个才往下走**：最后一行打印 `OK`。

> 后面一律用 `.venv/bin/python` 这个绝对路径，**不用再 `source activate`** —— 少一个出错环节。

---

## 第 3 步：写配置（在服务器上）

```bash
cd /root/city-atmos
cp .env.example .env
chmod 600 .env
nano .env
```

只改这三行（其余保持默认）：

```ini
ATMOS_ENV=prod
ATMOS_OPEN_BROWSER=false
# 鉴权两行保持空白 —— 双端口方案下开启鉴权会让治理平台整片 401
ATMOS_AUTH_USER=
ATMOS_AUTH_PASSWORD=
```

`Ctrl+O` 保存、回车确认、`Ctrl+X` 退出。

**看到这个才往下走**：`.env` 里 `ATMOS_OPEN_BROWSER=false`，两行 AUTH 为空。

---

## 第 4 步：先灌一点数据（在服务器上）

```bash
cd /root/city-atmos
.venv/bin/python -m atmos init --skip-archive
.venv/bin/python -m atmos collect --cities beijing,wuhan
.venv/bin/python -m atmos status
```

**看到这个才往下走**：`status` 打出资产和行数，不是一排 0。

> `--skip-archive` 跳过了 180 天历史归档（那要十几分钟），先让页面有数据看。全量以后再补。

---

## 第 5 步：起服务（在服务器上 · 这个窗口不许关）

```bash
cd /root/city-atmos
.venv/bin/python -m atmos serve --portal both --no-open
```

**看到这个才往下走**：日志里出现启动信息，然后**光标停住不动**（这是正常的，它在跑）。

另开一个服务器窗口验证：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8001/api/health
```

两个都返回 JSON 才算过。

---

## 第 6 步：开隧道（在 Windows 上 · 这个窗口也不许关）

```powershell
ssh -N -L 8000:127.0.0.1:8000 -L 8001:127.0.0.1:8001 root@你的服务器IP
```

**看到这个才往下走**：它**什么都不打印**，光标停住。这跟卡住长得一样，但它在工作。

---

## 第 7 步：打开网页

浏览器访问：

- http://127.0.0.1:8000 —— 分析平台
- http://127.0.0.1:8001 —— 治理平台

**看到这个才算成功**：页面打开、城市下拉框有内容、趋势图能画出来。

---

## 到这一步之后

先别急着往下。确认「重启服务器 / 关掉隧道」分别会发生什么，你就算真的学会了这套东西的哪儿连着哪儿。

然后再做这三件事（都在这份清单之外）：

1. 把它交给 systemd，让它开机自启、崩了自动重来 —— `DEPLOY.md` 第 7 节；
2. 用 `python -m atmos schedule` 让数据自动刷新 —— `DEPLOY.md` 第 9 节；
3. 想要密码保护，就必须先把两个端口收成一个 origin —— `DEPLOY.md` 第 0 节 + 第 8 节。

---

## 出错时只做一件事

在**出错的那个窗口**里跑这一块，把输出整段贴出来：

```bash
echo "=== 目录 ==="; pwd; ls -a
echo "=== 导入 ==="; .venv/bin/python -c "import atmos; print(atmos.__file__)" 2>&1 || echo "导入失败"
echo "=== 端口 ==="; ss -ltnp 2>/dev/null | grep -E ':800[01]' || echo "无监听"
echo "=== 健康 ==="; curl -s -m 3 http://127.0.0.1:8000/api/health || echo "连不上"
```

`echo` 的意义：**让每条命令都先打一行标题**，命令本身没输出时你也能看出它跑到哪一步了。
