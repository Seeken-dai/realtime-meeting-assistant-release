"""
讯飞开放平台 RTASR（实时语音转写）适配器。

鉴权：WebSocket 握手需 HMAC-SHA1 签名（下方 _build_url 实现）。
角色分离：握手参数 roleType=2 开启「盲分」（说话人不固定、无需预注册声纹，
          正是线下会议场景）；结果中每个词带 rl 字段表示角色编号。

⚠️ 需 POC 实测确认：
   - 盲分在真实多人会议下的角色稳定性（同一人是否频繁跳号）；
   - lang 参数（cn / 中英混合）对识别率的影响。
   官方文档：https://www.xfyun.cn/doc/asr/rtasr/API.html

音频要求：16kHz / 16bit / 单声道 PCM；每 40ms 发送 1280 字节。
"""

import base64
import hashlib
import hmac
import json
import ssl
import threading
import time
from urllib.parse import quote

import websocket  # websocket-client

from base import ASRBase

_HOST = "rtasr.xfyun.cn"
_BASE = f"wss://{_HOST}/v1/ws"
_MAX_RECONNECT = 8      # 指数退避后累计约 2 分钟，超过则判定为长时间断网

# 常见错误码的排查提示（官方：10105 = illegal access）
_ERROR_HINTS = {
    "10105": (
        "鉴权失败，按以下顺序排查：\n"
        "   ① 控制台 → 实时语音转写 → 我的应用，确认该 APPID "
        "【已开通「实时语音转写」服务】（这是最常见原因）；\n"
        "   ② APIKey 必须取自「实时语音转写」这个服务下，"
        "不能用账号级 Key 或语音听写等其它服务的 Key；\n"
        "   ③ 检查控制台的【IP 白名单】——若已配置，需加入你当前公网 IP，"
        "或直接关闭白名单；\n"
        "   ④ 确认 config.py 里的 APPID / APIKey 没有多余空格或换行。"
    ),
    "10106": "参数错误，检查 URL 参数拼接（signa 是否已 urlencode）。",
    "10700": "引擎错误，通常为服务端临时问题，稍后重试。",
    "11200": "该 APPID 没有权限调用此服务，或免费额度已用完。",
    "11201": "APPID today 调用次数超限（免费额度用尽）。",
}


