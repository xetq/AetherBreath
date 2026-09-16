# AetherBreath WebUI — P7 自检与验收报告

> 交付三件套之二/三：`README.md`（使用文档）+ 本报告（GitHub 友好自检 + 回归验收记录）。
> 代码规模：后端 13 文件 / 2684 行，前端 19 文件 / 1585 行，产物 dist 约 222 KB（gzip 后 JS 69 KB）。

## 1. GitHub 友好自检（逐条）

| 检查项 | 结论 | 取证方式 |
|---|---|---|
| 无硬编码绝对路径 | ✅ 零命中 | 正则 `\b[A-Za-z]:[\/]` 扫 `backend/*.py`、`scripts/*.py`、`frontend/src/**`、`README.md` → 0 命中；路径全部由 `config.py` 从 `__file__` 向上推导 |
| 新增环境变量均 `AETHER_*` 前缀 | ✅ | `AETHER_WEBUI_HOST/PORT`、`AETHER_GATEWAY_PYTHON`、`AETHER_AGENT_PYTHON`、`AETHER_START_TIMEOUT`、`AETHER_GRACEFUL_TIMEOUT`、`AETHER_CLARIFY_TIMEOUT`、`AETHER_CORS_ORIGINS`、`AETHER_AB_MODE`、`AETHER_BRIDGE_TOKEN`、`AETHER_KILL_ORPHANS` |
| 无 `Hermes` 品牌字样 | ✅ 零命中 | 全量源码 grep（大小写均无）；`vibe-coding-plan.md` 是历史任务书，保留原样不计入产物 |
| `backend/requirements.txt` 精简 | ✅ 仅 2 项 | `fastapi>=0.115`、`uvicorn>=0.30`（pydantic 等传递依赖不手写） |
| `frontend/package.json` 依赖合理 | ✅ 3 运行时 + 4 开发 | 运行时仅 `react`/`react-dom`/`marked`；无 UI 框架、无状态库（reducer 自写） |
| `node_modules/`、`dist/`、`__pycache__`、`logs/` 已忽略 | ✅ | `agent_webui/.gitignore`：`venv-gateway/`、`__pycache__/`、`*.py[cod]`、`frontend/node_modules/`、`frontend/dist/`、`logs/`、`.trash/`、`.tmp_*`、`.install_*.log|err` |
| 中文 UTF-8 无乱码 | ✅ | 网关/bridge 双端 `reconfigure(encoding="utf-8")` 守卫；日志与 API 中文实测正常（早期 GBK 乱码已修） |
| 无密钥/token 泄露 | ✅ 已修复并复验 | 曾发现 bridge access log 明文记录 `X-Bridge-Token`，现 `log_message` 经 `_redact_secret()` 打码；5 条构造用例（含同行双 token、header 形态）全部零泄露，线上日志实测为 `X-Bridge-Token=***`。AB 的 `.env` API key 未被 WebUI 读取或输出 |
| 前端质量门 | ✅ `tsc --noEmit` 退出码 0 | strict + `noUnusedLocals`；构建 `npm run build` 成功 |

## 2. 边界与错误处理（实测）

| 场景 | 期望 | 实测 |
|---|---|---|
| 非法 `session_id`（`../../evil`、`a b;rm`） | 拒绝 | `400 非法 session_id…（仅允许字母数字下划线点横线，≤80 字符）` |
| 空消息发回合 | 拒绝 | `400 消息不能为空` |
| 不存在会话的历史 | 不报错、可判定 | `200 {exists:false, messages:[]}` |
| 删除不存在会话 | 明确失败 | `404 会话不存在` |
| 已过期/伪造 `ask_id` 答复 | 明确失败 | `410 该提问已结束或超时` |
| 无活动回合时 `stop` | 立即返回、不误伤 | `200 {stopped:false, reason:"no-active-run"}`；未知 `run_id` → `404 run-not-found`（不再回落打断别人的回合） |
| 工作区目录穿越（`../../../`） | 拒绝 | `200 {ok:false, error:"路径越界：仅允许访问工作区内部"}` |
| 端口被占用时启动网关 | 明确报错且零破坏 | `[gateway] 端口 127.0.0.1:8901 已被占用…本次启动已中止，未回收任何 bridge 进程`，退出码 1 |
| SSE 断线 / 慢消费者 | 不阻塞 AB 回合 | 队列满即丢事件；网关侧事件带 `hub_seq`，重连 `?since=` 回放；前端另有 `history` 重建兜底 |
| bridge 开机失败/超时 | 可诊断 | `phase` 落 `OFF` + `last_error` 可见，`GET /api/agent/log` 取 stderr 尾部 |

## 3. 本轮实测发现并修复的缺陷（6 个，全部有复现证据）

| # | 缺陷 | 影响 | 修法 |
|---|---|---|---|
| 1 | `EventBus.emit()` 先写 `type` 再 `update(payload)`，而 clarify payload 自带 `type=kind` | **clarify 事件被改名成 `choice`**，前端永远等不到提问卡，AB 却最长等 900s → UI 假死 | 事件保留键后置（`payload` 先展开），bridge 侧字段改 `mode`；加回归守卫用例锁死 |
| 2 | `_start_run` 非阻塞取 `_TURN_GATE`，而 `done` 事件在门闩释放前发出 | 上一回合刚 `done` 就追发下一条 → **必现 409 busy** | 取门闩给 3s 宽限（不改持久化顺序），并加"P7.E 追发竞态"用例 |
| 3 | `_stop_run` 在给定 `run_id` 查不到时**回落到当前活动回合** | 一次打错目标的 stop 会静默中断别人的回合（实测误伤过一次回归） | 显式 `run_id` 未命中 → `run-not-found`，网关映射 `404` |
| 4 | `TaskOrchestrator` 首次创建发生在回合线程 → `signal.signal()` 抛 `ValueError: signal only works in main thread` | 首个回合直接失败 | bridge 主线程预建编排器单例（零侵入） |
| 5 | uv 的 `venv/Scripts/python.exe` 是 **trampoline**，`Popen.pid` 只是壳 | `proc.kill()` 杀壳留真身 → 每次强杀都攒孤儿进程（实测攒到 12 个 python） | 停止/强杀改 `taskkill /T /F` 杀进程树 + 启动时孤儿回收 |
| 6 | bridge access log 明文记录 `X-Bridge-Token`；`tools=0` 且无 `tool_begin/end`（worker 线程按回合线程 ID 匹配失败）；`tools: 13`；GBK 控制台崩溃；强杀白等 15s | 凭据泄露 / 工具时间线丢失 / 启动崩溃 / 强杀慢 | token 打码、加"活动回合指针"、UTF-8 守卫、`kill` 只在有回合时发 `/stop` 且超时压到 2.5s |

