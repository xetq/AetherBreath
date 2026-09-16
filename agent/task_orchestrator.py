"""
任务编排器（Task Orchestrator）
基于工具调用粒度的并行调度器，满足：
- 8 个 worker 并发，超出排队
- 主进程被 kill 时优雅关闭线程池
- 批量结果聚合后统一返回
- 三种依赖类型处理：数据依赖、文件冲突、交互/危险工具

架构（常驻双通道 + 工具模板 → 管道实例）：
- 编排器【常驻】：实例化一次即创建 串行管道（1 worker）+ 并行管道（8 worker）
  两个常驻线程池，整场会话复用，不再每次 execute() 反复建销线程池
- 路由规则（agent.py 仅作路由器，一切工具执行都经此）：
  · 单工具调用 → 串行管道（serial pool，并发=1）
  · 多工具批 → 依赖分层 → 并行管道（parallel pool，层内并发、层间串行）
- 工具库（AVAILABLE_TOOLS）中的工具以【模板】形式注册，保持只读、永不修改
- 每次执行一个 tool_call 时，编排器以对应工具模板 create_pipeline() 创建
  一个【独立管道实例】来执行该次任务
- 同一工具可以被并发创建多个管道并行执行，各管道状态/结果互不干扰
- RPC 扩展点：ToolPipeline.run() 是唯一执行入口，未来可替换为
  “通过 RPC 通道调用远端工具服务”以实现跨进程执行与批量超时中断
"""

import json
import os
import signal
import threading
import time
import inspect
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Callable, Any, Dict, Optional, Set

# ============================================================
# 1. 常量配置
# ============================================================

MAX_WORKERS = 8  # 最大并发 worker 数

# 需要强制串行的工具（交互、危险）——全局兜底，模板级 never_parallel 优先
# 注意工具名必须写**真名**：这里曾经写着 "clarify"，而 WebUI 注入的提问工具叫
# "ask_user" —— 那条名字从来没匹配上，等于"交互工具要串行"的设计意图从未生效。
_NEVER_PARALLEL_TOOLS = {
    "ask_user",         # 需要用户交互（clarify 提问）
    "delegate_task",    # 派发子任务，需等待
    "terminal",         # 终端交互
    "browser_exec",     # 浏览器交互
    # 自维护（AB 自己集成 MCP server）：它的每个动作都是"读注册表 → 改 → 写回"
    # 的序列，还写失败计数状态文件。同批里并发跑两个（或与别的写盘工具交错）
    # 会**丢更新**——后写的把先写的覆盖掉，表现为"明明集成了却没那条"。
    # 所以强制它单独占一层、串行执行。
    "mcp_manage",
}


# ============================================================
# 2. 数据结构
# ============================================================

@dataclass
class ToolCall:
    """单个工具调用（来自 LLM 的 tool_calls）"""
    id: str
    name: str
    arguments: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# 业务失败标记：部分工具（file_read / execute_shell / rag / execute_python /
# create_tool 共 27 处）遇到业务失败是【返回带 ❌ 前缀的字符串】而不是抛异常。
# 于是 success（=没抛异常）会把这类失败记成成功 —— 日志与界面因此虚报。
# 这里只做"显示层"识别，不参与 success 语义，运行时行为与 LLM 所见文本完全不变。
BIZ_FAIL_MARKS = ("❌",)          # ❌ 真正的错误


def is_business_failure(result: Any) -> bool:
    """返回值本身是否表达业务失败（只看字符串开头的失败标记）。"""
    return isinstance(result, str) and result.lstrip().startswith(BIZ_FAIL_MARKS)


@dataclass
class ToolResult:
    """单个工具执行结果"""
    tool_call_id: str
    success: bool
    result: Any = None
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 业务失败（函数正常返回、但返回值是错误文本）。只用于展示/计数，
    # 不改 success —— 避免污染传给 LLM 的 content 与依赖层判定。
    biz_fail: bool = False


@dataclass
class BatchResult:
    """一批工具调用的聚合结果"""
    results: List[ToolResult]
    success_count: int
    failure_count: int
    total_time: float
    is_interrupted: bool = False


# ============================================================
# 3. 工具模板与管道实例
# ============================================================

