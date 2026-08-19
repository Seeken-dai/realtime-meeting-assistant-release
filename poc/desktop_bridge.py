"""Electron 与 Python POC 之间的 JSON Lines 桥接层。

stdout 只输出协议事件；服务诊断信息转发到 stderr，避免污染协议。
该文件只定义并串联现有能力，不代表真实音频链路已经验证。
"""

import argparse
import contextlib
import json
import math
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import sounddevice as sd

import providers
from mic_stream import MicStream
from audio_clock import RecordingSampleClock
from audio_recorder import AudioRecorder
try:
    from online_audio_stream import (
        MicrophoneStreamError,
        OnlineMeetingStream,
        SystemAudioStreamError,
        default_loopback_info,
    )
except ImportError:
    OnlineMeetingStream = None  # type: ignore
    MicrophoneStreamError = None  # type: ignore
    SystemAudioStreamError = None  # type: ignore
    default_loopback_info = None  # type: ignore

try:
    from wav_stream import WavStream
except ImportError:                      # 回放是验证工具，缺了不影响开会
    WavStream = None  # type: ignore

try:
    from speaker_me import (
        MeIdentifier,
        SPEAKER_ID_ME,
        SPEAKER_ID_OTHER,
    )
except ImportError:
    MeIdentifier = None  # type: ignore
    SPEAKER_ID_ME = "me"
    SPEAKER_ID_OTHER = "other"

try:
    from turn_split import Span, clip_spans, split_text_by_spans
except ImportError:                      # 没有声纹依赖时不影响纯转写
    Span = None  # type: ignore
    clip_spans = None  # type: ignore
    split_text_by_spans = None  # type: ignore

try:
    import config
except ImportError:
    sys.exit("未找到 config.py")

try:
    from suggest import normalize_scene
except ImportError:
    def normalize_scene(scene=None):
        value = str(scene or "general")
        return value if value in ("general", "sales", "requirements") else "general"


def _char_times_from_words(words, text):
    """把 ASR 的词级时间戳摊成"每个字的时刻"（秒）。

    阿里 paraformer 的 sentence 里带 words[{text,begin_time,end_time}]。
    有它切点就准；字数对不上（标点、英文分词，或文本被"前导标点补回上一段"
    改写过）时宁可返回 None 走时长比例 —— 错位的字符时间比没有时间更糟，
    它会把切点稳定地放在错误的位置上，而且看不出来是错的。
    """
    if not words:
        return None
    times = []
    for word in words:
        if not isinstance(word, dict):
            return None
        piece = str(word.get("text") or word.get("word") or "")
        end = word.get("end_time", word.get("end"))
        if end is None:
            return None
        punctuation = str(word.get("punctuation") or "")
        for _ in range(len(piece) + len(punctuation)):
            times.append(float(end) / 1000.0)
    if len(times) != len(text):
        return None
    return times


# Electron 侧固定按 UTF-8 解析 JSON Lines。Windows 控制台默认 GBK，
# 设备名含 ® 等字符时会直接编码失败，因此协议层必须显式统一编码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

PROTOCOL_OUT = sys.stdout


def _finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _valid_audio_range(start, end):
    return (
        _finite_number(start)
        and _finite_number(end)
        and float(start) >= 0
        and float(end) > float(start)
    )

# 送给模型的上下文窗口。
#
# ⚠️ 按【字数】算，不按条数算。原来是"最近 10 条"，而 2026-07-27 起一条
#    过长的 final 会被按说话人切成好几条（见 turn_split.py），
#    同样 10 条覆盖的对话直接缩水一半 —— 切分是为了改派方便，
#    不该顺带把模型能看到的上下文砍掉。条数上限只作兜底防爆。
MAX_CONTEXT_CHARS = 1200
MAX_CONTEXT_ITEMS = 40

# 自动建议的触发闸门。
#
# ⚠️ 真机验证结论：只靠 debounce 会让建议刷屏（实测一场会 360 条）。
#    讯飞的 final 分段很碎，一句话常被切成几段，段间 2 秒停顿遍地都是，
#    于是对方连续讲话时每 2-3 秒就打一次 LLM。因此叠加三道闸门：
#      1) DEBOUNCE_SEC     —— 对方停下后再等这么久，确认这一轮真的说完了
#      2) MIN_INTERVAL_SEC —— 两批自动建议之间的冷却期
#      3) MIN_NEW_CHARS    —— 自上批以来对方新说的字数太少 = 没有新信息
#    手动触发（用户点「现在给建议」）绕过 2) 和 3)：那是明确的即时诉求，
#    不该被为"防刷屏"设计的闸门挡住。
#
# ⚠️ DEBOUNCE 从 5s 降到 3s（2026-07-27）：那个 5s 是为**讯飞**定的 ——
#    它的 final 很碎，一句话切成好几段，段间到处是 2 秒停顿，不等久点就重复触发。
#    阿里的 final 本来就在静音处断句、整段给出，这 5 秒对当前底座是纯等待。
#    而且 debounce 是从 **final 送达**开始算的，final 本身还滞后于说话结束
#    （同一场实测送达偏移中位 3.69s，含开录时刻差），实际静默远长于设定值。
#    防刷屏真正的兜底是 2)：20s 冷却把频率钉死在每分钟 ≤3 批，与 debounce 无关。
#    设置页「对方停顿多久后给建议」可覆盖此默认值。
DEBOUNCE_SEC = 3.0
MIN_INTERVAL_SEC = 20.0
MIN_NEW_CHARS = 40

# --wav-in 回放放完后的收尾等待（真实会议里这段由"用户点结束"覆盖）。
# 下限要盖住「最后一句 final 的送达延迟 + debounce」，否则最后一轮建议会被切掉。
REPLAY_DRAIN_MIN_SEC = 8.0
REPLAY_DRAIN_MAX_SEC = 40.0

# 音量电平上报节流。
# ⚠️ 每帧上报（FRAME_MS=40 → 25 次/秒）会让渲染进程每秒重渲染 25 次整棵组件树，
#    真机验证中表现为"会中打字很卡"。电平只是给人看的示意，降到 ~8 次/秒足够。
AUDIO_LEVEL_MIN_INTERVAL = 0.12
AUDIO_LEVEL_MIN_DELTA = 0.04


def emit(event_type, **payload):
    print(
        json.dumps({"type": event_type, **payload}, ensure_ascii=False),
        file=PROTOCOL_OUT,
        flush=True,
    )


class LevelThrottle:
    """按时间间隔 + 变化幅度双条件放行音量电平。

    静音时电平恒为 0，靠幅度条件天然静默；说话时靠时间间隔封顶。
    强制放行归零值，否则暂停/结束后指示条会停在最后一个非零值上。
    """

    def __init__(self):
        self._last_at = 0.0
        self._last_value = None

    def should_emit(self, value):
        now = time.time()
        just_went_silent = value == 0 and self._last_value != 0
        if self._last_value is None or just_went_silent:
            self._last_at, self._last_value = now, value
            return True
        if now - self._last_at < AUDIO_LEVEL_MIN_INTERVAL:
            return False
        if abs(value - self._last_value) < AUDIO_LEVEL_MIN_DELTA:
            return False
        self._last_at, self._last_value = now, value
        return True


def _public_suggestion(item):
    """把引擎的内部标记翻译成协议字段。

    ⚠️ 真机验证暴露的字段断层：引擎里分级字段叫 type、降级理由挂在下划线
    私有键上，而桌面端读的是 level / notice / sensitive。名字对不上时
    _validate() 的降级结论全部丢失，前端拿到 undefined 就一律按"有依据"
    显示 —— 这比标错更危险，因为它把"无依据"伪装成了"有依据"。
    """
    level = item.get("type") or "clarify"
    return {
        "intent": item.get("intent", ""),
        "script": item.get("script", ""),
        "references": item.get("references") or [],
        "evidence": item.get("evidence") or [],
        "level": level,
        "grounded": level == "grounded",
        # 被程序化校验改判的理由，UI 要原样展示给用户看
        "notice": item.get("_downgraded") or item.get("_reclassified") or "",
        # 命中内部资料/承诺性表述的提醒（标注而非拦截，见 suggest.py）
        "sensitive": item.get("_sensitive") or "",
        "category": item.get("category") or "",
    }