（P5 期间另有 2 个 `ask_user` 相关缺陷：参数名 `type` 遮蔽内置 `type()` 掩盖真实异常、`RunContext` 上不存在的属性引用——均已修，见 `webui_probe` 会话记录。）

## 4. 回归验收

见下节实际输出（`scripts/regression_p7.py`，结果落 `logs/regression_p7.json`）。

```
$ venv-gateway/Scripts/python.exe scripts/regression_p7.py     # 网关已运行，AB 关机态起测

PASS P7.0 网关健康 | dist_ready=True
PASS P7.0 AB 已开机 | pid=50104
PASS P7.A 多工具时间线 | ends=7 names=['calculator', 'execute_shell', 'read_file'] ev=done
PASS P7.A 文本含结果 | 7 × 8 = 56、11 × 13 = 143（两次 calculator 同一批发出）…
PASS P7.B clarify 提问送达 | q=今晚喝什么？
PASS P7.B 事件类型未被 payload 覆盖 | type=clarify_request mode=choice
PASS P7.B 回答后续跑完成 | done=done txt=你选的是：**黑咖啡（苦到清醒）**
PASS P7.C 优雅中断生效 | ev=done turn=IDLE
PASS P7.D 强杀后 OFF | phase=OFF
PASS P7.D 重开可发 | pid=50336
PASS P7.D 重启后续聊（历史恢复） | ev=done txt=7 和 8、11 和 13
PASS P7.E done 后立刻追发不 409 | A=done B=done
PASS P7.E 未知 run_id 的 stop 不误伤 | <HTTPError 404: 'Not Found'>

==== 回归总结：全部通过 (13/13) ====   退出码 0
```

覆盖链路：开机 → 真实 LLM 回合（多工具并行 + 编排器线程分组）→ SSE 事件流与时间线 →
clarify 暂停/答复续跑 → 优雅中断落盘 → 强杀进程树（零孤儿）→ 重开从磁盘恢复历史续聊 →
回合竞态与越权中断守卫 → 关机清理。

**测试过程中的自我更正（如实记录）**
- 前两轮 P7.A/P7.B 的 FAIL 是**测试脚本自身**的问题：① 事件字段按 `payload.data` 取值（实际平铺）；
  ② SSE 用 `urlopen(timeout=10)`，LLM 静默 >10s 即 socket 超时静默断流（真实前端 `EventSource` 自带重连）。
  已改为 90s socket 超时 + 按 `hub_seq` 自动续订。**产品无这两处缺陷。**
- 一轮 P7.D 的 FAIL 是断言过严：模型答「7×8、11×13」而未复述乘积，历史恢复其实成功。已放宽为二者取一。
- 收尾清理测试会话时误删了当轮在用的 `p7reg_20260907_212723`；因回合结束即落盘，实测对回归无影响（上表即为该轮结果）。
- 曾把「`main.py` 含绝对路径」写进自检——复查证明是**我自己的正则写错导致的假阳性**，源码零命中。

## 5. 一句话启动

```bash
agent_webui/venv-gateway/Scripts/python.exe agent_webui/backend/main.py    # → http://127.0.0.1:8900
```

## 6. 现存遗留（不阻塞日常使用）

- `webui设计说明.txt` 为空占位文件（0 字节），可直接删。
- 事件缓冲为内存环形队列，网关重启后旧事件不可回放（设计取舍，见 README §10）。
- `mode` 参数已贯通（开机→bridge→hello/status 回显），但尚未绑定真正的模式差异，留给下一轮「模式切换」。

---

## 7. 补丁轮（22:49–23:00）· 缺陷 #7：bridge 自死锁（严重）

**现象**（主人在界面发消息后点「回合终止」）：
`中断失败：bridge 通信失败: bridge /stop 请求失败: TimeoutError: timed out`

**隔离复现**（另起一个临时 bridge，不碰生产进程）：

| 请求 | 修复前 | 修复后 |
|---|---|---|
| `POST /stop {"run_id": null}` | 超时未响应 | 0.02s `{"stopped":false,"reason":"no-active-run"}` |
| `POST /stop {}` | 超时未响应 | 0.00s 同上 |
| `POST /stop {"run_id":"ffffffffffff"}` | 超时未响应 | 0.00s `{"ok":false,"reason":"run-not-found"}` |
| 随后 `GET /health` | **超时未响应** | 0.02s 正常 |
| 随后 `GET /tools` | 0.00s 正常 | 0.00s 正常 |

**根因**：`_RUNS_LOCK = threading.Lock()`（不可重入），而 `_stop_run` 在 `with _RUNS_LOCK:` 块内调用 `active_run()`，后者内部**再次** `with _RUNS_LOCK` → 同线程二次 acquire → 该处理线程永久阻塞且**始终持有这把锁**。于是所有需要 `_RUNS_LOCK` 的端点集体卡死（`/stop`、`/health`、`/chat`、回合收尾 `finally`），只有不碰锁的 `/tools` 还能应答 —— 这组"半死不活"的特征正是定位依据。访问日志里**完全没有 `/stop` 行**也印证了处理线程从未返回。

**修复**：① `_RUNS_LOCK` 改 `threading.RLock()`；② 把 `active_run()` 移出持锁区（结构性消除嵌套）；③ 全量审计 `with _RUNS_LOCK:` 块，确认仅此一处风险点（其余块只调 `dict.get/values`）；④ 前端 `stop` 按钮区分 `stopped=false`，不再把"没有活动回合"误报成"已请求中断"。

**测试覆盖的窟窿（自我批评）**：13 条回归用例每次都显式传 `run_id`，而**界面默认路径**（`state.agent.run_id` 为 null 时）从未被走过 —— 所以 13/13 全绿仍漏了这个必崩分支。已补 `P7.F`：无 run_id 的 `/stop` 必须 <3s 返回 + 随后 `/health` 必须存活。

**另一处误判纠正**：21:15 我曾把同类超时记为"优雅停止的延迟"，并声称"那发 bogus stop 打断了 A 回合"。对照现在的复现结果，那个 handler 是**卡死在锁上、根本没走到注入中断**，因此当时的两条推断都不成立；随后 v3 的 409 现象与本案是否同源属**推断**，未证实。