class ToolTemplate:
    """
    工具模板：以工具函数为蓝本的可复用定义。

    - 本身【只读】：保存工具名、函数、元信息，不执行任何逻辑
    - 每次执行通过 create_pipeline() 创建独立的管道实例
    - 同一模板可被并发创建多个管道，互不干扰
    - 工具库的原函数对象保持只读——模板仅引用它，不修改、不包装替换
    """

    def __init__(
        self,
        name: str,
        func: Callable,
        timeout: Optional[int] = None,
        max_retries: int = 0,
        never_parallel: bool = False,
        metadata: Dict[str, Any] = None,
    ):
        if not callable(func):
            raise TypeError(f"工具 {name} 不可调用: {type(func).__name__}")
        self.name = name
        self.func = func
        self.timeout = timeout
        self.max_retries = max_retries
        self.never_parallel = never_parallel
        self.metadata = metadata or {}

    def create_pipeline(self, tool_call: ToolCall) -> "ToolPipeline":
        """以本模板为蓝本，为一次具体调用创建独立管道实例"""
        return ToolPipeline(tool_call=tool_call, template=self)

    def __repr__(self) -> str:
        return f"<ToolTemplate {self.name} never_parallel={self.never_parallel}>"


class ToolPipeline:
    """
    一次工具调用的独立执行管道。

    - 绑定一个 ToolCall + 模板引用
    - 拥有独立的执行状态 / 结果 / 时间戳
    - 同一模板的多个管道可并行，互不干扰
    - 状态机: pending → running → done | biz_failed | failed | cancelled
      （biz_failed = 函数正常返回但返回值是错误文本；success 仍为 True，
        只有展示层与计数按失败算）

    RPC 扩展点：run() 是工具执行的唯一入口；未来可改为通过 RPC 通道
    调用远端工具服务，实现跨进程执行与真正的超时中断。
    """

    def __init__(self, tool_call: ToolCall, template: ToolTemplate, created_at: float = None):
        self.tool_call = tool_call
        self.template = template
        self.created_at = created_at if created_at is not None else time.time()
        self.status = "pending"
        self.result: Any = None
        self.error: Optional[str] = None
        self.biz_fail: bool = False      # 异常路径也要有这个属性，别让读的人踩 AttributeError
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._func_kwargs = None  # 存储注入后的参数

    @property
    def elapsed(self) -> float:
        """管道执行耗时（秒）"""
        end = self.finished_at if self.finished_at is not None else time.time()
        start = self.started_at if self.started_at is not None else self.created_at
        return end - start

    def set_kwargs(self, kwargs: Dict[str, Any]):
        """设置注入后的参数（由编排器调用）"""
        self._func_kwargs = kwargs

    def run(self) -> ToolResult:
        """执行管道：真正调用工具函数，维护状态机"""
        if self.status == "running":
            return ToolResult(self.tool_call.id, False, error="管道已在执行中")

        self.status = "running"
        self.started_at = time.time()

        # 使用注入后的参数，如果没有则使用原始 arguments
        kwargs = self._func_kwargs if self._func_kwargs is not None else self.tool_call.arguments

        try:
            self.result = self.template.func(**kwargs)
            # 正常返回，但返回值可能是错误文本：标出来给日志/界面用
            self.biz_fail = is_business_failure(self.result)
            self.status = "done" if not self.biz_fail else "biz_failed"
            return ToolResult(
                self.tool_call.id,
                True,
                result=self.result,
                metadata={"pipeline": repr(self)},
                biz_fail=self.biz_fail,
            )
        except Exception as e:
            self.status = "failed"
            self.error = f"{type(e).__name__}: {str(e)}"
            return ToolResult(self.tool_call.id, False, error=self.error)
        finally:
            self.finished_at = time.time()

    def cancel(self):
        """取消管道（仅 pending 可取消；running 由调用方决定中断策略）"""
        if self.status == "pending":
            self.status = "cancelled"

    def __repr__(self) -> str:
        return f"<ToolPipeline {self.tool_call.id} ({self.template.name}) status={self.status}>"


# ============================================================
# 4. 依赖解析器
# ============================================================

