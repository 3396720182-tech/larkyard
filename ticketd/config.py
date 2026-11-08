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


def runs_dir() -> Path:
    """沙箱外执行的脚本快照（worker 写不到这里）。"""
    return home() / "runs"


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


def executor() -> str:
    """新单默认用哪个执行器（ticket new 不带 --executor 时）。"""
    return _env("TICKETD_EXECUTOR", "claude")


def codex_bin() -> str:
    return _env("TICKETD_CODEX", "codex")


def codex_model() -> str:
    """Codex worker 的模型；空为 CODEX_HOME/config.toml 里的默认。和 TICKETD_MODEL 分开：两家模型名不通用。"""
    return _env("TICKETD_CODEX_MODEL", "")


def codex_home() -> str:
    """ticketd 专用的 CODEX_HOME；空则沿用用户的 ~/.codex（此时不写 execpolicy 规则）。"""
    return _env("TICKETD_CODEX_HOME", "")


def codex_git_writable() -> bool:
    """1（默认）= 让 Codex worker 能在授权仓库里 git commit（把 <授权目录>/.git 也列为可写根），和 Claude worker 一致；
    0 = 保留 Codex 自带的 .git 只读保护。"""
    return _env("TICKETD_CODEX_GIT_WRITABLE", "1") != "0"


def lark_bin() -> str:
    return _env("TICKETD_LARK", "lark-cli")


def video_folder() -> str:
    """视频产物上传到的云空间文件夹 token（用户身份）；空 = 用户云空间根目录。"""
    return _env("TICKETD_VIDEO_FOLDER", "")


def model() -> str:
    return _env("TICKETD_MODEL", "")


def max_workers() -> int:
    return int(_env("TICKETD_MAX_WORKERS", "3"))


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(_env(name, str(default))))
    except ValueError:
        return default


def media_workers() -> int:
    """媒体通道里最多同时准备几行（图片/视频上传、稿子导入、视频消息）。不同工单之间并行，同一工单仍按顺序。"""
    return _positive_int("TICKETD_MEDIA_WORKERS", 2)


def media_video_workers() -> int:
    """其中最多几个在上传视频（云空间上传或视频消息）：大文件占带宽、内存，也容易撞飞书限流。不超过 media_workers。"""
    return min(_positive_int("TICKETD_MEDIA_VIDEO_WORKERS", 1), media_workers())


def turn_timeout() -> float:
    return float(_env("TICKETD_TURN_TIMEOUT", "7200"))


def owner() -> str:
    """主用户的 open_id（ou_ 开头）：只为这个人发起的消息派单。没配置时拒绝所有带 --sender 的派单。"""
    return _env("TICKETD_OWNER", "").strip()


def retry_delays() -> list:
    return [float(x) for x in _env("TICKETD_RETRY_DELAYS", "10,40,160").split(",")]


def run_timeout() -> float:
    """一次沙箱外执行最长多久（秒），超时先 SIGTERM，10 秒后 SIGKILL。"""
    return max(1.0, float(_env("TICKETD_RUN_TIMEOUT", "1800")))


def outage_retry_every() -> float:
    """故障等待期间多久自动续跑一次（秒）：短重试用完的网络/接口故障，以及余额不足、认证失败。"""
    return max(1.0, float(_env("TICKETD_OUTAGE_RETRY_EVERY", "900")))


def outage_retry_hours() -> float:
    """故障等待最长多久（小时），超过就记失败；0 = 不进入故障等待，短重试用完直接失败。"""
    return max(0.0, float(_env("TICKETD_OUTAGE_RETRY_HOURS", "24")))


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


def worker_path() -> list:
    """追加到 worker（和沙箱外执行的脚本）PATH 里的目录，冒号分隔。服务由 launchd / systemd 托管，
    PATH 比登录 shell 短，装在 /opt/homebrew/bin、Docker.app 里的工具 worker 找不到（issue #19）。"""
    return [p for p in _env("TICKETD_WORKER_PATH", "").split(os.pathsep) if p.strip()]


def worker_env_names() -> list:
    """白名单：这些变量从 config.env（或服务环境）里取值传给 worker 和沙箱外执行的脚本，逗号分隔。"""
    return [n.strip() for n in _env("TICKETD_WORKER_ENV", "").split(",") if n.strip()]


def worker_env_value(name: str):
    """白名单变量的值：服务环境优先，其次 config.env；都没有返回 None。"""
    if name in os.environ:
        return os.environ[name]
    return _file_values().get(name)


def bin_dir() -> Path:
    return REPO / "bin"