---

## 8. 补丁轮二（09-08 08:5x）· 缺陷 #8：clarify 卡无自由文本入口（且我上一轮的结论是错的）

**主人报的现象**：`ask_user` 无法自己输入文本；怀疑超时是"webui 崩了之后换了通道"。

**真相（读 ClarifyCard.tsx 定案）**：`choice` 分支只渲染选项按钮 + 取消，**没有任何输入框**；
`multi` 只允许勾选，也不能补写。只有 `freeform`（或 options 为空）才有 textarea。
我那次问的是 `type="choice"` + 3 个选项，而你要答的是选项之外的内容 → **界面上无路可走**，
30 秒后编排器给回 `超时（30 秒）`。**所以超时是结果，不是原因。**

> 我上一轮把主因写成"编排器 30s 掐断"，属于**用未经核实的推断覆盖了显而易见的 UI 缺陷**，现予以纠正。

**30 秒这条约束本身是真的**（次因，已核实代码）：
```
agent.py:321            TaskOrchestrator(..., default_timeout=30)
_run_on_pool()          timeout = timeout or self.default_timeout   ← 整批共用
                        done, not_done = wait(futures, timeout=timeout)
                        not_done → ToolResult(ok=False, error=f"超时（{timeout} 秒）")
                        future.cancel()  ← 对已开始运行的任务无效
注释原文：池常驻…超时的 future 仅标记错误，其线程继续在后台跑到结束
```
即：超时后**那个 ask_user 线程还在后台等到 900s**，而你点的答案会投给一个已被判死刑的等待者
—— **答复被静默吞掉**。这解释了"我接收不到 ask_user 了"这类体感。

**已修（纯前端，不需重启）**：ClarifyCard 三种模式统一在选项下方增加**自由文本输入框 + 提交文本**；
`multi` 的勾选与手写内容合并提交（不互相丢弃）；新增 30s 倒计时与"已超出窗口"过期态
（过期后提交仍允许，但明确提示"本轮可能已判超时，请把答案直接发我"）。
`typecheck 0 错误`，`vite build` 通过（`index-eKpeDo8b.js`）。

**待你批准的后端修法（需重启 AB）**：在 bridge 里包一层 `_run_on_pool`，当本批 tasks 含 `ask_user`
时把该批 timeout 提到 `CLARIFY_TIMEOUT + 30`，并让 `clarify_request` 事件带出**真实可用窗口**，
前端倒计时就不再硬编码 30。副作用：同批其它工具的超时被一起拉长。

---

## 9. 补丁轮三（09-08 09:0x）· 缺陷 #9：会话视图串台（两型）

**主人报的现象**：AB 加载中卡片（`☲ AB ●●● 思考中… › 📜 上次语境快照 › 📂 加载会话…`）在切到别的会话后仍挂在新会话下方。

**根因**（读 store 定案，与后端无关）：
- 网关 `/events` 实时段**本来就按 session_id 过滤**（`evt.get("session_id") not in (None, session_id) → continue`），所以旧会话的新事件不会推过来；
- 但 `set_current` 只清了 `streaming/clarify/turnError`，**没清 `agent.busy`/`turn_phase`，也没管 `progress` 数组的归属** → 旧回合的状态卡在视图数据里，被当成"当前会话的状态"渲染出来。
- 本质：**把进程级事实（AB 在忙什么）当作会话级视图（这个会话在忙什么）来显示**。

**顺手挖出的同一类更严重的一型**：`done` 事件把 A 会话的助手回复 `append` 进 `base.messages`（= 你此刻正看着的 B 会话），**属于污染数据而非仅污染视觉**。

**修法**（纯前端，零后端改动、零重启）：
1. store 新增 `turnOwner`：由带 `session_id` 的事件更新，`done/error` 清空；`set_agent` 里据 `busy && session_id` 推导，使**刷新后仍能识别"哪个会话有回合在跑"**。
2. `progress` 条目与 `TimelineEntry` 都带上会话归属；`MessageList` 用 `owns = turnOwner === current` 门控状态卡，`myProgress` 只取本会话条目（切回来还在，不丢）。
3. `done`/`error` 的**写数据路径**同样按归属判定：非本会话只更新进程级状态，不动 messages/streaming/progress/turnError。
4. 会话在别处跑时，聊天区顶部显示提示条「⏳ 会话 X 的回合正在执行，本会话只是旁观位 · 切过去看」。
5. **延续状态**：新增 `lib/persist.ts`（命名空间 `abw.`，全程 try/catch 容错，隐私模式静默降级）
   - `abw.current`：记住当前会话，刷新后自动回到原处；
   - `abw.draft.<sid>`：**每会话输入草稿，400ms 防抖落盘**；单一真相源 `draftRef`，切会话时先结清上一份再恢复新的一份（防抖尾巴不会写错槽位）；发送成功即清。

**GitHub 友好性复检**：绝对路径 0、品牌字样 0、疑似密钥 0、**新增依赖 0**（草稿与防抖全用 `localStorage` + 自写 debounce，不引库）、`typecheck 0 错误`、构建 `index-DPZzCPnR.js` 222.86 KB（gzip 72.64 KB）。

**未做（避免误伤，需你点头）**：`abw.lastSeq` 持久化以做增量续订 —— 当前刷新用 `since=0` 全量回放环形缓冲，事件已带归属、前端按会话过滤，正确性无损；持久化 seq 反而有"bridge 重启后 seq 归零导致错位/漏事件"的风险，故不动。

---

## 10. clarify 时限标准化（09-08 16:4x）· 缺陷 #10：30 秒批次上限

**根因**（读码定案）：`_run_on_pool(pool, layer_tasks, timeout)` 对**一整批工具共用一个 timeout**，
`timeout = timeout or self.default_timeout`（`agent.py:321` 建的是 `default_timeout=30`），
到点走 `ToolResult(ok=False, error=f"超时（{timeout} 秒）")` 且 `future.cancel()` **对已运行的任务无效**
→ ask_user 的等待线程还在后台跑，但批次已结算，主人晚到的答复投给没人认领的等待者。

