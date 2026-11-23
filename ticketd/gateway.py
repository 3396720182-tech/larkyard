"""控制台的模型网关：领网关令牌、给每一单签子令牌（docs/spec.md §12「生图网关」）。

入网的机器上，守护进程在心跳成功后凭设备令牌领一枚网关令牌（POST /api/v1/gateway-token），和随它下发的子令牌钥匙
（sub_id、sub_key）一起存进控制台目录 `$TICKETD_HOME/console/gateway.json`（600）。这个目录 worker 读不到也写不了
（§6「凭据目录」），所以网关令牌和钥匙都不进 worker。

worker 每一轮启动时，守护进程用 sub_key 在本机给这一单签一枚子令牌（不联网），放进 worker 的环境变量：
- `LARKYARD_GATEWAY_TOKEN`：`lys_1.<sub_id>.<工单号>.<到期 Unix 秒>.<HMAC>`，只对这一单有效，单轮时长上限之后过期；
  控制台按它绑定的工单号记账，网关令牌被吊销 / 重新领取、设备吊销 / 退出、人员离职时一起失效。
- `LARKYARD_GATEWAY_URL`：网关地址（路径和火山方舟一样，…/api/v3）。
gen-image 等技能看到这两个变量就默认走网关（在导演组才能用，额度和用量按人记），见技能自己的说明。

- 没领到（旧版控制台、网络问题、控制台限流）：不设这两个变量，下次心跳再领（最多每小时一次）。
- 领到后每小时用网关令牌查一次自己（GET /api/v1/gateway/me）：被吊销（401）就删掉，下次心跳重新领。
- 退出控制台（--leave）、设备被吊销：控制台目录整个删掉 / 心跳停止，不再签子令牌。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from . import config, console, metrics

CLAIM_PATH = "/api/v1/gateway-token"
ME_PATH = "/api/v1/gateway/me"
CLAIM_EVERY = 3600          # 没领到时最多每小时领一次（控制台限每台设备每小时 10 次）
CHECK_EVERY = 3600          # 领到后多久查一次还有没有效（被作废时，worker 最多这么久拿到的是失效的子令牌）
OLD_CONSOLE_BACKOFF = 86400 # 旧版控制台（不给子令牌钥匙）一天才再试一次
TTL_MARGIN = 600            # 子令牌比单轮时长上限多活一点（启动、收尾的时间）
TTL_MAX = 12 * 3600         # 和控制台的 SUB_TTL_MAX 一致：再长控制台不认
GW_TOKEN_RE = re.compile(r"^lyg_[A-Za-z0-9_-]{20,90}$")
SUB_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
TICKET_RE = re.compile(r"^T[0-9]{1,6}$")
NEXT_KEY = "console.gateway_claim_next"   # kv：没领到时，下一次最早什么时候再领
ENV_TOKEN = "LARKYARD_GATEWAY_TOKEN"
ENV_URL = "LARKYARD_GATEWAY_URL"


def path() -> Path:
    return console.console_dir() / "gateway.json"


def load() -> Optional[dict]:
    """本机领到的网关令牌和钥匙；没有、坏了、和本机的控制台对不上返回 None。"""
    try:
        obj = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    sid = obj.get("sub_id")
    if not GW_TOKEN_RE.match(str(obj.get("gateway_token"))) or not SUB_KEY_RE.match(str(obj.get("sub_key"))) \
            or not isinstance(sid, int) or isinstance(sid, bool) or not 0 < sid < 10 ** 12 \
            or not _url_ok(obj.get("base_url")):
        return None
    return obj


def _url_ok(url) -> bool:
    """网关地址要是 https 的完整地址（响应走 TLS 校验过的控制台连接，这里只防格式不对）。"""
    if not isinstance(url, str) or len(url) > 300 or any(c.isspace() for c in url):
        return False
    a = urllib.parse.urlsplit(url)
    return a.scheme == "https" and bool(a.hostname) and not a.query and not a.fragment


def remove() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass


def claim(token: str, now: float) -> dict:
    """凭设备令牌领网关令牌（同一台设备重新领，控制台会作废旧的）。不合规抛 console.ConsoleError。"""
    resp = console.request("POST", CLAIM_PATH, {}, token=token)
    obj = {k: resp.get(k) for k in ("gateway_token", "base_url", "sub_id", "sub_key")}
    obj["claimed_at"] = obj["checked_at"] = now
    if not isinstance(obj["base_url"], str):
        raise console.ConsoleError("BadResponse", "领网关令牌：没有 base_url")
    obj["base_url"] = obj["base_url"].rstrip("/")
    if obj["sub_key"] is None:
        raise console.ConsoleError("BadResponse", "控制台版本太旧，领网关令牌时没给子令牌钥匙")
    if not console.enrolled():   # 领的时候执行了 --leave：不重建控制台目录、不留令牌
        raise console.ConsoleError("NotEnrolled", "领网关令牌期间本机退出了控制台")
    console._write_private(path(), (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"))
    if load() is None:
        remove()
        raise console.ConsoleError("BadResponse", "领网关令牌：响应格式不对")
    return obj


def ensure(con, token: str, now: Optional[float] = None) -> Optional[str]:
    """心跳成功后调用：没领到就领（每小时最多一次），领到了每小时查一次有没有被吊销。
    返回发生了什么："claimed" / "dropped"（被吊销，已删）/ None。网络和控制台的错误照样抛出（调用方记事件）。"""
    now = time.time() if now is None else now
    have = load()
    if have is None:
        try:
            nxt = float(metrics.kv_get(con, NEXT_KEY) or 0)
        except ValueError:
            nxt = 0.0
        if now < nxt <= now + OLD_CONSOLE_BACKOFF:   # 还没到下一次（比这还远说明时钟被拨过，不认）
            return None
        metrics.kv_set(con, NEXT_KEY, str(now + CLAIM_EVERY))
        try:
            claim(token, now)
        except console.ConsoleError as e:
            if e.code == "BadResponse" and "版本太旧" in e.detail:
                # 旧版控制台：每领一次都作废上一枚、多一条审计，退到一天一次
                metrics.kv_set(con, NEXT_KEY, str(now + OLD_CONSOLE_BACKOFF))
            raise
        return "claimed"
    if not 0 <= now - float(have.get("checked_at") or 0) < CHECK_EVERY:
        try:
            console.request("GET", ME_PATH, token=have["gateway_token"])
        except console.ConsoleError as e:
            if e.status == 401:
                remove()
                metrics.kv_set(con, NEXT_KEY, "0")  # 下次心跳马上重新领
                return "dropped"
            raise
        have["checked_at"] = now
        console._write_private(path(), (json.dumps(have, sort_keys=True) + "\n").encode("utf-8"))
    return None


def mint(sub_id: int, sub_key: str, ticket: str, exp: int) -> str:
    """子令牌，和控制台 console/gateway/core.py 的 Gateway.sub_token 逐字节一致。"""
    msg = "lys_1.%d.%s.%d" % (sub_id, ticket, exp)
    mac = base64.urlsafe_b64encode(hmac.new(sub_key.encode("ascii"), msg.encode("ascii"), hashlib.sha256).digest())
    return msg + "." + mac.rstrip(b"=").decode("ascii")


def worker_env(ticket_id: str, now: Optional[float] = None) -> dict:
    """这一轮 worker 要加的环境变量（不联网）；没入网、已停止上报、没领到网关令牌、工单号不对时为空。"""
    try:
        if not TICKET_RE.match(str(ticket_id)) or not console.enrolled() or console.stopped_path().exists():
            return {}
        have = load()
    except OSError:
        return {}
    if have is None:
        return {}
    now = time.time() if now is None else now
    exp = int(now + min(TTL_MAX, config.turn_timeout() + TTL_MARGIN))
    return {ENV_TOKEN: mint(have["sub_id"], have["sub_key"], ticket_id, exp), ENV_URL: have["base_url"]}


def status_text() -> str:
    have = load()
    if have is None:
        return "还没领到网关令牌（心跳成功后自动领；旧版控制台没有网关）"
    return "已领到网关令牌（%s 领取，worker 每轮只拿到只对这一单有效的子令牌）" % console._ts(have.get("claimed_at"))