class XfyunASR(ASRBase):
    name = "讯飞 RTASR"

    def __init__(self, app_id, api_key, role_separation=True, lang="cn", debug=False):
        # strip 防止复制粘贴带入空格/换行导致签名失败
        self.app_id = (app_id or "").strip()
        self.api_key = (api_key or "").strip()
        self.role_separation = role_separation
        self.lang = lang
        self.debug = debug
        self._ws = None
        self._on_result = None
        self._recv_thread = None
        self._running = False
        self._last_speaker = None   # rl=0 表示角色未切换，需沿用上一个
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._on_state = None       # 连接状态回调，供上层展示提示

    def _build_url(self):
        ts = str(int(time.time()))
        base_string = (self.app_id + ts).encode("utf-8")
        md5 = hashlib.md5(base_string).hexdigest()
        signa = base64.b64encode(
            hmac.new(self.api_key.encode("utf-8"), md5.encode("utf-8"),
                     hashlib.sha1).digest()
        ).decode("utf-8")
        # ⚠️ signa 是 base64，含 + / = 等字符，必须 urlencode，
        #    否则 '+' 会被解析成空格导致鉴权失败（错误码 10105）
        url = f"{_BASE}?appid={quote(self.app_id)}&ts={ts}&signa={quote(signa)}"
        if self.role_separation:
            url += "&roleType=2"   # 2 = 盲分（角色分离）
        url += f"&lang={self.lang}"
        return url

    # ── 以下两个 hook 供子类（如大模型版）覆写，其余机制全部复用 ──
    def _handshake_ok(self, handshake):
        """握手响应是否表示连接成功。标准版为 action==started。"""
        return handshake.get("action") == "started"

    def _dispatch_message(self, msg):
        """处理一条服务端消息。返回断线原因字符串表示需重连，None 表示正常。

        标准版：action=result→解析；action=error→重连；其余忽略。
        子类若消息封装不同，覆写此方法即可复用 recv/重连/断线机制。
        """
        if msg.get("action") == "error":
            print(f"[讯飞] 错误: {msg}")
            return f"服务端错误 {msg.get('code')}：{msg.get('desc', '')}"
        if msg.get("action") == "result":
            self._parse_result(msg.get("data", ""))
        return None

    def start(self, on_result):
        self._on_result = on_result
        self._ws = websocket.create_connection(
            self._build_url(), sslopt={"cert_reqs": ssl.CERT_NONE}
        )
        # 握手响应
        handshake = json.loads(self._ws.recv())
        if not self._handshake_ok(handshake):
            code = handshake.get("code")
            hint = _ERROR_HINTS.get(str(code), "")
            raise RuntimeError(
                f"{self.name} 握手失败: {handshake}"
                + (f"\n\n👉 排查建议：{hint}" if hint else "")
            )
        print(f"[{self.name}] 连接已建立")
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        while self._running:
            try:
                raw = self._ws.recv()
            except Exception as exc:
                if self._running:
                    self._handle_disconnect(f"连接中断：{str(exc)[:60]}")
                return
            if not raw:
                continue
            reason = self._dispatch_message(json.loads(raw))
            if reason is not None:
                if self._running:
                    self._handle_disconnect(reason)
                return

    def _handle_disconnect(self, reason):
        """转写连接断开时自动重连（PRD TRS-6）。

        ⚠️ 录音绝不能因此中断 —— 上层的采集循环独立运行，
        这里只负责把转写通道拉回来。重连期间 send() 静默丢弃音频，
        断档部分转写会缺失，但录音文件完整，会后仍可补听。
        """
        with self._reconnect_lock:
            if self._reconnecting or not self._running:
                return
            self._reconnecting = True
        self._notify_state("disconnected", reason)
        threading.Thread(target=self._reconnect_loop, args=(reason,),
                         daemon=True).start()

    def _reconnect_loop(self, reason):
        delay = 1.0
        attempt = 0
        while self._running and attempt < _MAX_RECONNECT:
            attempt += 1
            time.sleep(delay)
            delay = min(delay * 2, 15)      # 指数退避，避免雪崩
            if not self._running:
                break
            try:
                ws = websocket.create_connection(
                    self._build_url(), sslopt={"cert_reqs": ssl.CERT_NONE},
                    timeout=10)
                handshake = json.loads(ws.recv())
                if not self._handshake_ok(handshake):
                    ws.close()
                    raise RuntimeError(str(handshake))
                with self._reconnect_lock:
                    self._ws = ws
                    self._reconnecting = False
                self._notify_state("reconnected", f"第 {attempt} 次重连成功")
                self._recv_thread = threading.Thread(
                    target=self._recv_loop, daemon=True)
                self._recv_thread.start()
                return
            except Exception as exc:
                self._notify_state(
                    "reconnecting",
                    f"第 {attempt} 次重连失败（{str(exc)[:40]}），{delay:.0f}s 后重试")
        with self._reconnect_lock:
            self._reconnecting = False
        self._notify_state("failed", f"重连 {attempt} 次仍失败，转写已停止（录音继续）")

    def _notify_state(self, state, message):
        print(f"[讯飞] {state}: {message}")
        if self._on_state:
            try:
                self._on_state(state, message)
            except Exception:
                pass

    @staticmethod
    def _is_final_type(type_val):
        """type: 0=最终结果, 1=中间结果。

        标准版文档示例是字符串 "0"/"1"；大模型版线上常返回数字 0/1。
        若只写 `== "0"`，大模型版 is_final 恒为 False → _last_speaker 永不更新
        → 后续只有 rl=0 的词会落到 speaker=None（全部「未知说话人」）。
        """
        return type_val in (0, "0") or str(type_val).strip() == "0"

    @staticmethod
    def _normalize_rl(rl):
        """角色编号：非 0 表示切换到该说话人；0/空表示沿用上一个。"""
        if rl is None or rl == "":
            return None
        # 兼容 "1" / 1 / 1.0
        try:
            n = int(float(rl))
        except (TypeError, ValueError):
            s = str(rl).strip()
            return s if s and s != "0" else None
        if n == 0:
            return None
        return str(n)

    def _parse_result(self, data_str):
        if not data_str:
            return
        if isinstance(data_str, dict):
            data = data_str
        else:
            data = json.loads(data_str)
        if self.debug:
            print(f"\n\033[90m[原始返回] {json.dumps(data, ensure_ascii=False)}\033[0m")
        # 兼容 data 直接是 cn 包，或再包一层
        cn = data.get("cn") if isinstance(data, dict) else None
        if not cn and isinstance(data, dict) and "st" in data:
            st = data.get("st") or {}
        else:
            st = (cn or {}).get("st") or {}
        is_final = self._is_final_type(st.get("type"))
        end_ms = int(st.get("ed") or 0)

        # 展平为 (词, 角色) 序列。每个 ws 里 cw[0] 是最优候选。
        # rl 优先取词级；部分返回会把角色挂在 ws 上。
        tokens = []
        for rt in st.get("rt", []) or []:
            for ws_ in rt.get("ws", []) or []:
                cw_list = ws_.get("cw") or []
                if not cw_list:
                    continue
                cw = cw_list[0]
                rl = cw.get("rl")
                if rl in (None, "", 0, "0"):
                    rl = ws_.get("rl")
                tokens.append((cw.get("w", ""), rl))

        # ⚠️ 关键：一个分段内部可能发生角色切换（rl 由 0 变为新编号），
        #    必须按切换点把分段拆成多段，否则切换前的字会被错误归给新说话人。
        #    官方：rl 从 1 开始计数，rl=0 表示"沿用上一个角色"。
        runs = []            # [(speaker, text), ...]
        cur_speaker = self._last_speaker
        cur_text = []
        saw_explicit_role = False
        for word, rl in tokens:
            new_speaker = self._normalize_rl(rl)
            if new_speaker is not None:
                saw_explicit_role = True
                if new_speaker != cur_speaker and cur_text:
                    runs.append((cur_speaker, "".join(cur_text)))
                    cur_text = []
                cur_speaker = new_speaker
            cur_text.append(word)
        if cur_text:
            runs.append((cur_speaker, "".join(cur_text)))

        # 最终结果推进说话人；中间结果仅在「尚无归属」或「本帧明确给出新 rl」时推进，
        # 避免 type 字段类型不一致导致跨句说话人全部丢失。
        if cur_speaker is not None:
            if is_final or self._last_speaker is None or saw_explicit_role:
                self._last_speaker = cur_speaker

        for speaker, text in runs:
            text = text.strip()
            if text:
                self._on_result(text=text, speaker=speaker,
                                is_final=is_final, end_ms=end_ms)

    def set_state_callback(self, callback):
        """注册连接状态回调，用于向用户展示"转写已断开/重连中"。"""
        self._on_state = callback

    def send(self, pcm_bytes):
        # 重连期间静默丢弃：录音由上层独立保存，不受影响
        with self._reconnect_lock:
            if self._reconnecting or self._ws is None:
                return
            ws = self._ws
        try:
            ws.send(pcm_bytes, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as exc:
            if self._running:
                self._handle_disconnect(f"发送失败：{str(exc)[:60]}")

    def stop(self):
        self._running = False
        try:
            # 发送结束标志
            self._ws.send(json.dumps({"end": True}))
            time.sleep(0.5)
        except Exception:
            pass
        finally:
            if self._ws is not None:
                self._ws.close()