def _local_memory_candidates(context):
    """从明确语句提取候选记忆；候选必须由用户确认后才成为正式结论。"""
    out = []
    seen = set()
    # 实时链路不应因为 ASR 选择了 zh_en 就失去记忆候选。
    # 中英混会里常见的 decision / action item 也要能被本地兜底规则捕获，
    # 避免 LLM 没有返回结构化候选时整条信息消失。
    decision = re.compile(
        r"确定|决定|确认|同意|采用|结论|最终方案|定为|敲定|"
        r"\b(?:decision|decided|agreed|confirmed?|approved?|"
        r"finali[sz](?:e|ed)|go with|proceed with)\b",
        re.IGNORECASE,
    )
    action = re.compile(
        r"待办|需要|负责|跟进|补充|提交|整理|安排|完成|截止|下周|本周|明天|后续|发我|给我|"
        r"\b(?:action\s*item|to[- ]?do|follow[- ]?up|owner|deadline|due\s+date)\b|"
        r"\b(?:please|need(?:s)?\s+to|should|must|assigned\s+to|will)\s+"
        r"(?:send|submit|prepare|follow|confirm|finish|complete|review|update|provide|share|deliver|schedule)\b",
        re.IGNORECASE,
    )
    for index, item in enumerate(context or []):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        for sentence in re.split(r"[。！？!?；;\n]+", text):
            sentence = sentence.strip()[:500]
            kinds = []
            if decision.search(sentence):
                kinds.append("decision")
            if action.search(sentence):
                kinds.append("action_item")
            if not kinds:
                continue
            for kind in kinds:
                key = f"{kind}:{sentence.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                owner = None
                owner_match = re.search(
                    r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fff·]{0,10})(?:负责|来跟进|跟进)",
                    sentence,
                )
                if not owner_match:
                    owner_match = re.search(
                        r"\b(?:assigned\s+to|owner)\s*[:：]?\s*([A-Za-z][A-Za-z .'-]{0,40})",
                        sentence,
                        re.IGNORECASE,
                    )
                if owner_match:
                    owner = next(
                        (
                            group.strip(" .,:;")
                            for group in owner_match.groups()
                            if group and group.strip(" .,:;")
                        ),
                        None,
                    )
                due_match = re.search(
                    r"今天|明天|本周|下周|月底|\d{1,2}[月/-]\d{1,2}[日号]?|"
                    r"\b(?:today|tomorrow|this\s+week|next\s+week|"
                    r"by\s+(?:eod|end\s+of\s+day|monday|tuesday|"
                    r"wednesday|thursday|friday|saturday|sunday))\b",
                    sentence,
                    re.IGNORECASE,
                )
                out.append({
                    "id": f"memory-{abs(hash(key)) % 10**12:x}",
                    "kind": kind,
                    "status": "candidate",
                    "content": sentence,
                    "owner": owner,
                    "dueAt": due_match.group(0) if due_match else None,
                    "evidenceTranscriptId": item.get("id"),
                    "evidenceText": sentence,
                    "source": "rule",
                })
    return out[:20]


def list_devices():
    default_index = sd.default.device[0]
    host_apis = sd.query_hostapis()
    devices = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        host_api = host_apis[dev["hostapi"]]["name"]
        # Windows 会通过 MME / DirectSound / WASAPI / WDM-KS 把同一硬件
        # 暴露多次。当前 PortAudio 版本能枚举 WASAPI 端点但无法稳定打开，
        # 而 MME 是实际验证可用的捕获层；排除系统映射器后，MME 恰好对应
        # Windows 中启用的用户级麦克风，不会混入 WDM-KS 驱动节点。
        if host_api != "MME" or dev["name"].startswith("Microsoft 声音映射器"):
            continue
        devices.append(
            {
                "index": index,
                "name": dev["name"],
                "channels": int(dev["max_input_channels"]),
                "sampleRate": int(dev["default_samplerate"]),
                "isDefault": index == default_index,
                "hostApi": host_api,
            }
        )
    emit("devices", devices=devices)


def test_device(device_index, duration):
    """使用与正式链路相同的 16kHz 单声道参数做短时拾音测试。"""
    emit("device_test_status", status="starting", device=device_index)
    started = time.time()
    try:
        with MicStream(
            sample_rate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_MS,
            device=device_index,
        ) as mic:
            emit("device_test_status", status="listening", device=device_index)
            for pcm in mic.frames():
                level = int(np.abs(np.frombuffer(pcm, dtype=np.int16)).mean())
                emit("audio_level", level=min(level / 4000, 1))
                if time.time() - started >= duration:
                    break
        emit("device_test_status", status="completed", device=device_index)
    except Exception as exc:
        emit("error", stage="device_test", message=str(exc))
        raise