**修法**（`bridge.py` 运行期包一层，零改 `agent/`）：
```python
def _install_clarify_window(orch):        # 幂等：_abw_clarify_patch 标记防重复包
    orig = orch._run_on_pool
    def _patched(pool, tasks, timeout=None):
        if any(getattr(tc, "name", None) == CLARIFY_TOOL for tc in (tasks or [])):
            timeout = max(int(timeout or 0), CLARIFY_TIMEOUT + 10)   # 120 + 10s 余量
        return orig(pool, tasks, timeout)
    orch._run_on_pool = _patched
```
- `AETHER_CLARIFY_TIMEOUT` 默认 **900 → 120**（bridge 与网关 config 同步）
- **余量 +10 是设计要点**：让 waiter 自己先超时、返回「主人没在限定时间内回答」这条**正常结果**，
  而不是让编排器抢先结算把答复吞掉。
- 旧的"临时放宽 `orch.default_timeout`"保留为兜底并加注释说明（对本批仍无效）。
- 前端 `ClarifyCard` 倒计时不再硬编码 30，改读 `clarify_request.timeout` 真值（兜底 30，夹在 5–900s）。

**端到端实测**（另起临时 bridge pid 50776，不碰宿主 40776；故意不回答）：
```
16:41:58 hello: tools=13 injected=[ask_user] model=qwen3.8-flash
16:42:00 回合已发 run_id=6c416517bfac
16:42:10 ✅ clarify_request 到达  timeout=120  kind=choice
16:42:28    /health: {"ok":true,"phase":"RUNNING",...}   ← 未被 30s 掐断，回合仍在等待
```

```
16:44:16 T+149s 收尾 type=done → "⏱️ 弹窗超时了 —— 你没选。… 返回结果：主人在限定时间内没有回答 …"
        判定：✅ waiter 自己超时并给出正常结果，答复未被静默吞掉
        （从 clarify 到达 16:42:10 算起共 126s >> 旧的 30s 批次上限）
16:44:17 临时 bridge 已回收（宿主 40776 全程未受影响）
```
**生效条件**：本项改的是 `bridge.py`，需**关机→开机**（重启 AB 子进程）才载入。

---

## 11. 界面微调（09-08 16:5x）· 中期进度的两处视图问题

1. **「工具状态机」面板里的"中期进度（agent stdout）"整块移除**
   它是 stdout 流，一行行累积会把时间线顶出可视区，而面板只有 3/4 高度。
   全量并没丢：`logs/bridge_<开机时间>.log` 里的 `[ab] [中期进度]` 行，以及「运行时」面板
   拉的 `GET /api/agent/log?lines=120` 尾部都还在。顺带清掉已无使用者的 `.tl-progress` 样式。
2. **加载卡片里的进度改为「摘要 + 可展开全部」**
   原先硬截 `slice(-4)` 且每行 `nowrap` 省略 → 看起来"进度加载不全"。
   现在默认给最后 3 条单行摘要 + 一个 `▸ 展开全部 N 条进度` 开关；展开后允许换行、
   最大 40vh 内部滚动。展开态**按会话归位**（切会话即收起，避免又一处视图串台）。
   注：store 里 progress 是环形保留最近 60 条，更早的只有日志有，按钮 title 里写明了。

**GitHub 友好性**：新增依赖 0、绝对路径 0、品牌字样 0；`tsc --noEmit` 零错误；
构建 `index-BdGe67aH.js` 223.17 KB（gzip 72.86 KB）+ `index-DmL4vcxt.css` 19.03 KB。**纯前端，刷新即生效。**

---

## 12. 加固轮四（09-08 18:3x）· 误杀判据 + 静默失败 + 编排器真状态机

### A. 「更值钱的缺陷」：网关启动按子串强杀进程（已修）
旧 `cleanup_orphan_bridges()` 用 `CommandLine -like '*bridge.py*'` + `Stop-Process -Force`，
**任何命令行里出现过 `bridge.py` 字样的 python 进程都会被杀**。实测复现：一个只含该字符串的
`sleep` 进程（及其 trampoline 子进程）被判定为 bridge。`pytest test_bridge.py`、
`python -c "...bridge.py..."`、诊断脚本自己，全在误杀范围内。

新实现拆成独立模块 `backend/procguard.py`（可单测），三道判据：
1. **锚定结尾**：命令行必须以 `backend/bridge.py` 结束（`pytest test_bridge.py`、带尾参的
   `bridge.py --extra`、内联 `-c` 全部落空）；
2. **父链无活网关**：有活网关祖先 = 别人正在用的 AB，绝不碰；
3. **自身祖先无条件保护**：本函数由活网关自己调用，故"我+我的祖先"直接进保护集 ——
   正则好坏不再决定"会不会杀掉活动 AB"（这条是修好一个真实隐患后补的：网关用相对路径
   `backend/main.py` 启动时旧正则会判空，保护集失效 → 会杀掉现役 AB）。

顺带：`_kill_locked` 强杀失败不再静默（重试 Stop-Process 兜底 + `sm.set_error` + stderr），
旧版只记 `died=False` 就继续写 OFF，正是"状态说停了、进程还占着端口和 LLM 配置"的成因。

判据表驱动自检（6/6 通过）：绝对路径 bridge=✅、相对 `backend/bridge.py`=✅、
内联 `-c` 含该串=✗、`pytest test_bridge.py`=✗、带尾参=✗、`backend/main.py`(相对/绝对)=网关✅。

### B. 网关日志不再被覆盖 + 噪音
`main.py` 新增 `_open_tee_log()`：stdout/stderr 同时抄进 `logs/gateway_<启动时间>.log`
（追加写、只留最近 5 份），不再依赖外部重定向被每次覆盖；`_silence_pipe_noise()`
只压掉浏览器硬断 SSE 引发的 `ConnectionResetError/BrokenPipeError @ connection_lost`，
其余异常照旧上报。

### C. 编排器真状态机（扁平条，与 ToolPipeline 同颗粒度）
`bridge.py` 运行期包装（零改 `agent/`）：`ToolTemplate.create_pipeline` → pending、
`ToolPipeline.run` → running / done|failed|cancelled + elapsed、`_run_on_pool` → `orch_batch`
（层号、该层任务数、parallel/serial 池）。包装成功率写进 `_orch_state`
（如 `ready+wait130s+watch(batch,pipe,tpl)`），前端据此显示「真状态」或「推导」，不静默降级。
`_wrap_tool` 的 `call_id` 改取 thread-local 里的**真实 `tool_call.id`**（由被包的 `run()` 注入），
于是 pipeline 事件与 tool_begin/tool_end 指向同一次调用。每回合开头重置层号与登记表，
终态即删 + 240 条兜底裁剪（不再重演 `_RUNS` 永不 pop 那类泄漏）。

