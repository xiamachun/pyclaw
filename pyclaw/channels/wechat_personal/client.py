"""
个人微信客户端

基于 itchat-uos 实现个人微信消息收发，
通过 Gateway API 与 PyClaw Agent 交互。
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

import httpx

logger = logging.getLogger("pyclaw.channels.wechat_personal")

# itchat 延迟导入，避免未安装时影响其他模块
_itchat = None


def _disable_ssl_verification():
    """Disable SSL verification globally (for corporate proxies with self-signed certificates)"""
    import ssl
    import urllib3
    import requests as _requests

    os.environ["PYTHONHTTPSVERIFY"] = "0"
    os.environ["CURL_CA_BUNDLE"] = ""

    # 禁用 urllib3 SSL 警告
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # 设置默认 SSL context 不验证证书
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except AttributeError:
        pass

    # Patch requests.Session 的 verify 默认值
    _original_init = _requests.Session.__init__

    def _patched_init(self_session, *args, **kwargs):
        _original_init(self_session, *args, **kwargs)
        self_session.verify = False

    _requests.Session.__init__ = _patched_init

    logger.info("SSL verification disabled globally (for corporate proxy compatibility)")


def _patch_itchat_test_connect():
    """Monkey-patch itchat.utils.test_connect to skip SSL verification.

    itchat's test_connect uses requests.get(url) without verify=False,
    which fails under corporate proxies with self-signed certificates.
    """
    import requests as _requests
    from itchat import utils as _itchat_utils

    def _patched_test_connect(retryTime=5):
        for i in range(retryTime):
            try:
                _requests.get("https://login.weixin.qq.com", verify=False, timeout=10)
                return True
            except Exception:
                if i == retryTime - 1:
                    logger.error("无法连接微信服务器（已跳过 SSL 验证）")
                    return False
        return False

    _itchat_utils.test_connect = _patched_test_connect
    logger.info("已 patch itchat.utils.test_connect（跳过 SSL 验证）")


_ssl_disabled = False

def _get_itchat():
    """延迟导入 itchat（导入前先禁用 SSL 验证）"""
    global _itchat, _ssl_disabled
    if _itchat is None:
        if not _ssl_disabled:
            _disable_ssl_verification()
            _ssl_disabled = True
        try:
            import itchat
            _itchat = itchat
            # 导入后 patch itchat 的 test_connect
            _patch_itchat_test_connect()
        except ImportError:
            raise ImportError(
                "itchat-uos is required for WeChat Personal channel. "
                "Install it with: pip install itchat-uos"
            )
    return _itchat


# ---------------------------------------------------------------------------
# 数据持久化路径
# ---------------------------------------------------------------------------

_DATA_DIR = Path.home() / ".pyclaw" / "wechat_personal"
_SESSIONS_FILE = _DATA_DIR / "sessions.json"
_ITCHAT_DIR = _DATA_DIR / "itchat_data"


def _ensure_dirs():
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ITCHAT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 单例客户端
# ---------------------------------------------------------------------------


class WeChatPersonalClient:
    """个人微信客户端（单例）"""

    _instance: Optional["WeChatPersonalClient"] = None

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:18789",
        gateway_token: Optional[str] = None,
    ):
        self.gateway_url = gateway_url
        self.gateway_token = gateway_token

        # 登录状态
        self._logged_in = False
        self._login_thread: Optional[threading.Thread] = None
        self._qr_base64: Optional[str] = None
        self._qr_ready = threading.Event()
        self._login_success = threading.Event()  # 登录成功事件
        self._nickname: Optional[str] = None
        self._username: Optional[str] = None

        # 会话管理
        self._sessions: dict[str, list] = self._load_sessions()

        # 消息回调（用于 WebSocket 推送等）
        self._on_message_callback: Optional[Callable] = None

        _ensure_dirs()

    @classmethod
    def get_instance(
        cls,
        gateway_url: str = "http://127.0.0.1:18789",
        gateway_token: Optional[str] = None,
    ) -> "WeChatPersonalClient":
        if cls._instance is None:
            cls._instance = cls(gateway_url, gateway_token)
        return cls._instance

    # ------------------------------------------------------------------
    # 登录管理
    # ------------------------------------------------------------------

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def qr_base64(self) -> Optional[str]:
        return self._qr_base64

    @property
    def nickname(self) -> Optional[str]:
        return self._nickname

    def start_login(self) -> str:
        """启动登录流程，返回二维码 base64

        在后台线程中运行 itchat 登录，主线程通过轮询获取状态。
        """
        if self._logged_in:
            return ""

        if self._login_thread and self._login_thread.is_alive():
            # 登录流程已在进行中，等待二维码
            if self._qr_base64:
                return self._qr_base64
            self._qr_ready.wait(timeout=15)
            return self._qr_base64 or ""

        # 重置状态
        self._qr_base64 = None
        self._qr_ready.clear()
        self._login_success.clear()

        # 在后台线程启动 itchat
        self._login_thread = threading.Thread(
            target=self._login_worker,
            daemon=True,
            name="wechat-login",
        )
        self._login_thread.start()

        # 等待二维码生成（最多 20 秒）
        self._qr_ready.wait(timeout=20)
        return self._qr_base64 or ""

    def _login_worker(self):
        """后台线程：运行 itchat 登录和消息循环

        itchat.auto_login() 内部有 while self.isLogging 无限循环，
        扫码超时后会一直重新生成 QR 码永不返回。
        因此需要：
        1. 用定时器在超时后设置 isLogging=False 中断循环
        2. 显式对 itchat session 设置 verify=False
        3. monkey-patch check_login 增加诊断日志
        """
        itchat = _get_itchat()

        try:
            import sys
            print("[WECHAT-DEBUG] _login_worker started", flush=True)
            sys.stdout.flush()
            logger.info("开始微信登录流程...")

            # ── 确保 itchat 内部 session 也禁用 SSL 验证 ──────────
            _core = getattr(itchat, 'instance', itchat)
            if hasattr(_core, 's') and _core.s is not None:
                _core.s.verify = False
                logger.info("已对 itchat core.s 设置 verify=False")

            # ── monkey-patch login() 移除 alive/isLogging 检查 ───────
            # itchat 的 login() 在 alive/isLogging 为 True 时直接 return，
            # 导致重试时无法重新登录。补丁：自动重置这些标志。
            from itchat.core import Core as _CoreClass
            from itchat.components import login as _login_mod
            _orig_login_fn = _login_mod.login

            def _patched_login(self_core, enableCmdQR=False, picDir=None,
                              qrCallback=None, loginCallback=None, exitCallback=None):
                # 强制重置，允许重试
                if self_core.alive or self_core.isLogging:
                    logger.info("itchat alive/isLogging 为 True，强制重置以允许重试")
                    self_core.alive = False
                    self_core.isLogging = False
                return _orig_login_fn(self_core, enableCmdQR=enableCmdQR,
                                      picDir=picDir, qrCallback=qrCallback,
                                      loginCallback=loginCallback, exitCallback=exitCallback)

            _CoreClass.login = _patched_login
            logger.info("login() 已 monkey-patch 到 Core class（移除 alive/isLogging 锁）")

            # ── monkey-patch check_login 增加诊断日志 ─────────────
            # itchat binds module-level functions to core via load_login():
            #   core.check_login = login.check_login
            # Direct assignment to instance does NOT auto-bind self, so we
            # must use types.MethodType to create a proper bound method.
            import types as _types
            from itchat.components import login as _login_mod
            # itchat.instance is None; the real Core lives in instanceList[0]
            # or can be found via itchat.check_login.__self__
            _core = (
                itchat.instanceList[0]
                if hasattr(itchat, 'instanceList') and itchat.instanceList
                else itchat.check_login.__self__
            )
            _orig_check_login_fn = _login_mod.check_login

            # Track whether QR has been scanned to adjust tip parameter
            _scanned = [False]

            def _patched_check_login(core_self, uuid=None):
                import re as _re
                import time as _time
                from itchat.utils import config as _config

                uuid = uuid or core_self.uuid
                url = '%s/cgi-bin/mmwebwx-bin/login' % _config.BASE_URL
                local_time = int(_time.time())
                # tip=1 for first check (long poll), tip=0 after scan (long poll for confirm)
                tip = 0 if _scanned[0] else 1
                params = 'loginicon=true&uuid=%s&tip=%s&r=%s&_=%s' % (
                    uuid, tip, int(-local_time / 1579), local_time)
                headers = {'User-Agent': _config.USER_AGENT}

                t0 = _time.monotonic()
                try:
                    response = core_self.s.get(url, params=params, headers=headers,
                                               timeout=35)
                except Exception as exc:
                    elapsed = _time.monotonic() - t0
                    logger.warning("check_login request error (%.1fs): %s", elapsed, exc)
                    return '408'

                elapsed = _time.monotonic() - t0
                data = _re.search(r'window.code=(\d+)', response.text)
                if data and data.group(1) == '200':
                    _scanned[0] = False
                    logger.info("check_login -> status=200 (%.1fs) LOGIN SUCCESS", elapsed)
                    if _login_mod.process_login_info(core_self, response.text):
                        return '200'
                    return '400'
                elif data:
                    code = data.group(1)
                    if code == '201':
                        _scanned[0] = True
                    logger.info("check_login -> status=%s (%.1fs)", code, elapsed)
                    return code
                else:
                    logger.warning("check_login -> no code in response (%.1fs)", elapsed)
                    return '400'

            # Patch module-level function
            _login_mod.check_login = _patched_check_login
            # Bind to core instance as a proper method (so self is passed)
            _core.check_login = _types.MethodType(_patched_check_login, _core)
            logger.info("check_login patched (module + bound method on core)")

            # ── wrap process_login_info with logging + KeyError protection ───
            # itchat-uos uses cookies for wxsid/wxuin (UOS patch).
            # We keep the original implementation but add logging and
            # catch KeyError if cookies are missing.
            _orig_process_login = _login_mod.process_login_info

            def _patched_process_login(core, loginContent):
                try:
                    result = _orig_process_login(core, loginContent)
                    if result:
                        logger.info("process_login_info succeeded (original itchat-uos)")
                    else:
                        logger.error(
                            "process_login_info returned False. "
                            "This WeChat account may be restricted from web login (error 1203)."
                        )
                    return result
                except KeyError as exc:
                    logger.error(
                        "process_login_info KeyError: %s. "
                        "cookies=%s. This account is likely restricted from web WeChat login.",
                        exc, list(core.s.cookies.get_dict().keys()),
                        exc_info=True,
                    )
                    core.isLogging = False
                    return False

            _login_mod.process_login_info = _patched_process_login
            logger.info("process_login_info 已 monkey-patch")

            # ── 验证并清理可能损坏的 hotReload 缓存 ──────────────
            pkl_path = _ITCHAT_DIR / "itchat.pkl"
            if pkl_path.exists():
                try:
                    import pickle
                    with open(pkl_path, "rb") as f:
                        data = pickle.load(f)
                    if not isinstance(data, dict):
                        raise ValueError("缓存不是 dict")
                    login_info = data.get("loginInfo", {})
                    if not isinstance(login_info, dict) or "User" not in login_info:
                        raise ValueError(f"loginInfo 缺少 User 键: {list(login_info.keys())[:5]}")
                    logger.info("itchat.pkl 缓存有效，尝试热重载")
                except Exception as cache_err:
                    logger.warning(f"itchat.pkl 缓存无效（{cache_err}），删除重建")
                    pkl_path.unlink(missing_ok=True)

            # ── 二维码回调 ────────────────────────────────────────
            # itchat 在两个地方调用 qrCallback:
            #   1) get_QR() 首次生成时: status="0", qrcode=png_bytes
            #   2) check_login 轮询时: status="200"/"201"/"408" 等
            # 我们在 status=="0" 和外层循环重新生成 QR 时都更新 base64
            _last_qr_status = [None]  # mutable container for closure

            def qr_callback(uuid=None, status=None, qrcode=None):
                if status == "0" and qrcode:
                    encoded = base64.b64encode(qrcode).decode("utf-8")
                    self._qr_base64 = f"data:image/png;base64,{encoded}"
                    self._qr_ready.set()
                    logger.info("WeChat QR code generated (status=0)")
                elif status == "201":
                    # Only log once to avoid flooding
                    if _last_qr_status[0] != "201":
                        logger.info("QR scanned, waiting for phone confirmation...")
                elif status == "200":
                    logger.info("Login confirmed (status=200)")
                _last_qr_status[0] = status

            use_hot_reload = pkl_path.exists()
            logger.info(f"微信登录: hotReload={use_hot_reload}, cache_exists={pkl_path.exists()}")

            # ── 带重试的登录流程 ──────────────────────────────────
            max_retries = 5
            retry_delays = [5, 10, 20, 30, 30]  # 递增等待
            for attempt in range(max_retries):
                logger.info(f"微信登录尝试 {attempt + 1}/{max_retries}")

                # 重置 itchat 内部状态（防止 "itchat has already logged in"）
                # 必须在 core instance 上重置，因为 auto_login 内部检查 self.alive / self.isLogging
                _core = getattr(itchat, 'instance', itchat)
                _core.alive = False
                _core.isLogging = False
                logger.info(f"itchat core 状态已重置: alive={_core.alive}, isLogging={_core.isLogging}")

                # 重建 session（上次失败可能导致连接池损坏）
                import requests as _req
                _core.s = _req.Session()
                _core.s.verify = False
                logger.info("itchat core session 已重建 (verify=False)")

                # 定时器：防止 auto_login 内部无限循环
                _login_deadline = threading.Event()

                def _stop_login_loop():
                    from pyclaw.constants import WECHAT_PERSONAL_LOGIN_TIMEOUT_SECONDS
                    if not _login_deadline.wait(timeout=WECHAT_PERSONAL_LOGIN_TIMEOUT_SECONDS):
                        if not self._logged_in:
                            logger.warning(
                                "WeChat login timed out after %d seconds, "
                                "interrupting itchat login loop",
                                WECHAT_PERSONAL_LOGIN_TIMEOUT_SECONDS,
                            )
                            _core.isLogging = False

                timer_thread = threading.Thread(target=_stop_login_loop, daemon=True)
                timer_thread.start()

                def on_login_success():
                    self._on_login()
                    _login_deadline.set()  # 取消定时器
                    logger.info("loginCallback 被调用，登录成功")

                try:
                    itchat.auto_login(
                        hotReload=use_hot_reload,
                        statusStorageDir=str(pkl_path),
                        qrCallback=qr_callback,
                        loginCallback=on_login_success,
                        exitCallback=self._on_logout,
                        enableCmdQR=False,
                    )
                except Exception as login_exc:
                    _login_deadline.set()  # 取消定时器
                    logger.warning(f"微信登录尝试 {attempt + 1} 异常: {login_exc}")
                    if attempt < max_retries - 1:
                        wait = retry_delays[attempt]
                        logger.info(f"等待 {wait} 秒后重试...")
                        time.sleep(wait)
                        # 清除可能损坏的缓存后重试
                        use_hot_reload = False
                        pkl_path.unlink(missing_ok=True)
                        continue
                    else:
                        logger.error("微信登录最终失败", exc_info=True)
                        return

                # auto_login 正常返回，检查是否登录成功
                if self._logged_in:
                    logger.info("微信登录成功")
                    break
                else:
                    _login_deadline.set()  # 取消定时器
                    logger.warning(f"auto_login 返回但未登录成功，尝试 {attempt + 1}/{max_retries}")
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        use_hot_reload = False
                        pkl_path.unlink(missing_ok=True)
                    continue

            if not self._logged_in:
                logger.error("微信登录所有重试均失败")
                return

            # ── 注册消息处理器 ────────────────────────────────────
            # 处理两类消息：
            #   1) 别人发给我的私聊消息（自动回复）
            #   2) 我自己发给「文件传输助手」的消息（自聊模式）
            my_username = itchat.storageClass.userName

            @itchat.msg_register(itchat.content.TEXT)
            def on_text(msg):
                from_user = msg.get("FromUserName", "")
                to_user = msg.get("ToUserName", "")

                if from_user == my_username:
                    # 我自己发的消息 —— 只处理发给「文件传输助手」的
                    if to_user == "filehelper":
                        logger.info(f"文件助手自聊: {msg.get('Text', '')[:50]}")
                        self._handle_message(msg)
                    # 其他我自己发的消息不处理（避免干扰正常聊天）
                else:
                    # 别人发给我的消息 —— 自动回复
                    self._handle_message(msg)

            @itchat.msg_register(itchat.content.TEXT, isGroupChat=True)
            def on_group_text(msg):
                if msg.get("isAt", False):
                    self._handle_message(msg, is_group=True)

            # 启动消息循环（阻塞）
            logger.info("微信消息循环启动")
            itchat.run(debug=False, blockThread=True)

        except Exception as exc:
            logger.error(f"微信登录/消息循环异常: {exc}", exc_info=True)
            self._logged_in = False

    def _on_login(self):
        """登录成功回调"""
        itchat = _get_itchat()
        self._logged_in = True
        self._qr_base64 = None  # 清除二维码
        self._login_success.set()  # 通知登录成功

        # 获取当前用户信息
        try:
            user_info = itchat.search_friends()
            self._nickname = user_info.get("NickName", "未知")
            self._username = user_info.get("UserName", "")
            logger.info(f"微信登录成功: {self._nickname}")
        except Exception:
            self._nickname = "已登录"
            logger.info("微信登录成功")

    def _on_logout(self):
        """登出回调"""
        self._logged_in = False
        self._nickname = None
        self._username = None
        logger.info("微信已登出")

    def logout(self):
        """主动登出"""
        if self._logged_in:
            try:
                itchat = _get_itchat()
                itchat.logout()
            except Exception as exc:
                logger.warning(f"登出异常: {exc}")
            finally:
                self._logged_in = False
                self._nickname = None

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict, is_group: bool = False):
        """处理收到的微信消息"""
        sender_name = msg.get("ActualNickName") or msg.get("User", {}).get("NickName", "未知")
        sender_id = msg.get("FromUserName", "unknown")
        content = msg.get("Text", "")

        if not content or not content.strip():
            return

        # 群聊消息去掉 @机器人 前缀
        if is_group and content.startswith("@"):
            parts = content.split("\u2005", 1)  # \u2005 是微信 @ 后的特殊空格
            content = parts[1] if len(parts) > 1 else content

        logger.info(f"收到微信消息 from {sender_name}: {content[:50]}...")

        # 异步调用 Gateway
        loop = asyncio.new_event_loop()
        try:
            reply = loop.run_until_complete(
                self._call_gateway(sender_id, sender_name, content)
            )
        finally:
            loop.close()

        if reply:
            logger.info(f"回复 {sender_name}: {reply[:50]}...")
            try:
                msg.user.send(reply)
                logger.info("微信消息发送成功")
            except Exception as exc:
                logger.error(f"发送微信消息失败: {exc}")

    async def _call_gateway(
        self, user_id: str, user_name: str, message: str
    ) -> Optional[str]:
        """调用 PyClaw Gateway API"""
        url = f"{self.gateway_url}/v1/chat/completions"

        headers = {"Content-Type": "application/json"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"

        # 会话管理
        if user_id not in self._sessions:
            self._sessions[user_id] = []

        history = self._sessions[user_id]
        history.append({"role": "user", "content": message})

        # 限制历史长度
        if len(history) > 20:
            history = history[-20:]
            self._sessions[user_id] = history

        self._save_sessions()

        payload = {
            "model": "default",
            "messages": history,
            "stream": False,
            "user": f"wechat_{user_id}",
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0)
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()

                reply = (
                    result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )

                if reply:
                    history.append({"role": "assistant", "content": reply})
                    self._save_sessions()

                return reply

        except httpx.TimeoutException:
            logger.error("调用 Gateway 超时")
            return "⏳ 处理超时，请稍后重试"
        except httpx.HTTPStatusError as exc:
            logger.error(f"Gateway 错误: {exc.response.status_code}")
            return f"⚠️ 服务暂时不可用 (HTTP {exc.response.status_code})"
        except Exception as exc:
            logger.error(f"调用 Gateway 失败: {exc}")
            return "❌ 连接服务失败，请稍后重试"

    # ------------------------------------------------------------------
    # 会话持久化
    # ------------------------------------------------------------------

    def _load_sessions(self) -> dict[str, list]:
        if _SESSIONS_FILE.exists():
            try:
                return json.loads(_SESSIONS_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_sessions(self):
        _ensure_dirs()
        try:
            _SESSIONS_FILE.write_text(json.dumps(self._sessions, ensure_ascii=False))
        except Exception as exc:
            logger.warning(f"保存会话失败: {exc}")

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """获取当前状态（包含 QR 码数据供前端刷新）"""
        return {
            "logged_in": self._logged_in,
            "nickname": self._nickname,
            "has_qrcode": self._qr_base64 is not None,
            "qrcode": self._qr_base64,  # 前端可检测到 QR 变化并刷新
            "session_count": len(self._sessions),
        }

    def get_sessions_summary(self) -> list[dict]:
        """获取会话摘要列表"""
        result = []
        for user_id, messages in self._sessions.items():
            last_msg = messages[-1] if messages else {}
            result.append({
                "user_id": user_id,
                "message_count": len(messages),
                "last_message": last_msg.get("content", "")[:50],
                "last_role": last_msg.get("role", ""),
            })
        return result