class BridgeSession:
    def __init__(self, engine, me_label, me_detector=None, voiceprint_mode=False,
                 debounce_sec=DEBOUNCE_SEC, suggestion_count=3,
                 audio_clock=None, scene="general"):
        self.engine = engine
        self.scene = normalize_scene(scene)
        # 设置页「建议触发」两项：静默阈值与每次条数。
        # ⚠️ 这两个设置在 2026-07-27 之前只存在于界面，从没传到过这里 ——
        #    用户改了没有任何效果。加设置项时务必把这条链路走通：
        #    App.tsx startMeeting → main.cjs args.push → 这里的 argparse。
        self.debounce_sec = max(0.5, min(float(debounce_sec or DEBOUNCE_SEC), 15.0))
        self.suggestion_count = max(1, min(int(suggestion_count or 3), 5))
        # 声纹模式固定 me_label=me；云端角色号模式仍可 set_me
        self.me_label = str(me_label) if me_label else (
            SPEAKER_ID_ME if voiceprint_mode else None
        )
        self.me_detector = me_detector
        self.voiceprint_mode = bool(voiceprint_mode)
        self._announced_cut = False   # 自适应门槛只提示一次
        self.transcript = []
        self._lock = threading.Lock()
        self._pending_timer = None
        self._busy = False
        self._suggestions_paused = False
        self._last_final_fingerprint = None
        self._last_final_at = 0
        self.audio_clock = audio_clock
        self._last_final_audio_end_ms = {}
        # 频率闸门状态：上批自动建议的时刻，以及此后对方新说的字数
        self._last_auto_fire_at = 0.0
        self._new_chars = 0
        # 生成期间的新转写只保留一个“最新上下文待处理”标记；
        # 当前请求结束后最多补一轮，避免持续讲话形成排队刷屏。
        self._latest_context_pending = False
        self._pending_merge_count = 0

    def set_me(self, speaker_id):
        """会中改变"我"是谁。

        ⚠️ 必须支持会中变更：讯飞的角色编号要等真正开口后才出现，
        用户在会前无从得知自己是几号，只能开会后点一下认领。
        这个标记直接决定建议站在谁的立场，以及谁说完话不触发建议。
        """
        with self._lock:
            self.me_label = None if speaker_id is None else str(speaker_id)
            # 立场变了，历史上下文里的"我"标注也要跟着改，
            # 否则送给 LLM 的对话会把我方发言标成对方
            for item in self.transcript:
                item["speaker"] = self.speaker_name(item.get("speakerId"))
        emit("me_changed", speakerId=self.me_label)

    def speaker_name(self, speaker):
        if speaker is None:
            return "未知说话人"
        sid = str(speaker)
        if self.me_label and sid == str(self.me_label):
            return "我"
        if sid == SPEAKER_ID_OTHER:
            return "对方"
        return f"说话人{speaker}"

    def _split_final(self, text, begin_ms, end_ms, words):
        """把一条 final 按声纹时间轴切片。切不开返回 None。

        返回 [(片段文本, speakerId, 片段开始 start_ms, 片段结束 end_ms), ...]。
        """
        text = (text or "").strip()
        if not text or split_text_by_spans is None:
            return None
        segments = self.me_detector.spans_between(begin_ms, end_ms)
        if len(segments) < 2:
            return None
        spans = [
            Span(
                s.start_sec,
                s.end_sec,
                SPEAKER_ID_ME if s.is_me else SPEAKER_ID_OTHER,
            )
            for s in segments
        ]
        # 语音段与本句的时间窗只是重叠、不是包含：两端各裁一刀，
        # 并丢掉只渗进来一点点的碎片（那属于相邻的句子）
        if begin_ms and end_ms:
            spans = clip_spans(
                spans, float(begin_ms) / 1000.0 - 0.3, float(end_ms) / 1000.0 + 0.35
            )
            if len(spans) < 2:
                return None
        char_times = _char_times_from_words(words, text)
        try:
            chunks = split_text_by_spans(text, spans, char_times=char_times)
        except Exception as exc:            # 切分只是锦上添花，坏了不能拖垮转写
            print(f"[分段] 失败，退回整条：{exc}", file=sys.stderr)
            return None
        if len(chunks) < 2:
            return None
        out = []
        for chunk in chunks:
            piece_start = (
                int(chunk.start * 1000)
                if chunk.start is not None
                else int(begin_ms or 0)
            )
            piece_end = (
                int(chunk.end * 1000) if chunk.end is not None else int(end_ms or 0)
            )
            out.append(
                (chunk.text, chunk.label or SPEAKER_ID_OTHER, piece_start, piece_end)
            )
        return out

    def on_transcript(self, text, speaker, is_final, end_ms=0, begin_ms=None,
                      words=None, audio_clock=None):
        # 新会议以实际写入 WAV 的采样数为唯一时间轴。供应商时间只作为
        # “当前 ASR 会话中的位置”，必须先映射，不能直接当播放器时间。
        active_clock = audio_clock or self.audio_clock
        speaker_clock_key = str(speaker) if speaker is not None else "_unknown"
        if active_clock is not None:
            mapped_end = active_clock.map_ms(end_ms) if end_ms else None
            mapped_begin = (
                active_clock.map_ms(begin_ms) if begin_ms is not None else None
            )
            if mapped_end is not None:
                end_ms = mapped_end
                if mapped_begin is None and is_final:
                    mapped_begin = (
                        self._last_final_audio_end_ms.get(speaker_clock_key)
                        if speaker_clock_key in self._last_final_audio_end_ms
                        else 0
                    )
                begin_ms = mapped_begin
                if begin_ms is not None:
                    begin_ms = max(0, min(int(begin_ms), int(mapped_end)))
                end_ms = max(0, int(mapped_end))
                if words:
                    mapped_words = []
                    for word in words:
                        if not isinstance(word, dict):
                            mapped_words = []
                            break
                        mapped = dict(word)
                        valid_word = True
                        for key in ("begin_time", "begin", "end_time", "end"):
                            if key in mapped and mapped[key] is not None:
                                mapped_value = active_clock.map_ms(mapped[key])
                                if mapped_value is None:
                                    valid_word = False
                                    break
                                mapped[key] = mapped_value
                        if not valid_word:
                            mapped_words = []
                            break
                        mapped_words.append(mapped)
                    words = mapped_words or None
            else:
                # 无法证明它落在已写入的 WAV 范围内，就不要伪装成精确时间。
                begin_ms = None
                end_ms = 0
        # A+D1：启用声纹后，用本地判别覆盖/补齐 speakerId（阿里无角色号）
        splittable = False
        if self.voiceprint_mode and self.me_detector and self.me_detector.ready:
            if is_final:
                sid, score = self.me_detector.label_at(end_ms)
                speaker = sid
                splittable = True
                cut = self.me_detector.adaptive_cut
                emit(
                    "voiceprint",
                    speakerId=sid,
                    score=round(score, 3),
                    endMs=end_ms,
                    # None 表示还在用固定阈值（段数不足或无明显断层）
                    adaptiveCut=None if cut is None else round(cut, 3),
                    at=time.time(),
                )
                # 自适应判据是启发式，用户得看见它何时接管、切在哪 —— 只报一次
                if cut is not None and not self._announced_cut:
                    self._announced_cut = True
                    emit("status", stage="voiceprint",
                         message=f"已按本场发言自动校准「我」的判定门槛（{cut:.2f}）")
            else:
                sid, _ = self.me_detector.label_at(end_ms or None)
                speaker = sid
        name = self.speaker_name(speaker)
        text = text.strip()
        if is_final and text:
            # 讯飞会把上一句的句末标点作为下一次 final 的开头返回，例如
            # "？那客户问…"。把前导标点补回上一段，避免新气泡以孤立符号开头。
            match = re.match(r"^([，。！？；：、,.!?;:]+)\s*(.*)$", text)
            if match:
                punctuation, remainder = match.groups()
                with self._lock:
                    if self.transcript:
                        previous = self.transcript[-1]
                        missing = "".join(
                            char
                            for char in punctuation
                            if not previous["text"].endswith(char)
                        )
                        if missing:
                            previous["text"] += missing
                            emit(
                                "transcript_patch_last",
                                append=missing,
                                at=time.time(),
                            )
                text = remainder.strip()
                if not text:
                    return
        if is_final:
            fingerprint = (name, text, int(end_ms or 0))
            now = time.time()
            if (
                fingerprint == self._last_final_fingerprint
                and now - self._last_final_at < 10
            ):
                return
            self._last_final_fingerprint = fingerprint
            self._last_final_at = now
            if end_ms:
                self._last_final_audio_end_ms[speaker_clock_key] = int(end_ms)
        # 一条 final 里可能不止一个人说话（阿里只在静音处断句，实测一条能覆盖
        # 47 秒 / 4 次换人）。按声纹时间轴把它切开，每片各自归属。
        # ⚠️ 必须在上面的"前导标点补回上一段"之后再切：那一步会改写 text，
        #    先切就会拿着旧文本去分字，补回去的标点在片段里又出现一次。
        pieces = (
            self._split_final(text, begin_ms, end_ms, words)
            if (is_final and splittable)
            else None
        )
        # 切开的每一片都当成独立一条发言发出去：渲染层因此拿到可单独改派的段落，
        # 建议闸门也只会按"对方真正说的那部分"计数。
        parts = pieces or [
            (
                text,
                None if speaker is None else str(speaker),
                begin_ms,
                end_ms,
            )
        ]
        now_wall = time.time()
        emitted = []
        for piece_text, piece_speaker, piece_start_ms, piece_end_ms in parts:
            piece_text = (piece_text or "").strip()
            if not piece_text:
                continue
            piece_name = self.speaker_name(piece_speaker)
            # 片段的 at 按它在音频里的结束时刻回推，界面时间戳才对得上
            piece_at = now_wall
            if is_final and end_ms and piece_end_ms:
                piece_at = now_wall - max(0.0, (end_ms - piece_end_ms) / 1000.0)
            emit(
                "transcript",
                id=f"live-{len(self.transcript) + len(emitted) + 1}",
                text=piece_text,
                speaker=piece_name,
                speakerId=piece_speaker,
                isFinal=bool(is_final),
                audioStartMs=piece_start_ms,
                audioEndMs=piece_end_ms,
                endMs=piece_end_ms,
                at=piece_at,
            )
            emitted.append(
                (
                    piece_text,
                    piece_speaker,
                    piece_name,
                    piece_at,
                    piece_start_ms,
                    piece_end_ms,
                )
            )
        if not is_final:
            return
        with self._lock:
            new_chars = 0
            for (
                piece_text,
                piece_speaker,
                piece_name,
                piece_at,
                piece_start_ms,
                piece_end_ms,
            ) in emitted:
                # 保留原始 speakerId：改变"我"之后要据此重算历史归属
                self.transcript.append({
                    "id": f"live-{len(self.transcript) + 1}",
                    "speaker": piece_name,
                    "text": piece_text,
                    "speakerId": piece_speaker,
                    # 建议上下文除了正文，还要保留墙上时间和录音轴，
                    # 这样桌面端可以把一批建议准确定位回左侧转写。
                    "at": piece_at,
                    "audioStartMs": piece_start_ms,
                    "audioEndMs": piece_end_ms,
                })
                if piece_name != "我":
                    # 只统计对方的发言量：闸门问的是"对方有没有说出新东西"
                    new_chars += len(piece_text)
            if self._suggestions_paused or new_chars == 0:
                return
            self._new_chars += new_chars
            if self._busy:
                self._latest_context_pending = True
                self._pending_merge_count += 1
                return
            self._arm_timer(self.debounce_sec)

    @property
    def busy(self):
        """正在生成建议。回放收尾时用来判断"还有没有事情没做完"。"""
        with self._lock:
            return self._busy

    @property
    def pending(self):
        """有等待触发的建议定时器。"""
        with self._lock:
            return self._pending_timer is not None

    def _recent_context(self):
        """（调用方须持有 _lock）最近的对话，按字数截断。

        从后往前收，收满 MAX_CONTEXT_CHARS 为止 —— 保证不管转写被切成
        多少条，模型看到的对话长度是稳定的。
        """
        picked = []
        total = 0
        for item in reversed(self.transcript):
            text = item.get("text") or ""
            if picked and total + len(text) > MAX_CONTEXT_CHARS:
                break
            picked.append(item)
            total += len(text)
            if len(picked) >= MAX_CONTEXT_ITEMS:
                break
        return list(reversed(picked))

    @staticmethod
    def _context_range(context):
        """从实际送给模型的上下文生成可回看的时间范围。

        新会议的录音轴由 RecordingSampleClock 提供；若上下文中任何一段缺少
        录音时间，就不拼出一个看似精确的半截范围，只保留墙上时间。
        """
        if not context:
            return None
        wall_times = [
            float(item.get("at"))
            for item in context
            if isinstance(item, dict)
            and item.get("at") is not None
            and _finite_number(item.get("at"))
        ]
        audio_ranges = [
            (float(item.get("audioStartMs")), float(item.get("audioEndMs")))
            for item in context
            if isinstance(item, dict)
            and _valid_audio_range(item.get("audioStartMs"), item.get("audioEndMs"))
        ]
        result = {
            "wallStartAt": round(min(wall_times) * 1000) if wall_times else None,
            "wallEndAt": round(max(wall_times) * 1000) if wall_times else None,
            "audioStartMs": None,
            "audioEndMs": None,
            "approximate": False,
        }
        if len(audio_ranges) == len(context) and audio_ranges:
            result["audioStartMs"] = min(item[0] for item in audio_ranges)
            result["audioEndMs"] = max(item[1] for item in audio_ranges)
        return result

    def _arm_timer(self, delay):
        """（调用方须持有 _lock）重置待触发定时器。"""
        if self._pending_timer is not None:
            self._pending_timer.cancel()
        self._pending_timer = threading.Timer(delay, self._on_debounce_elapsed)
        self._pending_timer.daemon = True
        self._pending_timer.start()

    def _on_debounce_elapsed(self):
        """对方停下 debounce_sec 后的自动触发决策点。

        ⚠️ 冷却期未过时【重排定时器】而不是丢弃：直接丢弃会导致对方讲完一大段
        后恰好落在冷却窗口里，这一轮就永远等不到建议 —— 而那往往正是最该
        给建议的时刻。重排让它在冷却结束时补上。
        """
        with self._lock:
            if self._suggestions_paused or self._busy:
                return
            remaining = MIN_INTERVAL_SEC - (time.time() - self._last_auto_fire_at)
            if remaining > 0:
                self._arm_timer(remaining)
                return
            # 新增内容不足：本轮没有新信息值得再给一批建议。
            # 但明确说出 decision / action item 时要立即放行，即使这句英文很短，
            # 否则中英混写下决策和待办会被 40 字闸门一起吞掉。
            if (
                self._new_chars < MIN_NEW_CHARS
                and not _local_memory_candidates(self._recent_context())
            ):
                return
            self._pending_timer = None
        self.fire_suggestion(trigger="auto")

    def fire_suggestion(self, trigger="auto"):
        with self._lock:
            if self._suggestions_paused:
                return
            if self._busy:
                # 生成期间不排队重跑：那正是刷屏的放大器。新内容留给下一次
                # debounce，对方继续说话自然会把定时器重新起起来。
                return
            context = self._recent_context()
            if not context:
                return
            context_range = self._context_range(context)
            merge_count = self._pending_merge_count
            self._pending_merge_count = 0
            self._latest_context_pending = False
            self._new_chars = 0
            self._busy = True
        emit("suggestion_status", status="generating", trigger=trigger, scene=self.scene)
        started = time.time()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = self.engine.suggest(
                    context, count=self.suggestion_count
                )
            with self._lock:
                if self._suggestions_paused:
                    return
            memory_candidates = []
            by_memory_key = set()
            for candidate in [*(result.get("memoryCandidates") or []), *_local_memory_candidates(context)]:
                if not isinstance(candidate, dict):
                    continue
                content = str(candidate.get("content") or "").strip()
                kind = candidate.get("kind") if candidate.get("kind") in ("decision", "action_item") else None
                if not content or not kind:
                    continue
                key = f"{kind}:{content.lower()}"
                if key in by_memory_key:
                    continue
                by_memory_key.add(key)
                memory_candidates.append({
                    "id": str(candidate.get("id") or f"memory-{abs(hash(key)) % 10**12:x}"),
                    "kind": kind,
                    "status": "candidate",
                    "content": content[:500],
                    "owner": candidate.get("owner"),
                    "dueAt": candidate.get("dueAt"),
                    "evidenceTranscriptId": candidate.get("evidenceTranscriptId"),
                    "evidenceText": candidate.get("evidenceText"),
                    "source": candidate.get("source") if candidate.get("source") in ("rule", "model", "user") else "rule",
                })
            emit(
                "suggestions",
                suggestions=[
                    _public_suggestion(s) for s in result.get("suggestions", [])
                ],
                hits=result.get("hits", []),
                # 生成失败要如实上报，让界面显示"可重试"而不是假装有建议
                parseError=result.get("error"),
                context=context_range,
                elapsed=round(time.time() - started, 3),
                trigger=trigger,
                runtime={
                    "provider": getattr(self.engine, "provider", ""),
                    "model": getattr(self.engine, "model", ""),
                    "elapsed": round(time.time() - started, 3),
                    "trigger": trigger,
                    "timeoutStage": (result.get("error") or {}).get("timeoutStage"),
                    "errorKind": (result.get("error") or {}).get("kind"),
                    "retryable": (result.get("error") or {}).get("retryable"),
                    "attempts": (result.get("error") or {}).get("attempts"),
                    "timeoutSeconds": (result.get("error") or {}).get(
                        "timeoutSeconds", 12.0
                    ),
                    "mergeCount": merge_count,
                    "contextChars": sum(len(str(item.get("text") or "")) for item in context),
                },
                memoryCandidates=memory_candidates,
                at=time.time(),
            )
        except Exception as exc:
            emit("error", stage="suggestion", message=str(exc))
        finally:
            with self._lock:
                self._busy = False
                # 冷却与增量都从"这批发出去"开始重新计
                self._last_auto_fire_at = time.time()
                if self._latest_context_pending and self._new_chars > 0 and not self._suggestions_paused:
                    # 只补一轮最新上下文；中间收到多少段都由一个计数合并。
                    self._arm_timer(self.debounce_sec)
                else:
                    self._new_chars = 0
                    self._latest_context_pending = False
                    self._pending_merge_count = 0

    def suggest_now(self):
        """用户手动要建议：绕过冷却与增量门槛，立刻生成。"""
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
            if not self.transcript:
                emit("suggestion_status", status="skipped",
                     message="还没有转写内容")
                return
        threading.Thread(
            target=self.fire_suggestion,
            kwargs={"trigger": "manual"},
            daemon=True,
        ).start()

    def set_suggestions_paused(self, paused):
        with self._lock:
            self._suggestions_paused = paused
            if paused and self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

    def ask(self, question):
        with self._lock:
            context = self._recent_context()
            context_range = self._context_range(context)
        emit("answer_status", status="generating", question=question)
        started = time.time()
        first_at = [None]

        def on_delta(piece):
            # 记录首字时刻，用于验证 ASK-3 的"首字 < 3s"
            if first_at[0] is None:
                first_at[0] = time.time()
                emit("answer_first_token",
                     latency=round(first_at[0] - started, 3))
            emit("answer_delta", question=question, delta=piece)

        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = self.engine.answer(question, context, on_delta=on_delta)
            hits = result.get("hits", [])
            emit(
                "answer",
                question=question,
                answer=result.get("answer", ""),
                hits=hits,
                # 检索为空时回答里不可能有知识库依据。此前桌面端把问答卡
                # 硬编码成"有依据"，未关联任何文档时也照样显示 —— 必须由
                # 实际检索结果说了算。
                grounded=bool(hits),
                context=context_range,
                elapsed=round(time.time() - started, 3),
                firstToken=first_at[0] and round(first_at[0] - started, 3),
                at=time.time(),
            )
        except Exception as exc:
            emit("error", stage="answer", message=str(exc))