端到端实测（隔离临时 bridge，宿主未受影响；留档 `logs/pipeline_events_probe.log`）：
```
orch_batch  layer=1 pool=parallel size=2
pipeline    pending → running → done      × 2（call_13c…, call_1f7…）
tool_begin  call_id == 真实 tc_id ✅（两套事件已可对齐）
done        回合正常收尾
```

### D. 界面
- 卡片改**扁平一行一管道**：`状态点 + L层 + 工具名`，跑=高亮脉冲、排队=橙点、
  完成/关闭=灰空闲（回合收尾时残留的 running/pending 统一转 idle 灰），失败=红。
- 编排器区 1/4 → **2/5**，时间线相应压到 3/5（时间线有滑动窗口，不占空间）。
- 修复加载卡片展开进度"文字挤成一坨"：去掉 `max-height:40vh` 内滚限制，改为
  **随内容自然撑高** + `word-break:break-word` + 每行 `line-height:1.6` 与虚线分隔。

**GitHub 友好性**：新增依赖 0；新增模块 1（`procguard.py`）；绝对路径 / 品牌字样 grep 0 命中；
`tsc --noEmit` 零错误；构建 `index-BOW2oJs3.js` 224.23 kB（gzip 73.23 kB）。
**生效条件**：前端刷新即生效；`procguard/agent_proc/main` 需**重启网关**；
`bridge.py` 的状态机事件需**重启 AB**（关机→开机）。

---

## 13. 槽位视图（09-08 18:5x）· 编排器状态机的正确形态

**我第一版理解错了**：做成了"每次调用长出一条管道"（per-call 列表）。主人要的是**池槽位常驻平铺**
—— 串行 #0 + 并行 #0…#7 共 9 格固定显示，谁在跑谁高亮并显示工具名，不跑常灰，形如控制台。

### 关键实现障碍与解法
`ThreadPoolExecutor` 的线程是**懒创建**的：没用过的槽位在任何事件里都不会出现，
所以"9 格"不能靠观察线程名倒推，必须由后端显式上报**池容量**。
→ `bridge.py` 新增 `_pool_capacity(orch)`，读 `max_workers` / `serial_workers` / 池对象的
`_thread_name_prefix` 与 `_threads`（已起线程数），塞进 **hello 首行协议**；
网关把 hello 整包回显在 `status.hello`，前端 `state.agent.hello.pools` 直接可用（网关零改动）。
→ `pipeline` 事件补 `thread` 字段（`ToolPipeline.run` 就在工作线程里跑，取
`threading.current_thread().name` 即槽位身份）。

### 端到端实测（隔离 bridge，两份留档：`logs/slots_fast_probe.log`、`logs/slots_slow_probe.log`）
```
hello.pools = {parallel:{workers:8,started:0}, serial:{workers:1,started:0}}   → 9 格 ✅
快工具（4×calculator，单条 <1ms）： orch_batch(1,parallel,size=4)
                                     四条 pipeline 全落在 orch-parallel_0        ⚠️ 见下
慢工具（3×execute_shell sleep 4）：   orch_batch(1,parallel,size=3)
                                     orch-parallel_0 / _1 / _2 各自 pending→running→done ✅ 真并发
前端正则 ^(?:orch-)?(parallel|serial)_(\d+)$ 对全部实测线程名解析成功 ✅
```
**必须说明的行为**：并行池虽容量 8，但 Python 只在「队列仍有积压且无空闲线程」时才新建线程。
calculator 这种毫秒级任务，一个线程就把队列吃光了 → 界面上只有 `并#0` 亮、`#1…#7` 常灰。
**这是真实并发度的忠实反映，不是显示坏了**：只有慢工具（网络、shell、浏览器、LLM）才会点亮多个槽。
标题栏 `并 8 · 串 1 · 跑 N`，悬浮可看"已创建线程数"（懒起证据）。

### 前端形态
固定顺序（串行在前、按槽号，跑着的不顶位）；每格一行：`状态点 + 并#N/串#N + 工具名(+L层)`，
跑=蓝+脉冲、排队=橙、空闲=灰（附"上次 xxx"弱字）、失败=红；容量未知时回落 8+1 并在标题注明。
槽号大于上报容量时自动补格，绝不丢信息。`tsc --noEmit` 零错误，构建 `index-Brns4SQB.js`。

**13b. 槽位全展开（09-08 19:0x）**：槽位区改为 `flex: 0 0 auto`（按内容自然撑高，9 格全部直显），
内部滚动改由外层 `max-height:46vh + overflow-y:auto` 兜底 —— 9 格时不会出现滚动条，
仅在槽位暴增或窗口极矮时兜底，避免溢出压到时间线；行高压缩到 `line-height:1.35 / padding:1px`。
时间线区改为吃掉剩余空间（`flex:1 1 auto; min-height:84px`，自身仍有滑动窗口）。
预算：槽位区 ≈ 208px，900px 高窗口下时间线仍可得 ~600px。构建 `index-Blt750IH.js`。

---

## 14. 卡片恢复（09-11 19:xx）· 审批 / ask_user 弹窗在刷新·切会话·重开后消失

**主人报的现象**（四个，均导致工具全部返回空、回合当场卡死，下次进会话补一条「审批未完成（等待裁决时回合被中断）」）：
弹窗弹出后 ① 切会话再回来消失 ② 刷新页面消失 ③ 点新建再返回消失 ④ 关页面再打开消失。
上一轮（commit `584346a`）声称"改为服务端权威状态，四个现象一并修好"，主人实测四个都没好（②④在同一弹窗多次测试中复现）。

### 根因（四条，逐层拆开）

| # | 层 | 缺陷 | 为什么上一轮没发现 |
|---|---|---|---|
| A | 契约 | 前端 `api.ts` 用 `post()` 打 `/approval/pending`，后端只注册了 `@router.get` → **每个恢复请求吃 405**，而调用点 `catch { /* 拉不到就算了 */ }` 把它静默吞掉 | 验收脚本用 `page.request.get()` 直接打后端端点，**测的是"接口在不在"，不是"前端有没有打这个接口"**。恢复通道自上线起一次都没生效过 |
| B | 状态 | `set_current` 写成"只保留属于目标会话的卡" —— 切走时把卡删了，切回来自然什么都没有，而后端 `threading.Event` 还在死等 | 注释写的是"不再无条件清空"，实际改成了有条件清空，现象照旧 |
| C | 语义 | bridge 未开机时后端回 `ok:true + 空列表`，前端把它当"服务端说没有卡"去剪枝 → **把屏幕上真挂着的卡删掉**（"看不见"被当成"不存在"） | 空列表这个形状本身是上轮为了"别把页面加载带崩"引入的，安全取向对，但缺一个"这是不是权威快照"的字段 |
| D | 回放 | SSE `replay` 无差别重放旧事件，上一回合的 `done` 会**清掉此刻正挂着的卡**；旧 `approval_batch` 又会**复活早已结束的僵尸卡** | 初次打开 `since=0` 整环回放才触发，取决于回放与恢复请求谁先到 → 正是"要刷几次才复现" |

