# -*- coding: utf-8 -*-
"""依赖自检：逐个 import 全部项目模块，验证 requirements.txt 是否完整。

用法（仓库根，用项目 venv 的解释器）：
    venv/Scripts/python check_deps.py

为什么要它：**清单漏包的症状是"静默失效"而不是启动失败** ——
某些依赖只在特定工具被调用时才 import（函数内延迟导入），
装漏了会导致那个工具默默报错，而不是程序起不来。

agent_webui 侧另有一份对应的检查：agent_webui/backend/check_gateway_deps.py
"""
import os
import subprocess
import sys

# 默认以本脚本所在目录为仓库根（clone 到哪都能直接跑）
repo = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
repo = os.path.abspath(repo)

# agent/ 是**脚本式模块目录**（没有 __init__.py）：运行时把 agent/ 加进 sys.path
# 后按**裸名**导入（agent.py / approval.py / mcp_station.py …），不是 agent.xxx。
MODULES = []
agent_dir = os.path.join(repo, "agent")
if os.path.isdir(agent_dir):
    for f in sorted(os.listdir(agent_dir)):
        if f.endswith(".py") and not f.startswith("__"):
            MODULES.append(f[:-3])
    if os.path.isdir(os.path.join(agent_dir, "approvals")):
        MODULES.append("approvals")

# 包式目录（有 __init__.py）
for sub in ("agent_tools", "agent_MCP"):
    base = os.path.join(repo, sub)
    if not os.path.isdir(base):
        continue
    for f in sorted(os.listdir(base)):
        if f.endswith(".py") and not f.startswith("__"):
            MODULES.append("%s.%s" % (sub, f[:-3]))
MODULES.append("agent_tools")

print("验证 %d 个模块（解释器：%s）\n" % (len(MODULES), sys.executable))

failed = []
for mod in MODULES:
    # 用哨兵行取判定结果：被 import 的模块自己可能往 stdout 打印东西
    # （例如 agent.py 会打印 LLM/配置摘要），不能拿"最后一行"当结论。
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "sys.path.insert(0, %r)\n"
        "try:\n"
        "    import %s\n"
        "    print('__VERDICT__OK')\n"
        "except ModuleNotFoundError as e:\n"
        "    print('__VERDICT__MISSING:' + str(e.name))\n"
        "except Exception as e:\n"
        "    print('__VERDICT__WARN:' + type(e).__name__ + ':' + str(e)[:120])\n"
    ) % (repo, os.path.join(repo, "agent"), mod)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    verdict = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("__VERDICT__"):
            verdict = line[len("__VERDICT__"):]
            break
    if not verdict:
        verdict = "NOOUT"
    if verdict.startswith("MISSING:"):
        failed.append((mod, verdict[8:]))
        print("  MISS  %-38s 缺包: %s" % (mod, verdict[8:]))
    elif verdict.startswith("WARN:"):
        print("  WARN  %-38s %s" % (mod, verdict[5:]))
    elif verdict != "OK":
        failed.append((mod, verdict))
        print("  FAIL  %-38s %s" % (mod, verdict))
        if r.stderr:
            print("        %s" % r.stderr.strip().splitlines()[-1][:160])
    else:
        print("  ok    %s" % mod)

print()
if failed:
    pkgs = sorted({f[1] for f in failed})
    print("❌ %d 个模块 import 失败；涉及包: %s" % (len(failed), ", ".join(pkgs)))
    print("   → 补进 requirements.txt 后重跑本脚本")
    sys.exit(1)
print("✅ 全部 %d 个模块 import 通过 —— requirements.txt 完整" % len(MODULES))
