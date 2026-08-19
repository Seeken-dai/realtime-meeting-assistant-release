"""
讯飞「实时语音转写大模型」适配器（基于星火大模型，非标准版 RTASR）。

与标准版 RTASR 的关系：
  - 返回结构兼容（cn.st.rt[].ws[].cw[].w / .rl），转写解析、角色分离、
    断网重连、send 复用父类 XfyunASR。
  - 【鉴权与端点不同】：
      端点  wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1
      凭证  appId + accessKeyId + accessKeySecret
            （appId 是开放平台应用 ID，与 accessKeyId 不是同一个）
      签名  除 signature 外所有参数按名升序，键值分别 URL 编码后拼接，
            HmacSHA1(accessKeySecret) → Base64
  官方文档：https://www.xfyun.cn/doc/spark/asr_llm/rtasr_llm.html
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from asr_xfyun import XfyunASR

_HOST = "office-api-ast-dx.iflyaisol.com"
_PATH = "/ast/communicate/v1"
_BASE = f"wss://{_HOST}{_PATH}"

# 中国时区：文档示例 utc 使用 +0800，错误码 35013=时区格式错误
_TZ_CN = timezone(timedelta(hours=8))


def _url_encode(value: str) -> str:
    """文档要求键和值都做 urlencode；空格用 %20 而非 +。"""
    return quote(str(value), safe="")


class XfyunLlmASR(XfyunASR):
    name = "讯飞实时语音转写大模型"

    def __init__(
        self,
        access_key_id,
        access_key_secret,
        app_id="",
        role_separation=True,
        lang="autodialect",
        debug=False,
    ):
        # 复用父类状态（重连锁、last_speaker 等）；标准版 app_id/api_key 不用
        super().__init__(
            app_id="",
            api_key="",
            role_separation=role_separation,
            lang=lang,
            debug=debug,
        )
        self.llm_app_id = (app_id or "").strip()
        self.access_key_id = (access_key_id or "").strip()
        self.access_key_secret = (access_key_secret or "").strip()
        self._session_id: str | None = None

    def _utc_now(self) -> str:
        # 示例：2025-09-04T15:38:07+0800（注意无冒号的时区）
        return datetime.now(_TZ_CN).strftime("%Y-%m-%dT%H:%M:%S%z")

    def _business_params(self) -> dict:
        """参与签名的业务参数（不含 signature）。"""
        if not self.llm_app_id:
            raise RuntimeError(
                f"{self.name} 缺少 appId。请在设置页填写「应用 App ID」"
                "（开放平台应用 ID，与 Access Key ID 不同），"
                "或配置 XFYUN_APP_ID / XFYUN_LLM_ASR_APP_ID。"
            )
        if not self.access_key_id or not self.access_key_secret:
            raise RuntimeError(
                f"{self.name} 缺少 accessKeyId / accessKeySecret。"
                "请在控制台「实时语音转写大模型」服务页获取。"
            )
        params = {
            "accessKeyId": self.access_key_id,
            "appId": self.llm_app_id,
            "uuid": uuid.uuid4().hex,
            "utc": self._utc_now(),
            "audio_encode": "pcm_s16le",
            "lang": self.lang or "autodialect",
            "samplerate": "16000",
        }
        if self.role_separation:
            params["role_type"] = "2"  # 实时角色分离（盲分）
        return params

    def _sign_params(self) -> dict:
        """
        signature 生成（官方）：
        1. 除 signature 外参数按参数名升序
        2. 键、值分别 URL 编码后按 key=value& 拼接
        3. HmacSHA1(accessKeySecret) → Base64
        """
        params = self._business_params()
        base_string = "&".join(
            f"{_url_encode(k)}={_url_encode(params[k])}"
            for k in sorted(params)
        )
        signature = base64.b64encode(
            hmac.new(
                self.access_key_secret.encode("utf-8"),
                base_string.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")
        params["signature"] = signature
        if self.debug:
            print(f"[{self.name}] baseString={base_string}")
            print(f"[{self.name}] signature={signature}")
        return params

    def _build_url(self) -> str:
        # 查询串同样需要对键值编码（urlencode 默认 quote_via 会把空格变 +，用 quote）
        return f"{_BASE}?{urlencode(self._sign_params(), quote_via=quote)}"

    def _handshake_ok(self, handshake) -> bool:
        # 文档：action=started 表示握手；code 为 0 / "0" 也可
        if handshake.get("action") == "started":
            sid = handshake.get("sid") or handshake.get("sessionId")
            if sid:
                self._session_id = str(sid)
            return True
        if handshake.get("action") == "error":
            return False
        code = handshake.get("code")
        if code in (0, "0", None) and "error" not in handshake:
            sid = handshake.get("sid") or handshake.get("sessionId")
            if sid:
                self._session_id = str(sid)
            return True
        return False

    def _dispatch_message(self, msg):
        """兼容 action / msg_type 两种封装。

        文档 2.3 表：action + data(string)；
        示例 JSON：msg_type/res_type + data(object)。
        两种都会遇到。
        """
        if not isinstance(msg, dict):
            return None
        action = msg.get("action") or msg.get("msg_type")
        res_type = msg.get("res_type")
        if action == "error" or res_type == "frc":
            print(f"[讯飞大模型] 错误: {msg}")
            code = msg.get("code") or ""
            desc = msg.get("desc") or msg.get("data", {})
            if isinstance(desc, dict):
                desc = desc.get("desc") or desc
            return f"服务端错误 {code}：{desc}"
        if action in ("result", "asr") or res_type == "asr":
            data = msg.get("data", "")
            # data 可能是对象、JSON 字符串，或已是 cn/st 结构
            if isinstance(data, str) and data.strip():
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    pass
            if isinstance(data, dict):
                # 若整包就是识别结果（无 cn 但有 st），直接交给解析
                self._parse_result(data)
            elif data:
                self._parse_result(data)
            if self.debug and isinstance(data, dict):
                st = (data.get("cn") or {}).get("st") or data.get("st") or {}
                rls = []
                for rt in st.get("rt") or []:
                    for ws_ in rt.get("ws") or []:
                        for cw in ws_.get("cw") or []:
                            if "rl" in cw:
                                rls.append(cw.get("rl"))
                if self.role_separation and not any(
                    self._normalize_rl(r) for r in rls
                ):
                    # 只提示一次，避免刷屏
                    if not getattr(self, "_warned_no_rl", False):
                        self._warned_no_rl = True
                        print(
                            f"[{self.name}] 已开 role_type=2 但结果中未见有效 rl。"
                            "请确认控制台已开通角色分离；若仅一人说话属正常。"
                        )
        return None

    def stop(self):
        # 文档要求 end 时带 sessionId（有则带，无则仍发 end）
        self._running = False
        try:
            if self._ws:
                payload = {"end": True}
                if self._session_id:
                    payload["sessionId"] = self._session_id
                self._ws.send(json.dumps(payload))
                self._ws.close()
        except Exception:
            pass
        self._ws = None
