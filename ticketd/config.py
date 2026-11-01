from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def home() -> Path:
    return Path(os.environ.get("TICKETD_HOME", str(Path.home() / ".ticketd"))).expanduser()


def config_file() -> Path:
    """个人配置：$TICKETD_HOME/config.env，一行一个 KEY=VALUE。环境变量优先于这个文件。"""
    return home() / "config.env"


_file_cache: dict = {}


def _file_values() -> dict:
    p = config_file()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    hit = _file_cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    values = {}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        values[k.strip()] = v
    _file_cache[str(p)] = (mtime, values)
    return values


def _env(name: str, default: str) -> str:
    if name in os.environ:
        return os.environ[name]
    return _file_values().get(name, default)


def db_dir() -> Path:
    return home() / "db"


def db_path() -> Path:
    return db_dir() / "ticketd.db"


def heartbeat_path() -> Path:
    return home() / "heartbeat"


def settings_dir() -> Path:
    return home() / "settings"


def logs_dir() -> Path:
    return home() / "logs"


def workroot() -> Path:
    return Path(_env("TICKETD_WORKROOT", str(Path.home() / "larkyard-tickets"))).expanduser()


def claude_bin() -> str:
    return _env("TICKETD_CLAUDE", "claude")


def lark_bin() -> str:
    return _env("TICKETD_LARK", "lark-cli")


def model() -> str:
    return _env("TICKETD_MODEL", "")


def max_workers() -> int:
    return int(_env("TICKETD_MAX_WORKERS", "3"))


def turn_timeout() -> float:
    return float(_env("TICKETD_TURN_TIMEOUT", "7200"))


def owner() -> str:
    """主用户的 open_id（ou_ 开头）：只为这个人发起的消息派单。没配置时拒绝所有带 --sender 的派单。"""
    return _env("TICKETD_OWNER", "").strip()


def retry_delays() -> list:
    return [float(x) for x in _env("TICKETD_RETRY_DELAYS", "10,40,160").split(",")]


def card_heartbeat() -> float:
    """运行中的状态卡没有阶段变化时，多久刷新一次（秒）。"""
    return float(_env("TICKETD_CARD_HEARTBEAT", "150"))


def round_card_delay() -> float:
    """追问等后续轮次跑了多久还没出结果，才给这一轮另发一张状态卡（秒）。短的轮次只推结果。"""
    return float(_env("TICKETD_ROUND_CARD_DELAY", "120"))


def stall_seconds() -> float:
    """运行中多久没有任何工具调用就判定为疑似卡住、主动推一条消息。"""
    return float(_env("TICKETD_STALL_SECONDS", "600"))


def linger_grace() -> float:
    """worker 已交终态汇报、本轮也已结束，但进程还挂着（在等后台任务）时，再等多久就结束它。"""
    return float(_env("TICKETD_LINGER_GRACE", "60"))


def prompt_file() -> Path:
    return REPO / "prompts" / "worker.md"


def bin_dir() -> Path:
    return REPO / "bin"