### 修法

- **契约两侧都收紧**：前端改 `call()`（GET）；网关 `/approval/pending`、`/clarify/pending` 同时挂 `api_route(methods=["GET","POST"])` —— 观测面不该靠"调用方恰好写对方法"成立。
- **`authoritative` 结构化字段**：只有服务端确实回了权威快照才是 `true`；bridge 未就绪/异常一律 `false`。前端**仅在 `true` 时允许剪枝**，`prune_gates` 也拆成 `approvals`/`clarify` 两条通道开关。语义不放文案上。
- **卡片不再按会话过滤**：审批与提问是**进程级**闸门（主循环串行，同时最多一批），归属只是它顺带的标签。切走保留卡片，归属不同时卡面显式提示「这张卡属于会话 X —— AB 正在那边等你裁决」。
- **`replayed` 标记**（`sse.py` 的 `replay()` 给副本打标）：回放帧只补时间线，不建卡、不清卡、不改忙闲、不弹告警。"此刻谁在等人"的唯一真相 = REST pending 通道。
- **恢复失败必须留痕**：分通道 `Promise.allSettled`（一个失败不拖垮另一个）+ `console.warn` + 通道曾成功过才弹去重 toast。**静默即失控** —— 上一轮四层链路全废而无人察觉，直接原因就是那个空 `catch`。

### 实测（`scripts/verify_gate_restore.py` 逐层跑，88 项断言全绿）

```
[PASS] 1. 服务端待裁决台账（pending_cards / answer / 超时）        31/31
[PASS] 1b. SSE 回放标记（回放只补时间线，不改写此刻状态）          10/10
[PASS] 2. 前端状态语义（切会话·刷新·重开·剪枝守卫·回放帧）        31/31
[PASS] 3. 真实浏览器链路（DOM 层：卡片真的出现在屏幕上）          16/16
```

- 第 2 层直接 bundle **生产** `appStore.tsx` 跑纯函数断言（现象 1/3 的状态语义、恢复倒计时按 `remaining` 续算、剪枝守卫、回放帧守卫）。
- 第 3 层驱动真实 headless Edge + 生产前端代码：切会话后卡片从服务端找回并渲染、**刷新后页面重建仍找回**、倒计时续算（248s 而非满格 300）、归属提示出现/消失。
- **真机端到端**（真 LLM，零副作用）：让 AB 真调一次 `ask_user` → 真卡挂起（9s）→ 新开浏览器页面**靠恢复通道找回**（标「由服务端找回」，倒计时 113s→刷新后 103s 续算）→ 回答后 pending 归零。
- 契约实测：bridge 未开机 `{"authoritative":false,"error":"bridge 未就绪…"}`；在线 `{"authoritative":true}`；GET 与 POST 均 200（修复前 POST=405）。

### 未做 / 遗留（如实记录）

- **真审批卡**（AB 真提出需审批的系统盘操作）未做真机验证 —— 那需要让 AB 真发起一个高危请求，有"被误批准后真执行"的风险；已验证的是同一套机制的 `ask_user` 真卡 + 审批通道的后端单元级（31 项）+ 注入级浏览器回归（16 项）。
- 页面加载/重连时恢复请求会发多次（挂载、SSE open、切会话各一次）。全是幂等 GET，代价可忽略，未加节流（避免为省一次请求引入"该同步时被跳过"的新风险）。
- 第 3 层回归脚本会自拉一个 headless 浏览器；若环境没有 Edge/Chrome 会明确报错，不是静默跳过。

---

## 15. 中期交互（「用户交代」，09-14）· 回合跑动中改方向

**需求**：AB 干长活时会持续产生中期输出（工具时间线、中期进度），主人据此判断它是否跑在预定路线上。
发现跑偏时，要在**不打断当前回合**的前提下递一句话进去。WebUI 输入框打字后按钮由红「停止本回合」
变**紫**「发送」，回车即发送；**空录入时回车无作用**（防误触"终止本会话"）；消息标「用户交代」，
**跟随下一个工具返回**一起传回 LLM。命令行端不适配，故只在 WebUI 开投递入口。

### 实现落点（agent.py 仍是路由器）

| 层 | 文件 | 改动 |
|---|---|---|
| 核心 | `agent/mid_turn.py`（新，与 agent.py 平级） | 线程安全信箱 + 渲染 + 注入；观察者出口，不反向依赖 WebUI |
| 接线 | `agent/agent.py` | **仅 2 处共 6 行**：`import mid_turn` + 工具批次之后 `flush_after_tools(...)` |
| 通道 | `backend/bridge.py` | `POST /mid_turn`（三道校验）+ 观察者 → SSE + 回合收尾 discard → dropped |
| 通道 | `backend/{api,agent_client,agent_proc}.py`、`backend/events.py` | `/api/chat/mid_turn` 端点、客户端方法、事件常量 |
| 前端 | `frontend/src/{lib/midTurn.ts(新), components/ChatView.tsx, components/MessageList.tsx, store/appStore.tsx, types.ts, api.ts, styles.css}` | 紫色发送态、空回车不动作、消息标识与三态徽标、历史前缀识别 |
| 回归 | `scripts/mid_turn_e2e.py`（新）、`scripts/verify_mid_turn_ui.mjs`（新）、`tests/test_mid_turn.py`（新） | 真机端到端 / 界面层 / 单测 |

### 关键决策（含被真机推翻后修正的一条）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 注入走**独立 user 消息**，绝不拼进 tool 返回 | 拼进去模型会把主人的话当成工具的真实内容 —— 读文件时最危险 |
| 2 | 回合结束仍未送达 → **作废 + 明确回报**（主人选定） | 留着会在下个回合诈尸，让模型执行一个早已不成立的要求 |
| 3 | 多条按到达顺序合并成一条（`1.` `2.` …） | 一次工具返回只插一条，模型视野干净 |
| 4 | 三道校验（有活动回合 / `run_id` 命中 / 会话一致） | 宁可明确拒绝，也不"猜你想投给谁" |
| 5 | **修正（真机暴露）**：`accepted`/`injected` 事件必须带 `run_id` | 首版只有 `dropped` 带；本项目所有事件都靠 `run_id` 标回合归属，少一个字段前端就只能退回会话级判断 |

