"""临时文件安全删除：防 WorkBuddy 沙箱 tsbx.dll 钩子死锁阻塞管线。

背景（2026-10-01 实测，代价一整条编排流 20+ 分钟不收敛）
------------------------------------------------------------
WorkBuddy 托管运行时自带的沙箱 `tsbx.dll`（`cli/vendor/sandbox/5.6.10/`）会挂钩
`DeleteFileW`，把删除重定向到 `SHFileOperation`（回收站语义），并在 COM 临界区上
**死锁**（py-spy 原生栈：`DeleteFileW → tsbx.dll → SHFileOperationWithAdditionalFlags
→ CoInitializeEx → RtlEnterCriticalSection → 永久等待`）。

关键点：Python 层的 `sitecustomize._safe_remove` 对 `%TEMP%` 有旁路，但旁路之后调用的
仍是**原生 `DeleteFileW` —— 它照样被 tsbx.dll 拦截**。所以「临时目录旁路」救不了这一层。

实测编排链路的 `sysml_ast.parse_ast` 在 `finally: os.remove(临时 .sysml)` 上卡了 40+ 分钟，
生成器线程永远到不了落库 → 表现就是「4 个子任务全 done、refine 也走完，但链路不收敛、
助手消息不落库」。这不是 LLM、不是 checker、不是正则 —— 是**删除一个临时文件**。

本模块只解决一件事：**临时文件 / 清理类删除，永远不能阻塞调用方**。
删除交给 daemon 线程，超时未完成就放弃（泄漏一个文件，由 OS 临时目录清理兜底），
并打印告警。**用户主动删除文件（file_tools._do_delete）不走这里** —— 那类删除需要
向用户准确返回成功/失败，语义不同，见 AGENTS.md 该处注释。
"""
import os
import threading

#: 看门狗超时（秒）：实测正常删除 <50ms；给足余量防误杀，又远小于「卡死」的量级。
DEFAULT_WATCHDOG_TIMEOUT = 5.0


def bounded_unlink(path, timeout=DEFAULT_WATCHDOG_TIMEOUT, deleter=None):
    """在 daemon 线程里删文件，最多等 `timeout` 秒。

    返回 True  = 已删除 / 本就不存在（含删除时报错也降级为 False，见下）
    返回 False = 超时泄漏（删除线程仍卡着）或删除抛异常 —— **绝不抛、绝不阻塞调用方**

    参数：
      path     要删的文件路径
      timeout  看门狗上限（秒）；测试可传小值模拟「慢删除」
      deleter  仅测试注入用（默认 os.remove），便于构造「永远挂起」的假删除函数做变异自证
    """
    deleter = deleter or os.remove
    if not os.path.exists(path):
        return True
    state = {}

    def _run():
        try:
            deleter(path)
            state["done"] = True
        except Exception as exc:  # noqa: BLE001 —— 临时文件删除失败一律降级，不抛
            state["err"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # 看门狗超时：删除线程仍卡着（tsbx 死锁）。泄漏该文件，绝不阻塞调用方。
        # 那个 daemon 线程可能永远挂着 —— daemon=True 保证不挡进程退出；每次泄漏 +1 线程，
        # 属极端罕见路径（只有沙箱死锁才触发），可接受。
        print(f"[fs_guard] 临时文件删除超时（>{timeout}s），已放弃并泄漏: {path}", flush=True)
        return False
    if "err" in state:
        # 删除抛异常（如权限）：临时文件同样降级，记录后继续，不把清理失败升级成管线失败。
        print(f"[fs_guard] 临时文件删除失败（降级，不阻断）: {path} -> {state['err']!r}", flush=True)
        return False
    return True