def command_loop(
    session_holder,
    stop_event,
    recording_paused,
    begin_event=None,
    input_stream=None,
):
    """session_holder: dict with optional key 'session' (filled after prep)."""
    for line in input_stream or sys.stdin:
        if stop_event.is_set():
            return
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            emit("error", stage="command", message="无法解析桌面端命令")
            continue
        if command.get("command") == "stop":
            stop_event.set()
            if begin_event is not None:
                begin_event.set()
            return
        if command.get("command") == "begin_recording":
            # 准备阶段结束后，由桌面端确认再真正开麦/计时
            if begin_event is not None:
                begin_event.set()
            emit("status", stage="arming", message="正在正式开始录制…")
            continue
        session = (session_holder or {}).get("session")
        if session is None:
            # 等待 begin 期间仅处理 stop / begin
            continue
        if command.get("command") == "set_controls":
            if isinstance(command.get("recordingPaused"), bool):
                if command["recordingPaused"]:
                    recording_paused.set()
                else:
                    recording_paused.clear()
            if isinstance(command.get("suggestionsPaused"), bool):
                session.set_suggestions_paused(command["suggestionsPaused"])
            emit(
                "controls",
                recordingPaused=recording_paused.is_set(),
                suggestionsPaused=session._suggestions_paused,
            )
        if command.get("command") == "suggest_now":
            session.suggest_now()
        if command.get("command") == "set_me":
            session.set_me(command.get("speakerId"))
        if command.get("command") == "ask" and command.get("question"):
            threading.Thread(
                target=session.ask,
                args=(str(command["question"]),),
                daemon=True,
            ).start()