### 真机端到端（`scripts/mid_turn_e2e.py`，真模型 qwen3.8-flash + 真工具 execute_shell）

```
PASS E2E.0 工具真跑起来了（投递窗口开启） | tool=execute_shell 线程=orch-serial_0
PASS E2E.2 会话不符被拒 | 409「AB 正在跑会话 mide2e_…，本会话的交代无处可投」
PASS E2E.3 假 run_id 被拒 | 404「那一回合已经结束了，请直接发消息」
PASS E2E.1 跑动中投递被受理 | 200 item_id=12e002ac97db pending=1
PASS E2E.1b SSE accepted（带 run_id + item_id） | hub_seq=113
PASS E2E.4 SSE injected（带 run_id + ids） | hub_seq=117
PASS E2E.4b 注入时机正确：在本批工具返回之后 | tool_end hub_seq=[114] < injected hub_seq=117
PASS E2E.5 落盘原文里存在该「用户交代」（带标识前缀） | 【用户交代 · 回合进行中追加】
PASS E2E.5b 它紧跟本批工具返回（独立消息，未污染工具输出） | 前一条 role=tool
PASS E2E.5c tool_call_id 配对完整（会话不会因此断头） | call_848f9b… ⊆ [call_848f9b…]
PASS E2E.5d 它后面是 AB 的收尾回复 | 后一条 role=assistant
PASS E2E.6 AB 回复体现了交代内容 | "紫电青霜…（已按中期交代执行）"
PASS E2E.7c 无活动回合时投递被拒（不悄悄变成新回合） | 409
PASS E2E.8 SSE dropped（回合被中断 → 未送达明确回报） | count=1 reason=turn-ended
PASS E2E.9 作废就是真作废：该交代未落进会话文件 | 落盘命中=False

==== 中期交互端到端：全部通过 (21/21) ====   退出码 0
```

### 界面层真机（`scripts/verify_mid_turn_ui.mjs`，headless Edge + 生产前端 + 真回合）

```
PASS UI.1  空闲态按钮为绿色「发送 ↵」                    cls="btn primary"
PASS UI.2  回合跑动中按钮为红色「⏹ 停止本回合」           cls="btn danger"
PASS UI.3  空录入回车无作用：回合仍在跑（没被误触停止）
PASS UI.4  有录入时按钮变紫「⤴ 发送（用户交代）」         cls="btn mid"
PASS UI.4b 输入框提示切换为中期交互话术
PASS UI.5  回车即发送：消息以「用户交代」身份入列（徽标「⏳ 待送达」）
PASS UI.5b 发送后输入框已清空
PASS UI.5c 挂「待送达」徽标（发出 ≠ 送到）
PASS UI.6  徽标回流转「✅ 已随工具返回注入」
PASS UI.7  AB 回复带上口令（模型真收到中途交代） | "…赤霄断水"
PASS UI.8  刷新后历史里仍标为「用户交代」且已送达

==== 中期交互界面层：全部通过 (14/14) ====
```

### 单测与回归

- `tests/test_mid_turn.py` **24 条**：信箱读写 / 会话隔离 / 上限拒绝（**绝不丢最老的腾位子**）/
  8 线程 × 25 条并发不丢件；渲染（单条不编号、多条按序）；注入形态（空信箱零开销且 conversation
  一字不动、独立 user 消息、tool 消息原封不动、只注一次、跨会话隔离、只在真注入时写日志）；
  观察者（三阶段回调、观察者自己炸了也不影响投递）。
  **契约锁 3 条**：前端 `lib/midTurn.ts` 的标识前缀必须与后端逐字一致（历史消息靠它认出来）、
  `agent.py` 必须挂着注入点且**只挂一次**、`api.ts` 的 EVENTS 白名单不能漏 `mid_turn`
  （漏了 EventSource 永远收不到 —— `clarify` 卡片踩过同类坑）。
- 全量回归：**127 passed / 2 skipped**（原 103 + 新增 24）。
- **CLI 端零影响**（独立子进程实测）：`bridge` 未被导入 → 没有投递入口；信箱恒空、观察者 `None`、
  空信箱 `flush` 返回 0 且 conversation 一字未动。

### 生效层次（回答"改了哪一层要重启什么"）

| 改动 | 生效条件 |
|---|---|
| `agent/mid_turn.py`、`agent/agent.py`、`backend/bridge.py` | **重启 AB 子进程**（UI「优雅关机」→「开机」） |
| `backend/{api,agent_client,agent_proc,events}.py` | **重启网关** |
| 前端 `src/**`、`styles.css` | `npm run build` + **刷新页面**（本轮已构建） |

### 未做 / 遗留（如实记录）

- 未做"多条交代在同一批工具返回前连发"的真机验证：L1 覆盖了合并渲染与顺序（`test_many_turns_share_one_message`
  等），真机只验证了单条与"作废"路径。要真机复现需要精确卡住工具返回窗口，收益低于成本。
- 交代只在回合**运行中**可投递；没有活动回合时接口明确拒绝（409），不替主人"顺手变成一次普通发送"。

---

## 16. 提问多卡（`ask_user` 并行提问，09-14）· 一个回合里同时问 N 个问题

**主人报的现象**：不管并行还是串行调用，一个循环内只能呼出一张 ask 卡。AB 想一次问三个问题时，
并行发出三张卡只有第一张弹得出来；答完它之后第二、三张不弹 —— 要等第一张的答案回到 LLM、
下个回合才可能重新问，效率全耗在来回上。

**根因（先取证，不猜）**：`scripts/repro_multi_clarify.py` 真机取证 —— 三个 `ask_user` **确实并行**进入
clarify 通道（SSE 收到 3 个 `clarify_request`）、服务端台帐里**三张卡同时挂着**、逐个答复也都能收。
**后端无辜，问题全在前端**：`appStore.tsx` 的 `clarify: ClarifyRequest | null` 是**单槽**，
`case 'clarify_request'` 无条件覆盖 → 屏幕上只存在最后到达的那张，答完即清空，其余两张在服务端干等
（与 `approvals` 的数组设计不对称）。这条诊断与主人此前逼三题并发实测的结论一致，AB 的长期记忆里
已有留档（本轮把它从"修（未实施）"变成了落地）。

