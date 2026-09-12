#!/usr/bin/env python
"""一键启动脚本（跨平台）：自动准备环境并同时拉起后端与前端开发服务器。

用法：
    python start.py              # 前后端一起起（开发模式，前端 5173 + 后端 8000）
    python start.py --backend    # 仅启动后端
    python start.py --frontend   # 仅启动前端
    python start.py --setup-only # 只装依赖，不启动服务
    python start.py --skip-install
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"
IS_WINDOWS = os.name == "nt"

if sys.version_info < (3, 10):
    sys.exit(
        f"需要 Python 3.10 及以上版本，当前为 {sys.version.split()[0]}。\n"
        "请从 https://www.python.org/downloads/ 安装后重试。"
    )


def _enable_ansi() -> None:
    """Windows 控制台默认不解析 ANSI 转义序列，不开启的话日志里会满是 `←[36m`。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 7 = ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


def _tolerate_unencodable() -> None:
    """把标准输出的编码错误降级为 `?`，不要因为一个字符编码不了就崩掉启动器。

    这里**不改编码**，只改错误处理策略：控制台走什么编码（start.bat 里 chcp 65001
    就是 UTF-8，直接跑 python 则为系统 ANSI 代码页）就继续用什么编码，因为强行改
    成 UTF-8 反而会让非 65001 的控制台显示乱码。单纯保证不会抛 UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - 极少数流不支持
            pass


_enable_ansi()
_tolerate_unencodable()


def log(msg: str, tag: str = "start") -> None:
    print(f"\033[36m[{tag}]\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"\033[33m[warn]\033[0m {msg}", flush=True)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    log(" ".join(str(c) for c in cmd))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if check and proc.returncode != 0:
        raise SystemExit(f"命令执行失败（退出码 {proc.returncode}）: {' '.join(map(str, cmd))}")
    return proc.returncode


# --------------------------------------------------------------------------
# 环境准备
# --------------------------------------------------------------------------
def ensure_env_file() -> None:
    env_file, example = ROOT / ".env", ROOT / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copy(example, env_file)
        warn("已从 .env.example 生成 .env —— 请填入 OPENAI_API_KEY，或设 MOCK_LLM=true 先跑通链路")


def ensure_backend(install: bool) -> Path:
    py = venv_python()
    if not py.exists():
        log("未检测到虚拟环境，正在创建 .venv …")
        run([sys.executable, "-m", "venv", str(VENV)])

    if install:
        log("安装后端依赖 …")
        run([str(py), "-m", "pip", "install", "--upgrade", "pip", "-q"])
        run([str(py), "-m", "pip", "install", "-r", str(BACKEND / "requirements.txt")])
    return py


def ensure_frontend(install: bool) -> None:
    if not FRONTEND.exists():
        raise SystemExit("未找到 frontend 目录")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("未检测到 npm，请先安装 Node.js 18+：https://nodejs.org")

    if install:
        if not (FRONTEND / "node_modules").exists():
            log("安装前端依赖（首次较慢）…")
            run([npm, "install", "--no-audit", "--no-fund"], cwd=FRONTEND)
        else:
            log("前端依赖已存在，跳过安装")


# --------------------------------------------------------------------------
# 进程编排
# --------------------------------------------------------------------------
class Launcher:
    def __init__(self) -> None:
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self._stop = threading.Event()

    def spawn(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.procs.append((name, proc))
        threading.Thread(target=self._pump, args=(name, proc), daemon=True).start()

    @staticmethod
    def _pump(name: str, proc: subprocess.Popen) -> None:
        colors = {"backend": "\033[32m", "frontend": "\033[35m"}
        color, reset = colors.get(name, "\033[37m"), "\033[0m"
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"{color}[{name}]{reset} {line.rstrip()}", flush=True)

    def shutdown(self, *_args) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        print()
        log("正在停止服务 …")
        for name, proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print(f"  已停止 {name}")

    def run_forever(self) -> int:
        signal.signal(signal.SIGINT, self.shutdown)
        try:
            signal.signal(signal.SIGTERM, self.shutdown)
        except (AttributeError, ValueError):  # pragma: no cover - Windows 上部分信号不可用
            pass
        try:
            while not self._stop.is_set():
                for name, proc in self.procs:
                    code = proc.poll()
                    if code is not None:
                        warn(f"{name} 已退出（退出码 {code}），正在停止其余服务")
                        self.shutdown()
                        return code
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="多语种UI文本格式检测系统 —— 一键启动")
    parser.add_argument("--backend", action="store_true", help="仅启动后端")
    parser.add_argument("--frontend", action="store_true", help="仅启动前端")
    parser.add_argument("--setup-only", action="store_true", help="仅安装依赖")
    parser.add_argument("--skip-install", action="store_true", help="跳过依赖安装")
    parser.add_argument("--host", default=None, help="后端监听地址")
    parser.add_argument("--port", default=None, help="后端监听端口")
    args = parser.parse_args()

    install = not args.skip_install
    ensure_env_file()

    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # 读取 .env 中的端口配置，让前后端代理保持一致
    env_file = ROOT / ".env"
    env_values: dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_values[k.strip()] = v.strip()

    backend_port = args.port or env_values.get("PORT") or "8000"
    backend_host = args.host or env_values.get("HOST") or "0.0.0.0"
    frontend_port = env_values.get("FRONTEND_PORT") or "5173"

    only_both = not (args.backend or args.frontend)
    launcher = Launcher()

    if only_both or args.backend:
        py = ensure_backend(install)
        if args.setup_only:
            if only_both or args.frontend:
                ensure_frontend(install)
            log("依赖安装完成，未启动服务（--setup-only）")
            return 0
        launcher.spawn(
            "backend",
            [
                str(py), "-m", "uvicorn", "backend.app.main:app",
                "--host", str(backend_host), "--port", str(backend_port),
                "--app-dir", str(ROOT),
            ],
            cwd=ROOT,
            env=env,
        )

    if only_both or args.frontend:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        try:
            ensure_frontend(install)
        except SystemExit as exc:
            if args.frontend:
                raise
            warn(f"{exc} —— 将只启动后端（可用前端产物由后端直接托管）")
            npm = None
        if args.setup_only:
            log("依赖安装完成，未启动服务（--setup-only）")
            return 0
        if npm:
            fe_env = env.copy()
            fe_env["VITE_BACKEND_ORIGIN"] = f"http://127.0.0.1:{backend_port}"
            fe_env["VITE_PORT"] = str(frontend_port)
            launcher.spawn("frontend", [npm, "run", "dev"], cwd=FRONTEND, env=fe_env)

    if not launcher.procs:
        warn("没有任何服务被启动")
        return 1

    print()
    log("=" * 62)
    if only_both or args.backend:
        log(f"后端 API 文档:  http://127.0.0.1:{backend_port}/docs")
    if len(launcher.procs) > 1:
        log(f"前端页面:       http://127.0.0.1:{frontend_port}")
    log("按 Ctrl+C 停止全部服务")
    log("=" * 62)
    print()
    return launcher.run_forever()


if __name__ == "__main__":
    sys.exit(main())