def wait_for_recording_start(
    begin_event,
    stop_event,
    *,
    wav_in=False,
    timeout_seconds=120,
):
    """Wait for the renderer cue without opening audio during preparation."""
    if wav_in:
        begin_event.set()
    else:
        begin_event.wait(timeout=timeout_seconds)
    return not stop_event.is_set()


def _resolve_doc_paths(args):
    """解析本场会议选中的知识文档。

    ⚠️ 知识范围隔离：桌面端必须显式传入本场允许使用的文档路径。
    只有在完全未指定时（如命令行独立调试）才回退到全局 docs/ 目录 ——
    正式会议链路绝不应走到那个回退分支，否则会串用其它项目的资料。
    """
    if args.docs_file:
        with open(args.docs_file, "r", encoding="utf-8") as f:
            return json.load(f)
    if args.docs:
        return json.loads(args.docs)
    return None


def classify_startup_error(component, error):
    """把启动异常归到 UI 可操作的故障域；不依赖供应商私有异常类型。"""
    message = str(error or "").lower()
    credential_hints = (
        "key",
        "secret",
        "token",
        "appid",
        "auth",
        "unauthorized",
        "凭证",
        "鉴权",
        "填入",
        "未配置",
    )
    model_hints = (
        "model",
        "onnx",
        "sherpa",
        "no module named",
        "模型",
        "加载",
        "依赖",
    )
    if any(hint in message for hint in credential_hints):
        return f"{component}_credentials"
    if any(hint in message for hint in model_hints):
        return "model_load"
    return f"{component}_service"