### 改动

| 层 | 文件 | 改动 |
|---|---|---|
| 窗口 | `backend/clarify_batch.py`（新） | 同批卡**共享一条 deadline**：有人作答即续窗（连续 120s 无人答才算没人应答），硬上限 `120×N+60s`；`batch_timeout(N)=120×N+10s`（N=1 与历史行为一致） |
| 通道 | `backend/bridge.py` | `_clarify_channel` 改为 join → 等共享窗口 → leave；事件带 `batch_size/batch_live/remaining`；`_answer_clarify` 作答后 `touch()` 续窗；新增 `_cancel_all_clarify` + `clarify_resolved` 事件；**删除**并发下会漂移的 `orch.default_timeout` 兜底改写 |
| 编排 | `agent/task_orchestrator.py` | `_NEVER_PARALLEL_TOOLS` 的 `"clarify"` → `"ask_user"`（死条目） |
| 工具 | `backend/ask_user_tool.py` | 撤销"一次只问一个问题"的规避性约定，明确允许并行问独立问题 |
| 前端 | `store/appStore.tsx`、`components/ClarifyCard.tsx`、`types.ts`、`api.ts`、`styles.css`、`events.py` | `clarifies: ClarifyRequest[]`（**队列**）+ 按 `ask_id` 摘卡 + 逐张剪枝 + 共享倒计时 `clarifyWindow`；`ClarifyCard` **只渲染队首** —— 答完一张下一张顶上，屏幕上永远只有一张卡 |

### 真机（`scripts/verify_multi_clarify.py`，真模型 qwen3.8-flash）

```
PASS 1.  同批并行提问：SSE 收到 3 个 clarify_request | 收到 3 个
PASS 1b. 服务端台帐同时挂着 3 张卡（不是排队等） | pending=3
PASS 1c. 事件带批次信息（共几问 / 还剩几问 / 共享剩余秒数）
PASS 1d. 三张卡 timeout 都是 120s，但共享同一条 deadline | [120, 120, 120]
PASS 2.  窗口自然衰减（等 25s） | remaining=90
PASS 2c. **作答把共享窗口续上**（不是继续往下掉） | 答前 90s → 答后 119s
PASS 2d. 台帐还剩 2 张（答一张只摘一张） | pending=2
PASS 3b. 三个答案都进了同一批 → 回复里能同时看到我实际提交的值 ['美式','民谣','图书馆']
         | 回复：**咖啡=美式 ｜ 音乐=民谣 ｜ 周末=图书馆**（各自配对、无错位）
PASS 4b. 终止回合后卡立即出局（不留僵尸卡） | pending=0
PASS 4c. SSE 收到 clarify_resolved | {"reason":"stopped","count":3}
==== 多卡 clarify 真机验收：全部通过 (17/17) ====   退出码 0
```

**自我更正（如实记录）**：第一轮我给三张卡统一回了同一个值，音乐题因此收到"拿铁"（不在该题选项里）——
模型当场指出该值无效。那是**我脚本的缺陷**，而且它让断言 3b 变成假阳性（回复里出现"拿铁"就过了）。
已改成"每张卡答它自己选项里的值"并重跑，断言升级为强证据：三个值各自配对、无错位。

### 界面层真机（`scripts/verify_multi_clarify_ui.mjs`，headless Edge + 生产前端）

**交付形态是逐张**（主人明确要求：独立问题占一张独立卡，答完上一个下一个出现），
所以最硬的判据是「任何时刻都不超过一张卡」+「答完自动换下一张」：

```
PASS UI.1  屏幕上只有一张卡（三个问题没有一起挤上来） | peak=1 now=1 head="🙋 还有 2 个问题 · 单选 120s"
PASS UI.2  卡片告诉你还剩几个问题
PASS UI.2b 卡上有倒计时（共享窗口真值） | countdown="120s"
PASS UI.3  答完第一张 → 下一张自动顶上（张数仍是 1，问题换了） | 上题="今天想听什么音乐？" 现题="更想喝哪种咖啡？"
PASS UI.3b 顶部提示改为「还有 1 个问题」 | head="🙋 还有 1 个问题 · 单选 118s"
PASS UI.4  答完第二张 → 第三张顶上 | 现题="周末更想去哪？"
PASS UI.4b 只剩一个时不再显示「还有」（不啰嗦） | head="🙋 AB 需要你确认 · 单选 118s"
PASS UI.5  答完最后一张 → 卡消失、队列清空 | count=0
PASS UI.6  整批答案一起回到模型 | 回复："美式 / 民谣 / 图书馆。咖啡→美式、音乐→民谣、周末→图书馆，没有串位"
PASS UI.6b 全程没有任何时刻出现超过一张卡 | peak=1
PASS UI.7  刷新后无残留提问卡 | count=0
==== 提问逐张界面层：全部通过 (13/13) ====
```

> 第一版我按"并排堆叠 + 顶部横幅 + 每卡第 N/M 问"做的，主人当场纠偏：**要的是逐张**，
> 而且"弹窗通知不要那么细致，用户也不是傻子"。已按此重做：去掉横幅与逐卡序号、
> 去掉卡下的教学式提示（只保留超时警示），剩余问题数只在还有排队时用一句「还有 N 个问题」带过。

### 单测与回归

- `tests/test_clarify_multi.py` **16 条**：窗口语义（并行共 deadline、后来者只能延后不能缩短、作答续窗、
  硬上限封顶、最后一张出局即关窗、下一批重开、时间用 `now` 参数走**不真等**、40 线程三阶段并发计数不丢）、
  `batch_timeout(N)` 公式；**契约锁**：前端 store 必须是 `clarifies` 数组（不许退回单槽）、
  编排器串行名单必须写工具真名、bridge 必须真接共享窗口。
- `scripts/verify_gate_store.mjs`：**39/39**（新增 8 条多卡断言：三卡并存、批次信息透传、同 id 不堆叠、
  答一张只摘一张、续窗真值、剪枝逐张、`clarify_resolved` 清场、全答完关窗）。
- 全量 pytest：**143 passed / 2 skipped**（127 + 16）。

### 生效层次

| 改动 | 生效条件 |
|---|---|
| `bridge.py`、`clarify_batch.py`、`task_orchestrator.py`、`ask_user_tool.py` | **重启 AB 子进程**（UI 关机→开机） |
| 前端 `src/**`、`styles.css` | `npm run build` + **刷新页面**（本轮已构建） |
