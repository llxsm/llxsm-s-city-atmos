"""前端模块的语法与模块图测试。

为什么单独立一个文件
--------------------
``test_frontend_contract.py`` 只做静态字符串检查（端点、路径是否存在于后端），
它**不会解析**前端源码。因此一个括号写错、导致整个视图加载失败的语法错误，
后端测试全绿、契约测试也全绿，只有在浏览器里点开那一页才会暴露。

而 ``node --check`` 并不能补上这个缺口：对含 ESM 语法的 ``.js`` 文件它会
**假通过**（实测同一个坏文件，``.mjs`` 报错退出 1，``.js`` 退出 0）。所以这里
直接用 V8 自己的解析器 ``vm.SourceTextModule``，与浏览器同一套语义。

两层校验
--------
1. **语法**：把 ``web/`` 下每个 ``.js`` 当 ESM 解析一遍，报出精确文件；
2. **模块图**：真正 import 每个视图，顺带验证它对 ``/shared/*`` 的具名导入
   确实存在——导出被改名这类问题同样是静态检查抓不到的。

node 不是本项目的运行依赖（前端无构建步骤），因此找不到 node 时整体跳过。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
APP = WEB / "app"
PORTAL_CONFIG = APP / "static" / "portal.config.js"

MODULE_DECL = re.compile(r"""module\s*:\s*['"](/static/views/[A-Za-z0-9_\-/.]+\.js)['"]""")

# 文件数下限：防止收集逻辑坏掉后"零个文件、零个失败"地静默通过。
MIN_JS_FILES = 20
MIN_VIEWS = 16

TIMEOUT_SECONDS = 120


# ==================================================================
# node 调用
# ==================================================================
def _node_prefix() -> list[str] | None:
    """定位 node。Windows 上可能是 ``node.cmd`` 垫片，需经 cmd.exe 执行。"""
    executable = shutil.which("node")
    if not executable:
        return None
    if executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", executable]
    return [executable]


NODE = _node_prefix()

pytestmark = pytest.mark.skipif(NODE is None, reason="未找到 node，跳过前端模块检查")


def _run_node(script: Path, args: list[str], env_extra: dict[str, str] | None = None) -> dict:
    """执行 node 脚本，取回它打印的 JSON 结果（最后一行有效输出）。"""
    env = dict(os.environ)
    env.update(env_extra or {})
    completed = subprocess.run(
        [*(NODE or []), "--no-warnings", "--experimental-vm-modules", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        pytest.fail(f"node 未输出结果（退出码 {completed.returncode}）：{completed.stderr.strip()[:800]}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        pytest.fail(f"node 输出不是 JSON：{lines[-1][:400]}\nstderr: {completed.stderr.strip()[:400]}")


def _walk_js() -> list[Path]:
    """``web/`` 下全部前端 JS（排除不存在的情况由断言负责）。"""
    return sorted(path for path in WEB.rglob("*.js") if path.is_file())


def _declared_views() -> list[str]:
    """从 ``portal.config.js`` 解析出视图模块路径，而不是在测试里硬编码文件名。"""
    text = PORTAL_CONFIG.read_text(encoding="utf-8")
    specifiers = sorted(set(MODULE_DECL.findall(text)))
    assert len(specifiers) >= MIN_VIEWS, f"只解析出 {len(specifiers)} 个视图，portal.config.js 结构可能已变"
    return specifiers


# ==================================================================
# 1. 语法
# ==================================================================
PARSE_SCRIPT = r"""
const vm = require('node:vm');
const fs = require('node:fs');

const failures = [];
let parsed = 0;
for (const file of process.argv.slice(2)) {
  parsed += 1;
  try {
    // 与浏览器同一套解析语义：强制按 ES 模块解析，不看扩展名
    new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { identifier: file });
  } catch (error) {
    failures.push({ file, message: error.message });
  }
}
console.log(JSON.stringify({ parsed, failures }));
"""


@pytest.fixture(scope="module")
def parse_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("frontend") / "parse_all.cjs"
    path.write_text(PARSE_SCRIPT, encoding="utf-8", newline="\n")
    return path


def test_frontend_js_parses_as_esm(parse_script: Path) -> None:
    """每个前端 JS 都必须能被 V8 按 ES 模块解析。"""
    files = _walk_js()
    assert len(files) >= MIN_JS_FILES, f"只收集到 {len(files)} 个前端 JS，收集逻辑可能已失效"

    result = _run_node(parse_script, [str(path) for path in files])
    failures = result["failures"]
    assert result["parsed"] == len(files)
    if failures:
        detail = "\n".join(f"  {item['file']}\n    {item['message']}" for item in failures)
        pytest.fail(f"{len(failures)} 个前端文件语法错误（浏览器打开对应页面会白屏/报错）：\n{detail}")


# ==================================================================
# 2. 模块图（具名导出是否对得上）
# ==================================================================
LOADER_SCRIPT = r"""
import { pathToFileURL } from 'node:url';

const WEB = process.env.ATMOS_WEB_ROOT.replace(/\\/g, '/');

// 复刻浏览器：把 /shared/* 与 /static/* 映射到仓库内的真实文件
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('/shared/')) {
    return { url: pathToFileURL(WEB + specifier).href, shortCircuit: true };
  }
  if (specifier.startsWith('/static/')) {
    return { url: pathToFileURL(`${WEB}/app${specifier}`).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
"""

RUNNER_SCRIPT = r"""
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';

register('./loader.mjs', import.meta.url);

const WEB = process.env.ATMOS_WEB_ROOT.replace(/\\/g, '/');
const failures = [];
let ok = 0;

for (const specifier of process.env.ATMOS_VIEWS.split(',').filter(Boolean)) {
  try {
    const module = await import(pathToFileURL(`${WEB}/app${specifier}`).href);
    if (typeof module.render !== 'function') {
      failures.push({ specifier, message: `未导出 render（实际导出：${Object.keys(module).join(', ') || '无'}）` });
    } else {
      ok += 1;
    }
  } catch (error) {
    failures.push({ specifier, message: `[${error.name}] ${error.message}` });
  }
}

console.log(JSON.stringify({ ok, failures }));
"""


@pytest.fixture(scope="module")
def graph_scripts(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("frontend-graph")
    (directory / "loader.mjs").write_text(LOADER_SCRIPT, encoding="utf-8", newline="\n")
    (directory / "runner.mjs").write_text(RUNNER_SCRIPT, encoding="utf-8", newline="\n")
    return directory


def test_view_modules_resolve_and_export_render(graph_scripts: Path) -> None:
    """每个视图都要能被真正 import，并导出 render。

    这一步覆盖静态检查抓不到的两类问题：视图依赖的共享模块不存在，
    以及视图从 ``/shared/*`` 具名导入的符号已被改名或删除。
    """
    specifiers = _declared_views()

    result = _run_node(
        graph_scripts / "runner.mjs",
        [],
        env_extra={"ATMOS_WEB_ROOT": str(WEB), "ATMOS_VIEWS": ",".join(specifiers)},
    )

    failures = result["failures"]
    if failures:
        detail = "\n".join(f"  {item['specifier']}\n    {item['message']}" for item in failures)
        pytest.fail(f"{len(failures)} 个视图无法加载：\n{detail}")
    assert result["ok"] == len(specifiers), f"只成功加载 {result['ok']}/{len(specifiers)} 个视图"