def run(args):
    emit("status", stage="initializing", message="正在加载知识库与模型配置")
    online_mode = getattr(args, "meeting_mode", "in_person") == "online"
    if online_mode and OnlineMeetingStream is None:
        emit(
            "error",
            stage="system_audio",
            message="线上会议音频组件未安装，请重新安装项目依赖",
            fatal=True,
        )
        return
    doc_paths = _resolve_doc_paths(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            kb = providers.build_kb(verbose=False, doc_paths=doc_paths)
    except (Exception, SystemExit) as exc:
        emit(
            "error",
            stage="model_load",
            component="knowledge",
            message=str(exc) or "知识库加载失败",
            fatal=True,
        )
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = providers.build_llm(
                kb,
                me_name="我",
                provider=args.provider,
                model=args.llm_model,
                scene=getattr(args, "scene", "general"),
                timeout_seconds=12.0,
                retry_attempts=2,
            )
    except (Exception, SystemExit) as exc:
        emit(
            "error",
            stage=classify_startup_error("llm", exc),
            message=str(exc) or "建议模型配置不可用",
            fatal=True,
        )
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):
            # 专有名词库 → 仅阿里 Paraformer 实时会同步 vocabulary_id；
            # 其它 ASR 忽略词表（词库仍允许用户维护，只是本场不生效）。
            vocabulary_id = getattr(args, "vocabulary_id", None) or None
            if vocabulary_id:
                vocabulary_id = str(vocabulary_id).strip() or None
            hotwords_path = getattr(args, "hotwords_file", None)
            asr_provider = args.asr_provider or None
            terms = []
            try:
                import asr_hotwords
                from asr_hotwords import (
                    ensure_aliyun_vocabulary_id,
                    load_hotwords_file,
                )
                from providers import _cfg as _prov_cfg

                terms = load_hotwords_file(hotwords_path)
                resolved_asr = (asr_provider or _prov_cfg("ASR_PROVIDER", default="xfyun") or "xfyun")
                if terms and str(resolved_asr).lower() in ("aliyun", "ali", "dashscope"):
                    if vocabulary_id:
                        sync_warning = getattr(asr_hotwords, "LAST_SYNC_DIAGNOSTIC", None)
                        emit(
                            "status",
                            stage="hotwords",
                            hotwordStatus="loaded",
                            hotwordCount=len(terms),
                            vocabularyId=str(vocabulary_id),
                            hotwordReason=sync_warning or None,
                            message=(
                                f"已使用预同步专有名词 {len(terms)} 个（阿里热词）"
                                + (f"；{sync_warning}" if sync_warning else "")
                            ),
                        )
                    else:
                        emit(
                            "status",
                            stage="hotwords",
                            hotwordStatus="pending",
                            hotwordCount=len(terms),
                            message=f"正在同步专有名词 {len(terms)} 个（阿里热词）",
                        )
                        api_key = _prov_cfg("ALIYUN_ASR_KEY", "ALIYUN_API_KEY")
                        vocabulary_id = ensure_aliyun_vocabulary_id(
                            terms,
                            api_key=api_key,
                            target_model="paraformer-realtime-v2",
                        )
                        if vocabulary_id:
                            sync_warning = getattr(asr_hotwords, "LAST_SYNC_DIAGNOSTIC", None)
                            emit(
                                "status",
                                stage="hotwords",
                                hotwordStatus="loaded",
                                hotwordCount=len(terms),
                                vocabularyId=str(vocabulary_id),
                                hotwordReason=sync_warning or None,
                                message=(
                                    f"已加载专有名词 {len(terms)} 个（阿里热词）"
                                    + (f"；{sync_warning}" if sync_warning else "")
                                ),
                            )
                        else:
                            reason = (
                                getattr(asr_hotwords, "LAST_SYNC_DIAGNOSTIC", None)
                                or "供应商未返回具体原因"
                            )
                            emit(
                                "status",
                                stage="hotwords",
                                hotwordStatus="degraded",
                                hotwordCount=len(terms),
                                hotwordReason=f"阿里热词同步失败：{reason}",
                                message=(
                                    f"专有名词 {len(terms)} 个未能同步到阿里，"
                                    f"本场按无热词转写（{reason}）"
                                ),
                            )
                elif terms:
                    emit(
                        "status",
                        stage="hotwords",
                        hotwordStatus="unsupported",
                        hotwordCount=len(terms),
                        hotwordReason=f"当前转写服务「{resolved_asr}」不读取热词",
                        message=(
                            f"专有名词库有 {len(terms)} 个词，"
                            f"当前转写服务「{resolved_asr}」本版不会读取，词库仍保留"
                        ),
                    )
                else:
                    emit(
                        "status",
                        stage="hotwords",
                        hotwordStatus="empty",
                        hotwordCount=0,
                        hotwordReason="本场没有配置专有名词",
                        message="本场未配置专有名词，按默认词表转写",
                    )
            except Exception as hot_exc:
                safe_hot_exc = asr_hotwords._safe_diagnostic(hot_exc)
                emit(
                    "status",
                    stage="hotwords",
                    hotwordStatus="degraded",
                    hotwordCount=len(terms),
                    hotwordReason=f"热词准备异常：{safe_hot_exc}",
                    message=f"热词准备异常，本场按无热词转写（{safe_hot_exc}）",
                )
                print(f"[热词] 准备失败：{safe_hot_exc}", flush=True)

            # ASR 供应商 / 识别语种 / 模型现在也可被 UI 覆盖（此前只能读 config）
            asr = providers.build_asr(
                provider=args.asr_provider,
                model=getattr(args, "asr_model", None),
                lang=getattr(args, "asr_lang", None),
                vocabulary_id=vocabulary_id,
            )
            # 线上会议两条物理通道天然知道身份。分别转写，避免把混音重新交给
            # 云端猜说话人；代价是 ASR 并发和用量约为线下模式的两倍。
            asr_other = (
                providers.build_asr(
                    provider=args.asr_provider,
                    model=getattr(args, "asr_model", None),
                    lang=getattr(args, "asr_lang", None),
                    vocabulary_id=vocabulary_id,
                )
                if online_mode
                else None
            )
    except (Exception, SystemExit) as exc:
        emit(
            "error",
            stage=classify_startup_error("asr", exc),
            message=str(exc) or "语音转写配置不可用",
            fatal=True,
        )
        return

    # 方案 A+D1：--enroll-wav 存在则启用本地声纹认「我」
    me_detector = None
    voiceprint_mode = False
    enroll_wav = getattr(args, "enroll_wav", None)
    me_threshold = float(getattr(args, "me_threshold", None) or 0.65)
    if enroll_wav and not online_mode:
        if MeIdentifier is None:
            emit("error", stage="voiceprint",
                 message="未安装 sherpa-onnx，无法启用声纹认我")
        else:
            emit("status", stage="voiceprint", message="正在加载声纹模型并注册「我」")
            try:
                me_detector = MeIdentifier(enroll_wav, threshold=me_threshold)
            except Exception as exc:
                emit(
                    "error",
                    stage="model_load",
                    component="voiceprint",
                    message=f"声纹模型加载失败：{exc}",
                    fatal=False,
                )
                me_detector = None
            if me_detector is not None and me_detector.ready:
                voiceprint_mode = True
                emit(
                    "voiceprint_ready",
                    # 多段注册后是列表；保留 enrollWav 为首个样本以兼容旧渲染端
                    enrollWav=enroll_wav[0],
                    enrollWavs=list(enroll_wav),
                    enrollSamples=len(enroll_wav),
                    threshold=me_threshold,
                    enrollSegments=me_detector.enroll_segments,
                    meSpeakerId=SPEAKER_ID_ME,
                )
            elif me_detector is not None:
                emit(
                    "error",
                    stage="voiceprint",
                    message=f"声纹注册失败：{me_detector.error}",
                )
                me_detector = None

    emit(
        "knowledge_scope",
        documentCount=len({c["source"] for c in kb.chunks}),
        chunkCount=len(kb.chunks),
        missing=[
            {"path": p, "name": p.replace("\\", "/").rsplit("/", 1)[-1]}
            for p in getattr(kb, "missing_paths", [])
        ],
        parseErrors=getattr(kb, "parse_errors", []),
        scoped=doc_paths is not None,
    )

    # 录音器必须在 ASR 会话和音频源之前创建。第一块真正写入 WAV 的 PCM
    # 就是播放器 t=0；不能再拿“用户点击开始”或“ASR 连接成功”当零点。
    recorder = None
    if args.audio_out:
        try:
            os.makedirs(os.path.dirname(args.audio_out), exist_ok=True)
            track_paths = {"mixed": args.audio_out}
            if online_mode:
                if args.mic_audio_out:
                    track_paths["mic"] = args.mic_audio_out
                if args.system_audio_out:
                    track_paths["system"] = args.system_audio_out
            recorder = AudioRecorder(
                args.audio_out,
                config.SAMPLE_RATE,
                config.CHANNELS,
                track_paths=track_paths,
            )
            emit(
                "recording_file",
                path=args.audio_out,
                tracks=recorder.status(),
            )
            for failure in recorder.failures:
                emit(
                    "error",
                    stage="recording_file",
                    channel=failure["name"],
                    message=failure["error"],
                )
        except Exception as exc:
            # 录音落盘失败不应中断会议：转写和建议才是核心价值
            emit("error", stage="recording_file", message=str(exc))
    audio_clock = None
    channel_clocks = {}
    if recorder is not None:
        if online_mode:
            channel_clocks = {
                SPEAKER_ID_ME: RecordingSampleClock(
                    config.SAMPLE_RATE, config.CHANNELS
                ),
                SPEAKER_ID_OTHER: RecordingSampleClock(
                    config.SAMPLE_RATE, config.CHANNELS
                ),
            }
        else:
            audio_clock = RecordingSampleClock(
                config.SAMPLE_RATE, config.CHANNELS
            )

    session = BridgeSession(
        engine,
        SPEAKER_ID_ME if online_mode else args.me,
        me_detector=me_detector,
        voiceprint_mode=voiceprint_mode,
        debounce_sec=getattr(args, "silence_seconds", None) or DEBOUNCE_SEC,
        suggestion_count=getattr(args, "suggestion_count", None) or 3,
        audio_clock=audio_clock,
        scene=getattr(args, "scene", "general"),
    )
    session_holder = {"session": session}
    emit(
        "suggestion_config",
        silenceSeconds=session.debounce_sec,
        suggestionCount=session.suggestion_count,
        minIntervalSec=MIN_INTERVAL_SEC,
        minNewChars=MIN_NEW_CHARS,
        scene=session.scene,
        provider=getattr(engine, "provider", ""),
        model=getattr(engine, "model", ""),
        timeoutSeconds=12,
        retryAttempts=2,
    )
    emit(
        "meeting_mode",
        mode="online" if online_mode else "in_person",
        speakerMapping=(
            {"microphone": "我", "system": "对方"} if online_mode else None
        ),
    )
    if voiceprint_mode:
        # 固定立场：me；UI 仍可改名，但不需要再手动认领角色号
        emit("me_changed", speakerId=SPEAKER_ID_ME, source="voiceprint")
    stop_event = threading.Event()
    recording_paused = threading.Event()
    begin_event = threading.Event()
    # 准备阶段即可收 stop / begin_recording；session 命令在 session 建好后才处理
    threading.Thread(
        target=command_loop,
        args=(session_holder, stop_event, recording_paused, begin_event),
        daemon=True,
    ).start()

    # 麦克风/ASR 在桌面端确认「正式开始」后再启动，避免准备期计入会议时间与录音轴。
    # 注意：AudioRecorder 文件已创建，真正写入 PCM 仍随下方音频循环开始。
    emit(
        "status",
        stage="ready",
        message="设备与模型已就绪，等待正式开始录制",
        model=getattr(engine, "label", ""),
    )
    # 桌面端会提示后发 begin_recording；若未发，最长等待 120s 后仍开录以免卡死。
    # wav 回放验证不等人，直接开始。
    if not wait_for_recording_start(
        begin_event,
        stop_event,
        wav_in=bool(getattr(args, "wav_in", None)),
        timeout_seconds=120,
    ):
        emit("status", stage="cancelled", message="已在正式开录前取消")
        return

    emit(
        "status",
        stage="connecting",
        message="正在连接实时语音识别并开始录制",
        model=getattr(engine, "label", ""),
    )
    # 断网重连状态上报（PRD TRS-6）：录音不受影响，但必须让用户知道
    # 这段时间的转写是缺失的，否则他会以为"没人说话"。
    def configure_asr(asr_client, channel, fixed_speaker=None, clock=None):
        is_aliyun = asr_client.__class__.__name__ == "AliyunASR"
        if is_aliyun:
            language_hints = list(getattr(asr_client, "language_hints", []) or [])
            model = str(getattr(asr_client, "model", "") or "")
            sample_rate = int(getattr(asr_client, "sample_rate", 0) or 0)
            emit(
                "status",
                stage="asr_config",
                provider="aliyun",
                model=model,
                languageHints=language_hints,
                sampleRate=sample_rate,
                vocabularyConfigured=bool(getattr(asr_client, "vocabulary_id", None)),
                channel=channel,
                message=(
                    f"阿里 ASR 实际启动参数：model={model}，"
                    f"language_hints={language_hints}，sample_rate={sample_rate}"
                ),
            )
        if hasattr(asr_client, "set_state_callback"):
            def on_asr_state(state, message):
                if clock is not None:
                    if state == "reconnected":
                        clock.reset_asr_session()
                    elif state in ("disconnected", "reconnecting", "failed"):
                        clock.set_accepting_audio(False)
                emit(
                    "asr_connection",
                    state=state,
                    message=message,
                    channel=channel,
                    at=time.time(),
                )
            asr_client.set_state_callback(on_asr_state)

        if fixed_speaker is None:
            asr_client.start(session.on_transcript)
            return

        def on_channel_transcript(
            text,
            speaker,
            is_final,
            end_ms=0,
            begin_ms=None,
            words=None,
        ):
            session.on_transcript(
                text,
                fixed_speaker,
                is_final,
                end_ms=end_ms,
                begin_ms=begin_ms,
                words=words,
                audio_clock=clock,
            )

        asr_client.start(on_channel_transcript)

    try:
        if online_mode:
            configure_asr(
                asr,
                "microphone",
                fixed_speaker=SPEAKER_ID_ME,
                clock=channel_clocks.get(SPEAKER_ID_ME),
            )
            configure_asr(
                asr_other,
                "system",
                fixed_speaker=SPEAKER_ID_OTHER,
                clock=channel_clocks.get(SPEAKER_ID_OTHER),
            )
        else:
            configure_asr(asr, "microphone", clock=audio_clock)
    except Exception as exc:
        emit(
            "error",
            stage=classify_startup_error("asr", exc),
            message=str(exc) or "语音转写服务连接失败",
            fatal=True,
        )
        try:
            asr.stop()
        except Exception:
            pass
        if asr_other is not None:
            try:
                asr_other.stop()
            except Exception:
                pass
        return
    emit(
        "status",
        stage="listening",
        message=(
            f"正在回放录音（{os.path.basename(args.wav_in)}）"
            if getattr(args, "wav_in", None)
            else (
                "麦克风与系统音频已连接，正在听取线上会议"
                if online_mode
                else "麦克风已连接，正在听取会议"
            )
        ),
    )

    throttle = LevelThrottle()
    # --wav-in：把录音当麦克风回放，不开会也能跑通整条会中链路（见 wav_stream.py）。
    # 除了音频来源，下面的处理路径与真实会议**完全一致**——这正是它的价值所在，
    # 一旦为回放开特例，验证出来的就不是真实链路了。
    source = None
    if getattr(args, "wav_in", None):
        if WavStream is None:
            emit("error", stage="audio", message="缺少 wav_stream 模块，无法回放")
            return
        source = WavStream(
            args.wav_in,
            sample_rate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_MS,
            speed=getattr(args, "wav_speed", 1.0) or 1.0,
            on_progress=lambda done, total: emit(
                "replay_progress",
                audioSec=round(done, 1),
                totalSec=round(total, 1),
                at=time.time(),
            ),
        )
    elif online_mode:
        if OnlineMeetingStream is None:
            emit(
                "error",
                stage="system_audio",
                message="线上会议音频组件未安装，请重新安装项目依赖",
                fatal=True,
            )
            return
        source = OnlineMeetingStream(
            sample_rate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_MS,
            device=args.device,
        )
    else:
        source = MicStream(
            sample_rate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            frame_ms=config.FRAME_MS,
            device=args.device,
        )
    emitted_audio_source_errors = set()

    def emit_audio_source_errors():
        if source is None or not hasattr(source, "drain_runtime_errors"):
            return
        for error in source.drain_runtime_errors():
            key = (error.get("channel"), error.get("message"))
            if key in emitted_audio_source_errors:
                continue
            emitted_audio_source_errors.add(key)
            emit(
                "error",
                stage=error.get("stage") or "audio",
                channel=error.get("channel"),
                message=error.get("message") or "音频源已降级为静音",
                fatal=False,
            )

    try:
        with source as mic:
            if online_mode:
                emit(
                    "system_audio_ready",
                    device=getattr(mic, "loopback_info", None),
                )
            for captured in mic.frames():
                emit_audio_source_errors()
                if online_mode:
                    pcm, mic_pcm, system_pcm = captured
                else:
                    pcm = captured
                    mic_pcm = pcm
                    system_pcm = None
                if stop_event.is_set():
                    break
                if recording_paused.is_set():
                    if throttle.should_emit(0):
                        emit("audio_level", level=0)
                    # 保持实时 ASR 连接存活，但绝不发送暂停期间的真实音频。
                    # 暂停期间也不写入录音文件。
                    silence = bytes(len(pcm))
                    if audio_clock is not None:
                        audio_clock.advance(silence, recorded=False)
                    for clock in channel_clocks.values():
                        clock.advance(silence, recorded=False)
                    asr.send(silence)
                    if asr_other is not None:
                        asr_other.send(silence)
                    continue
                raw = int(np.abs(np.frombuffer(pcm, dtype=np.int16)).mean())
                level = min(raw / 4000, 1)
                if throttle.should_emit(level):
                    emit("audio_level", level=level)
                if recorder is not None:
                    recorder.write_tracks(
                        mixed=pcm,
                        mic=mic_pcm if online_mode else None,
                        system=system_pcm if online_mode else None,
                    )
                if audio_clock is not None:
                    audio_clock.advance(pcm, recorded=True)
                if online_mode:
                    channel_clocks[SPEAKER_ID_ME].advance(
                        mic_pcm, recorded=True
                    )
                    channel_clocks[SPEAKER_ID_OTHER].advance(
                        system_pcm, recorded=True
                    )
                # 声纹和 ASR 回调都消费录音轴时间，所以先登记/喂入本地数据，
                # 再发送给可能同步触发回调的供应商 SDK。
                if me_detector is not None and me_detector.ready:
                    me_detector.feed_pcm(pcm)
                if online_mode:
                    asr.send(mic_pcm)
                    asr_other.send(system_pcm)
                else:
                    asr.send(pcm)
            # 回放读完就结束了，但最后一句的 final 还在路上，最后一批建议
            # 可能正在生成。真实会议里这段时间由"用户点结束"自然覆盖；
            # 回放不等一下就会把尾巴切掉，验证结果里凭空少几条。
            if getattr(args, "wav_in", None) and not stop_event.is_set():
                emit("status", stage="draining",
                     message="录音已放完，等待收尾的转写与建议")
                deadline = time.time() + REPLAY_DRAIN_MAX_SEC
                # 先无条件等一会儿：最后一句的 final 本身就滞后好几秒，
                # 它还没到就判"没有待办"会直接把最后一轮建议漏掉
                settle_until = time.time() + REPLAY_DRAIN_MIN_SEC
                while time.time() < deadline:
                    time.sleep(0.3)
                    if time.time() < settle_until:
                        continue
                    if not session.busy and not session.pending:
                        break
            emit_audio_source_errors()
    except Exception as exc:
        if MicrophoneStreamError is not None and isinstance(
            exc, MicrophoneStreamError
        ):
            stage = "microphone"
        elif SystemAudioStreamError is not None and isinstance(
            exc, SystemAudioStreamError
        ):
            stage = "system_audio"
        else:
            stage = "microphone" if not online_mode else "audio"
        emit("error", stage=stage, message=str(exc), fatal=True)
    finally:
        emit_audio_source_errors()
        audio_source_status = None
        if source is not None and hasattr(source, "source_status"):
            try:
                audio_source_status = source.source_status()
            except Exception:
                audio_source_status = None
        stop_event.set()
        if me_detector is not None:
            try:
                me_detector.flush()
            except Exception:
                pass
        # ⚠️ 收尾里的任何一步都不许把 `ended` 掀掉。
        #    实测（回放验证时发现）：连接已被服务端关闭后再 asr.stop() 会抛异常，
        #    整个 finally 从这里断掉 —— 录音时长、声纹统计、ended 事件全部没发出去，
        #    渲染端只能靠 bridge_closed 兜底，会议档案里少一截信息。
        try:
            with contextlib.redirect_stdout(sys.stderr):
                asr.stop()
        except Exception as exc:
            emit("error", stage="asr_stop", message=str(exc))
        if asr_other is not None:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    asr_other.stop()
            except Exception as exc:
                emit(
                    "error",
                    stage="asr_stop",
                    channel="system",
                    message=str(exc),
                )
        try:
            saved = recorder.close() if recorder is not None else None
        except Exception as exc:
            saved = None
            emit("error", stage="recording_file", message=str(exc))
        try:
            vp = me_detector.stats() if me_detector is not None else None
        except Exception:
            vp = None
        emit(
            "ended",
            transcriptCount=len(session.transcript),
            audio=saved,
            voiceprint=vp,
            audioSources=audio_source_status,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--test-device", type=int)
    parser.add_argument("--test-duration", type=float, default=6)
    parser.add_argument("--me")
    parser.add_argument("--device", type=int)
    parser.add_argument(
        "--meeting-mode",
        dest="meeting_mode",
        choices=("in_person", "online"),
        default="in_person",
    )
    parser.add_argument(
        "--scene",
        choices=("general", "sales", "requirements"),
        default="general",
        help="会议场景：general / sales / requirements",
    )
    parser.add_argument("--provider")          # LLM 供应商（UI 切换）
    parser.add_argument("--llm-model", dest="llm_model")  # LLM 模型名（UI 探测选中）
    parser.add_argument("--asr-provider", dest="asr_provider")  # ASR 供应商（UI 切换）
    parser.add_argument("--asr-model", dest="asr_model")        # ASR 模型名（UI 选中或配置覆盖）
    # 识别语种：zh / en / zh_en。限制自动语种识别串出日文等（见 HANDOFF / 设置页）
    parser.add_argument("--asr-lang", dest="asr_lang",
                        help="识别语种：zh / en / zh_en（默认读 ASR_LANG 或 zh_en）")
    # 本场会议的知识范围。docs-file 优先，避免文档很多时超出命令行长度限制。
    parser.add_argument("--docs", help="选中文档路径的 JSON 数组")
    parser.add_argument("--docs-file", help="存放上述 JSON 的临时文件路径")
    parser.add_argument(
        "--hotwords-file",
        dest="hotwords_file",
        help="专有名词 JSON（通用+项目合并后的临时文件）",
    )
    parser.add_argument(
        "--vocabulary-id",
        dest="vocabulary_id",
        help="预同步好的阿里热词 vocabulary_id，传入则跳过云端 create/update",
    )
    parser.add_argument("--audio-out", help="录音落盘的 wav 路径（边录边写）")
    parser.add_argument("--mic-audio-out", dest="mic_audio_out",
                        help="线上会议麦克风独立音轨路径（可选）")
    parser.add_argument("--system-audio-out", dest="system_audio_out",
                        help="线上会议系统回环独立音轨路径（可选）")
    # 验证用：把已有录音当麦克风回放，不开会也能跑通整条会中链路
    parser.add_argument("--wav-in", dest="wav_in",
                        help="回放这个 wav 代替麦克风（16kHz 单声道）")
    parser.add_argument("--wav-speed", dest="wav_speed", type=float, default=1.0,
                        help="回放倍速，默认 1.0（>1 会让建议闸门失真，仅供压测）")
    # 设置页「建议触发」两项（此前只存在于界面，从未生效）
    parser.add_argument("--silence-seconds", dest="silence_seconds", type=float,
                        help=f"对方停顿多久后给建议，默认 {DEBOUNCE_SEC}")
    parser.add_argument("--suggestion-count", dest="suggestion_count", type=int,
                        help="每批生成几条建议，默认 3")
    # 方案 D1：本地声纹认「我」。Electron 在 enroll wav 存在时一定会传这两个，
    # ⚠️ 漏声明会让 argparse 直接 exit(2)，表现为「会议一开就结束」——
    #    消费侧用的 getattr(args, "enroll_wav", None) 挡不住，那时早已退出。
    parser.add_argument("--enroll-wav", dest="enroll_wav", action="append",
                        help="声纹注册 wav；可重复传多个（多段注册）。给了就启用本地认「我」")
    parser.add_argument("--me-threshold", dest="me_threshold", type=float,
                        help="声纹 1:1 判定阈值，默认 0.65")
    args = parser.parse_args()
    if args.list_devices:
        list_devices()
        return
    if args.test_device is not None:
        test_device(args.test_device, max(1, min(args.test_duration, 15)))
        return
    run(args)


if __name__ == "__main__":
    main()