class DependencyResolver:
    """
    解析工具调用之间的依赖关系
    三种依赖类型：
    1. 数据依赖：由模型声明（跨回合串行）
    2. 文件冲突：引擎自动检测（write → read 拆开）
    3. 交互/危险：引擎强制串行（全局 _NEVER_PARALLEL_TOOLS + 模板 never_parallel）
    """

    @staticmethod
    def resolve(
        tool_calls: List[ToolCall],
        tools_map: Optional[Dict[str, "ToolTemplate"]] = None,
    ) -> List[List[ToolCall]]:
        """
        返回分层列表：每层内部可并行，层与层之间串行

        tools_map: 归一化后的模板注册表（可选）；用于读取模板级 never_parallel
        """
        # 第一步：构建基础依赖图
        dep_map = {tc.id: set(tc.depends_on) for tc in tool_calls}

        # 第二步：检测并添加文件冲突依赖
        DependencyResolver._add_file_conflict_deps(tool_calls, dep_map)

        # 第三步：强制串行工具（全局集合 + 模板标记）添加屏障
        DependencyResolver._add_barrier_deps(tool_calls, dep_map, tools_map)

        # 第四步：拓扑排序 → 分层
        return DependencyResolver._topological_layers(tool_calls, dep_map)

    @staticmethod
    def _add_file_conflict_deps(tool_calls: List[ToolCall], dep_map: Dict[str, Set[str]]):
        """检测 write_file + read_file 同一路径 → 强制串行"""
        writes = {}
        reads = {}

        for tc in tool_calls:
            if tc.name == "write_file":
                path = tc.arguments.get("file_path") or tc.arguments.get("path")
                if path:
                    writes[path] = tc.id
            elif tc.name == "read_file":
                path = tc.arguments.get("file_path") or tc.arguments.get("path")
                if path:
                    reads.setdefault(path, []).append(tc.id)

        for path, write_id in writes.items():
            if path in reads:
                for read_id in reads[path]:
                    if read_id != write_id:
                        dep_map[read_id].add(write_id)

    @staticmethod
    def _add_barrier_deps(
        tool_calls: List[ToolCall],
        dep_map: Dict[str, Set[str]],
        tools_map: Optional[Dict[str, "ToolTemplate"]] = None,
    ):
        """
        强制串行工具：
        所有普通工具依赖它，它不依赖任何人。
        效果：该工具单独一层，前后层不能与它并行。

        判定来源（并集）：
        1. 全局 _NEVER_PARALLEL_TOOLS（兼容旧用法）
        2. 模板级 never_parallel=True
        """
        barrier_ids = []
        for tc in tool_calls:
            if tc.name in _NEVER_PARALLEL_TOOLS:
                barrier_ids.append(tc.id)
            elif (
                tools_map
                and tc.name in tools_map
                and getattr(tools_map[tc.name], "never_parallel", False)
            ):
                barrier_ids.append(tc.id)

        if not barrier_ids:
            return

        all_ids = {tc.id for tc in tool_calls}
        normal_ids = all_ids - set(barrier_ids)

        for barrier_id in barrier_ids:
            for normal_id in normal_ids:
                dep_map[normal_id].add(barrier_id)

    @staticmethod
    def _topological_layers(tool_calls: List[ToolCall], dep_map: Dict[str, Set[str]]) -> List[List[ToolCall]]:
        """拓扑排序：返回分层列表"""
        task_map = {tc.id: tc for tc in tool_calls}
        remaining = set(task_map.keys())
        layers = []

        while remaining:
            current_layer = []
            for task_id in list(remaining):
                if all(dep not in remaining for dep in dep_map.get(task_id, set())):
                    current_layer.append(task_map[task_id])

            if not current_layer:
                raise RuntimeError(f"无法解析依赖关系，剩余节点: {remaining}, 依赖图: {dep_map}")

            layers.append(current_layer)
            for task in current_layer:
                remaining.remove(task.id)

        return layers


# ============================================================
# 5. 核心编排器
# ============================================================

class TaskOrchestrator:
    """
    任务编排器
    - 8 个 worker 并发
    - 超时控制
    - 优雅关闭（shutdown hook）
    - 结果聚合
    - 工具模板 → 管道实例：每次 tool_call 创建独立管道执行
    - 自动注入 logger 到支持该参数的工具
    """

    def __init__(
        self,
        max_workers: int = MAX_WORKERS,
        default_timeout: int = 30,
        tools_map: Dict[str, Callable] = None,
        log_enabled: bool = True,
        log_instance=None,
        serial_workers: int = 1,
    ):
        self.max_workers = max_workers
        self.serial_workers = serial_workers
        self.default_timeout = default_timeout
        self.tools_map = self._normalize_tools(tools_map)
        self.log_enabled = log_enabled
        self.log_instance = log_instance

        # ===== 常驻双通道：一次性创建，整场会话复用 =====
        # 串行管道：并发 = serial_workers（默认 1），服务单工具调用等顺序任务
        # 并行管道：并发 = max_workers（默认 8），服务多工具批（依赖分层，层内并行）
        self._serial_pool = ThreadPoolExecutor(
            max_workers=self.serial_workers, thread_name_prefix="orch-serial"
        )
        self._parallel_pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="orch-parallel"
        )

        self._shutdown_event = threading.Event()
        self._active_futures: Dict[Any, str] = {}
        self._lock = threading.Lock()

        self._register_signal_handlers()

    @staticmethod
    def _normalize_tools(tools_map: Optional[Dict[str, Any]]) -> Dict[str, ToolTemplate]:
        """工具库归一化：把裸函数/任意 callable 包装为 ToolTemplate。"""
        normalized: Dict[str, ToolTemplate] = {}
        for name, tool in (tools_map or {}).items():
            if isinstance(tool, ToolTemplate):
                normalized[name] = tool
            else:
                normalized[name] = ToolTemplate(name=name, func=tool)
        return normalized

    # ==================== 日志 ====================

    def _log(self, msg: str, level: str = "INFO"):
        if not self.log_enabled:
            return
        if self.log_instance:
            level_lower = level.lower()
            level_map = {"warn": "warning", "err": "error", "ok": "info"}
            level_lower = level_map.get(level_lower, level_lower)
            getattr(self.log_instance, level_lower)(f"[Orchestrator] {msg}")

    # ==================== 信号处理 ====================

    def _register_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        self._log(f"⚠️ 收到信号 {signum}，正在关闭编排器...", "WARN")
        self.shutdown()

    def set_logger(self, log_instance):
        """运行时更新日志实例（常驻单例跨会话复用时，日志归属当前会话）"""
        self.log_instance = log_instance

    def shutdown(self):
        """关闭编排器：取消所有未完成任务，回收常驻双通道线程池"""
        self._shutdown_event.set()
        self._log("🛑 正在取消未完成的任务...", "WARN")
        with self._lock:
            for future, tc_id in list(self._active_futures.items()):
                future.cancel()
        self._serial_pool.shutdown(wait=False)
        self._parallel_pool.shutdown(wait=False)
        self._log("✅ 编排器已关闭", "OK")

    # ==================== 主体执行 ====================

    def execute(
        self,
        tool_calls: List[ToolCall],
        timeout: Optional[int] = None,
    ) -> BatchResult:
        """
        执行一批工具调用。

        路由规则：单任务批 → 串行管道；多任务批 → 依赖分层 → 并行管道。
        agent.py 作为路由器，所有工具执行统一经此入口。
        """
        if not tool_calls:
            return BatchResult(results=[], success_count=0, failure_count=0, total_time=0.0)

        if self._shutdown_event.is_set():
            return BatchResult(
                results=[ToolResult(tc.id, False, error="编排器已关闭") for tc in tool_calls],
                success_count=0,
                failure_count=len(tool_calls),
                total_time=0.0,
                is_interrupted=True,
            )

        self._log(f"📋 收到 {len(tool_calls)} 个工具调用")
        for tc in tool_calls:
            deps = f" (依赖: {tc.depends_on})" if tc.depends_on else ""
            self._log(f"  └─ {tc.id}: {tc.name}{deps}")

        # ===== 路由：单工具 → 串行管道；多工具批 → 依赖分层 → 并行管道 =====
        if len(tool_calls) == 1:
            self._log("📐 单工具调用 → 串行管道（1 worker）")
            layer_groups = [(self._serial_pool, [tool_calls[0]])]
        else:
            try:
                layers = DependencyResolver.resolve(tool_calls, tools_map=self.tools_map)
                self._log(f"📐 分层结果: {len(layers)} 层 → 并行管道（{self.max_workers} worker）")
                for i, layer in enumerate(layers):
                    ids = [tc.id for tc in layer]
                    self._log(f"  Layer {i}: {ids}")
            except RuntimeError as e:
                self._log(f"❌ 依赖解析失败: {e}", "FAIL")
                return BatchResult(
                    results=[ToolResult(tc.id, False, error=f"依赖解析失败: {e}") for tc in tool_calls],
                    success_count=0,
                    failure_count=len(tool_calls),
                    total_time=0.0,
                )
            layer_groups = [(self._parallel_pool, layer) for layer in layers]

        all_results: Dict[str, ToolResult] = {}
        total_start = time.time()

        total_layers = len(layer_groups)
        for layer_idx, (pool, layer_tasks) in enumerate(layer_groups):
            if self._shutdown_event.is_set():
                self._log(f"⚠️ 执行到 Layer {layer_idx+1} 时被中断", "WARN")
                for tc in tool_calls:
                    if tc.id not in all_results:
                        all_results[tc.id] = ToolResult(tc.id, False, error="执行被中断")
                break

            self._log(f"▶️  执行 Layer {layer_idx+1}/{total_layers} ({len(layer_tasks)} 个任务)")

            layer_results = self._run_on_pool(pool, layer_tasks, timeout)

            for res in layer_results:
                all_results[res.tool_call_id] = res
                # 三态：异常失败 ❌ / 业务失败 ⚠ / 成功 ✅
                if not res.success:
                    mark, shown = "❌", res.error
                elif res.biz_fail:
                    mark, shown = "⚠️", res.result
                else:
                    mark, shown = "✅", res.result
                self._log(f"  {mark} {res.tool_call_id}: {shown}")

            if any((not r.success) or r.biz_fail for r in layer_results):
                self._log(f"⚠️ Layer {layer_idx+1} 有失败任务，继续执行后续层", "WARN")

        total_time = time.time() - total_start

        ordered_results = [all_results.get(tc.id, ToolResult(tc.id, False, error="结果丢失")) for tc in tool_calls]
        # 计数含业务失败，与上面逐条标记保持同一口径（虚报"全成功"就是这里来的）
        success_count = sum(1 for r in ordered_results if r.success and not r.biz_fail)
        failure_count = len(ordered_results) - success_count

        self._log(f"🏁 全部完成 | 成功: {success_count} | 失败: {failure_count} | 耗时: {total_time:.2f}s")

        return BatchResult(
            results=ordered_results,
            success_count=success_count,
            failure_count=failure_count,
            total_time=total_time,
            is_interrupted=self._shutdown_event.is_set(),
        )

    # ==================== 单层执行（常驻池提交） ====================

    def _run_on_pool(self, pool: ThreadPoolExecutor, tasks: List[ToolCall], timeout: Optional[int] = None) -> List[ToolResult]:
        """
        在指定常驻线程池上执行一组任务（池的 worker 数决定并发度）。

        - serial pool（1 worker）→ 任务顺序执行
        - parallel pool（8 worker）→ 同批任务并行执行
        池常驻，不随 execute() 建销；超时的 future 仅标记错误，
        其线程继续在后台跑到结束（真正的超时中断依赖 RPC 化）。
        """
        if not tasks:
            return []

        if self._shutdown_event.is_set():
            return [ToolResult(tc.id, False, error="编排器已关闭") for tc in tasks]

        timeout = timeout or self.default_timeout
        results: List[ToolResult] = []
        if pool is self._serial_pool:
            pool_label = f"串行管道 ×{self.serial_workers}"
        else:
            pool_label = f"并行管道 ×{self.max_workers}"

        self._log(f"🚀 [{pool_label}] 提交 {len(tasks)} 个任务")

        futures = {}
        for tc in tasks:
            future = pool.submit(self._run_one, tc)
            with self._lock:
                self._active_futures[future] = tc.id
            futures[future] = tc.id

        done, not_done = wait(futures.keys(), timeout=timeout)

        for future in done:
            tc_id = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append(ToolResult(tc_id, False, error=f"执行异常: {type(e).__name__} - {str(e)}"))
            finally:
                with self._lock:
                    self._active_futures.pop(future, None)

        for future in not_done:
            tc_id = futures[future]
            future.cancel()
            results.append(ToolResult(tc_id, False, error=f"超时（{timeout} 秒）"))
            with self._lock:
                self._active_futures.pop(future, None)

        return results

    # ==================== 单个工具执行（核心：注入 logger） ====================

    def _run_one(self, tool_call: ToolCall) -> ToolResult:
        """
        执行单个工具调用：
        1. 以工具模板为蓝本创建独立管道实例
        2. 检测工具函数是否支持 logger 参数，如果支持则自动注入
        3. 执行管道
        """
        if self._shutdown_event.is_set():
            return ToolResult(tool_call.id, False, error="编排器已关闭")

        template = self.tools_map.get(tool_call.name)
        if not template:
            return ToolResult(tool_call.id, False, error=f"未知工具: {tool_call.name}")

        if not callable(template.func):
            return ToolResult(tool_call.id, False, error=f"工具 {tool_call.name} 不可调用")

        # ===== 核心：为工具注入 logger（如果支持） =====
        func_kwargs = tool_call.arguments.copy()
        sig = inspect.signature(template.func)
        if "logger" in sig.parameters and self.log_instance is not None:
            func_kwargs["logger"] = self.log_instance

        # 创建管道并注入参数
        pipeline = template.create_pipeline(tool_call)
        pipeline.set_kwargs(func_kwargs)

        self._log(f"   🧵 {tool_call.id}: 模板 '{template.name}' → 管道 #{id(pipeline):x} @{threading.current_thread().name}")

        return pipeline.run()


# ============================================================
# 6. 便捷函数（供 agent.py 调用）
# ============================================================

def orchestrate_tool_calls(
    tool_calls_data: List[Dict[str, Any]],
    tools_map: Dict[str, Callable],
    max_workers: int = MAX_WORKERS,
    default_timeout: int = 30,
    log_enabled: bool = True,
    log_instance=None,
) -> List[Dict[str, Any]]:
    """
    编排工具调用（便捷入口）

    参数:
        tool_calls_data: LLM 返回的 tool_calls 原始数据列表
        tools_map: AVAILABLE_TOOLS 字典（裸函数或 ToolTemplate 均可，内部自动归一化）
        max_workers: 最大并发数
        default_timeout: 默认超时
        log_enabled: 是否开启日志
        log_instance: 日志实例（用于注入到工具）

    返回:
        结果列表，每个元素包含 tool_call_id, content, success
    """
    tool_calls = []
    for tc_data in tool_calls_data:
        func = tc_data.get("function", {})
        tool_calls.append(ToolCall(
            id=tc_data.get("id", ""),
            name=func.get("name", ""),
            arguments=json.loads(func.get("arguments", "{}")),
            depends_on=[],
        ))

    # ---- W1 守卫：本便捷入口自建编排器，会绕过 agent.py 主循环的审批闸门 ----
    # grep 实测当前零调用方；留着不管 = 给未来留一条没人看守的旁路。
    # 确有需要在审批之外批量跑工具时，显式设 AETHER_AUDIT_ALLOW_UNGATED=1。
    _ungated = os.environ.get("AETHER_AUDIT_ALLOW_UNGATED", "").strip().lower()
    if _ungated not in ("1", "true", "yes"):
        raise RuntimeError(
            "orchestrate_tool_calls 会绕过审批闸门（它自建编排器，不经 agent.py 的 gate），"
            "已默认禁用。请改走主循环，或显式设 AETHER_AUDIT_ALLOW_UNGATED=1 承担该风险。")

    orchestrator = TaskOrchestrator(
        max_workers=max_workers,
        default_timeout=default_timeout,
        tools_map=tools_map,
        log_enabled=log_enabled,
        log_instance=log_instance,
    )

    try:
        batch_result = orchestrator.execute(tool_calls)
    finally:
        # 便捷入口是一次性用法：用完即关，避免常驻线程池泄漏
        orchestrator.shutdown()

    output = []
    for res in batch_result.results:
        output.append({
            "tool_call_id": res.tool_call_id,
            "success": res.success,
            "content": str(res.result) if res.success else f"❌ {res.error}",
        })

    return output