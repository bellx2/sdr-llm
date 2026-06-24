#!/usr/bin/env python3
"""
RTL-SDR FM受信 → AmiVoice Wrp ストリーミング → Gemini要約+抽出 → ブラウザ表示
"""

import argparse
import asyncio
import json
import os
import queue
import struct
import sys
import threading
import wave
from datetime import datetime
from pathlib import Path

import httpx
import websockets
import websockets.server
from dotenv import load_dotenv

# AmiVoice 公式 Wrp クライアントライブラリ
# https://github.com/advanced-media-inc/amivoice-api-client-library
WRP_SRC = Path(__file__).parent / "vendor" / "amivoice-wrp"
sys.path.insert(0, str(WRP_SRC))

import com.amivoice.wrp.Wrp as AmiWrp  # noqa: E402
import com.amivoice.wrp.WrpListener as AmiWrpListenerBase  # noqa: E402

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"環境変数 {name} が未設定です。.env を確認してください。")
    return value


# ===== 設定 =====
FREQUENCY       = "433.00M"
SAMPLE_RATE_OUT = "16000"
FM_BANDWIDTH    = os.getenv("FM_BANDWIDTH", "25k")   # NBFM: 12.5k / 25k（15kは非標準で歪みやすい）
GAIN            = os.getenv("RTL_GAIN", "0")         # 0=自動ゲイン
RTL_PPM         = os.getenv("RTL_PPM", "0")        # 周波数補正（例: 50）

AMIVOICE_SERVER_URL = os.getenv(
    "AMIVOICE_SERVER_URL", "wss://acp-api.amivoice.com/v1/recognize"
)
AMIVOICE_APP_KEY = os.getenv("AMIVOICE_APP_KEY", "")
AMIVOICE_CODEC = os.getenv("AMIVOICE_CODEC", "LSB16K")
AMIVOICE_GRAMMAR = os.getenv("AMIVOICE_GRAMMAR", "-a2-ja-general")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

BROWSER_WS_PORT = 8765
CHUNK_BYTES     = 3200   # 16kHz 16bit 100ms
BUFFER_SECONDS  = 60     # 1分

# ===== Gemini プロンプト =====
# 要約 + 交信ログ抽出を1回のリクエストで行う
# 出力は必ずJSONのみ（マークダウン不可）
SYSTEM_PROMPT = """あなたはアマチュア無線・FM無線の受信ログを解析するアシスタントです。
与えられたテキストログを解析し、以下のJSON形式のみで応答してください。
マークダウン、コードブロック、説明文は一切含めないこと。

{
  "summary": "200字以内の日本語要約",
  "topics": ["トピック1", "トピック2"],
  "calls": [
    {
      "type": "CQ" または "個別",
      "from": "発信者（コールサインや名前。不明なら null）",
      "to":   "受信者（個別交信の場合。CQや不明なら null）",
      "time": "最初に登場した時刻 HH:MM:SS",
      "note": "補足（省略可、なければ null）"
    }
  ]
}

交信の種別ルール:
- テキスト中に「CQ」という語が含まれる発話 → type: "CQ"、to: null
- それ以外 → type: "個別"
発信者の抽出:
- 「こちらは〜」の〜部分をfromに使う
受信者の抽出:
- 「〇〇、こちらは〜」の〇〇部分をtoに使う（個別交信のみ）
不明な場合は必ず null を使い、空文字は使わないこと。
calls が0件の場合は空配列 [] を返すこと。"""

def _validate_api_keys():
    _require_env("AMIVOICE_APP_KEY")
    _require_env("GEMINI_API_KEY")


def _print_rx_params():
    print(
        f"[受信設定] {FREQUENCY} · FM bandwidth={FM_BANDWIDTH} "
        f"· gain={GAIN} · ppm={RTL_PPM} · audio={SAMPLE_RATE_OUT}Hz"
    )


# ===== ブラウザ接続管理 =====
browser_clients: set = set()

async def browser_handler(websocket):
    browser_clients.add(websocket)
    print(f"[Browser] 接続: {websocket.remote_address}")
    await websocket.send(json.dumps(_browser_config(), ensure_ascii=False))
    try:
        await websocket.wait_closed()
    finally:
        browser_clients.discard(websocket)


def _browser_config() -> dict:
    mode = "NBFM" if FM_BANDWIDTH in ("12.5k", "15k", "25k") else "FM"
    return {
        "type": "config",
        "frequency": FREQUENCY,
        "mode": mode,
        "buffer_seconds": BUFFER_SECONDS,
    }

async def broadcast(message: dict):
    if not browser_clients:
        return
    data = json.dumps(message, ensure_ascii=False)
    await asyncio.gather(
        *[ws.send(data) for ws in list(browser_clients)],
        return_exceptions=True,
    )

# ===== Gemini 呼び出し =====
async def call_gemini(text_log: str) -> dict:
    """ログを送り、{summary, topics, calls} のdictを返す。"""
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "parts": [{"text": f"以下は過去{BUFFER_SECONDS // 60}分間の受信テキストログです。\n\n{text_log}"}]
        }],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.2,
            "responseMimeType": "application/json",   # JSON出力を強制
        },
    }
    url = f"{GEMINI_API_URL}?key={_require_env('GEMINI_API_KEY')}"
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(url, json=payload,
                                 headers={"Content-Type": "application/json"})
        resp.raise_for_status()

    raw = (resp.json()
               .get("candidates", [{}])[0]
               .get("content", {})
               .get("parts", [{}])[0]
               .get("text", "{}"))

    # フェイルセーフ: JSONパース失敗時は最低限の構造を返す
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"summary": raw, "topics": [], "calls": []}

    # キーが欠けていても壊れないよう補完
    result.setdefault("summary", "")
    result.setdefault("topics", [])
    result.setdefault("calls", [])
    return result

# ===== テキストバッファ & 定期Gemini処理 =====
text_buffer: list[dict] = []
buffer_lock = asyncio.Lock()

async def gemini_summarizer():
    while True:
        await asyncio.sleep(BUFFER_SECONDS)

        async with buffer_lock:
            if not text_buffer:
                continue
            entries = list(text_buffer)
            text_buffer.clear()

        log = "\n".join(f"[{e['time']}] {e['text']}" for e in entries)
        period_start = entries[0]["time"]
        period_end   = entries[-1]["time"]

        print(f"[Gemini] 解析開始 ({period_start}〜{period_end}, {len(entries)}件)")
        await broadcast({"type": "ai_thinking", "text": "Geminiが解析中..."})

        try:
            result = await call_gemini(log)
            print(f"[Gemini] 完了: {len(result['calls'])}件の交信を検出")
            await broadcast({
                "type":    "ai_summary",
                "summary": result["summary"],
                "topics":  result["topics"],
                "calls":   result["calls"],
                "from":    period_start,
                "to":      period_end,
                "count":   len(entries),
            })
        except Exception as e:
            print(f"[Gemini] エラー: {e}")
            await broadcast({"type": "error", "text": f"Geminiエラー: {e}"})

# ===== AmiVoice Wrp ストリーミング =====
class AmiVoiceListener(AmiWrpListenerBase):
    """Wrp コールバックを asyncio イベントループへ橋渡しする。"""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def resultUpdated(self, result: str):
        self._on_result(result, final=False)

    def resultFinalized(self, result: str):
        self._on_result(result, final=True)

    def eventNotified(self, event_id: str, event_message: str):
        self._schedule(broadcast({
            "type": "error",
            "text": f"AmiVoiceイベント {event_id}: {event_message}",
        }))

    def utteranceStarted(self, start_time: int):
        pass

    def utteranceEnded(self, end_time: int):
        pass

    def resultCreated(self):
        pass

    def TRACE(self, message: str):
        pass

    def _on_result(self, result: str, final: bool):
        text = _extract_amivoice_text(result)
        if not text:
            return
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[{'確定' if final else '途中'}] {now} {text}")
        self._schedule(broadcast({
            "type": "result", "final": final, "text": text, "time": now,
        }))
        if final:
            self._schedule(_append_text_buffer(now, text))

    def _schedule(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self._loop)


def _extract_amivoice_text(result: str) -> str:
    try:
        return json.loads(result).get("text", "").strip()
    except json.JSONDecodeError:
        return ""


async def _append_text_buffer(time: str, text: str):
    async with buffer_lock:
        text_buffer.append({"time": time, "text": text})


def _amivoice_worker(sync_queue: queue.Queue, loop: asyncio.AbstractEventLoop):
    """Wrp は同期 API のため専用スレッドで実行する。"""
    while True:
        listener = AmiVoiceListener(loop)
        wrp = AmiWrp.construct()
        wrp.setListener(listener)
        wrp.setServerURL(AMIVOICE_SERVER_URL)
        wrp.setCodec(AMIVOICE_CODEC)
        wrp.setGrammarFileNames(AMIVOICE_GRAMMAR)
        wrp.setAuthorization(_require_env("AMIVOICE_APP_KEY"))

        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "status", "text": "AmiVoice接続中..."}), loop
        ).result()

        if not wrp.connect():
            print(f"[AmiVoice] 接続失敗: {wrp.getLastMessage()}")
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "status", "text": "再接続中..."}), loop
            ).result()
            wrp.sleep(3000)
            continue

        if not wrp.feedDataResume():
            print(f"[AmiVoice] 送信開始失敗: {wrp.getLastMessage()}")
            wrp.disconnect()
            wrp.sleep(3000)
            continue

        asyncio.run_coroutine_threadsafe(
            broadcast({"type": "status", "text": "受信中..."}), loop
        ).result()

        disconnected = False
        while True:
            chunk = sync_queue.get()
            if chunk is None:
                disconnected = True
                break

            max_sleep_ms = 50000
            while wrp.getWaitingResults() > 1 and max_sleep_ms > 0:
                wrp.sleep(100)
                max_sleep_ms -= 100

            if not wrp.feedData(chunk, 0, len(chunk)):
                print(f"[AmiVoice] 送信エラー: {wrp.getLastMessage()}")
                disconnected = True
                break
            wrp.sleep(1)

        wrp.disconnect()
        if disconnected:
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "status", "text": "再接続中..."}), loop
            ).result()
            wrp.sleep(3000)


async def _audio_bridge(audio_queue: asyncio.Queue, sync_queue: queue.Queue):
    """asyncio.Queue → queue.Queue へ音声チャンクを転送する。"""
    while True:
        sync_queue.put(await audio_queue.get())


async def amivoice_stream(audio_queue: asyncio.Queue):
    loop = asyncio.get_running_loop()
    sync_queue: queue.Queue = queue.Queue(maxsize=50)
    asyncio.create_task(_audio_bridge(audio_queue, sync_queue))
    threading.Thread(
        target=_amivoice_worker, args=(sync_queue, loop), daemon=True
    ).start()
    await asyncio.Future()  # Wrp スレッドを稼働させ続ける

# ===== rtl_fm =====
class PcmRecorder:
    """rtl_fm の PCM（16bit mono LE）を WAV に書き出す。"""

    def __init__(self, path: Path):
        self.path = path
        self._wav: wave.Wave_write | None = None
        self._bytes = 0

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(int(SAMPLE_RATE_OUT))

    def write(self, chunk: bytes):
        if not chunk or self._wav is None:
            return
        self._wav.writeframes(chunk)
        self._bytes += len(chunk)

    def close(self):
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        if self._bytes == 0:
            print(f"[録音] {self.path} — データなし")
            return
        with wave.open(str(self.path), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        peak = max(abs(s) for s in samples)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        secs = self._bytes / (int(SAMPLE_RATE_OUT) * 2)
        print(f"[録音] {self.path} ({secs:.1f}秒)")
        print(f"[録音] peak={peak} rms={rms:.0f}  ※peakが数百未満なら無音/弱信号の可能性")


def _build_rtl_fm_cmd() -> list[str]:
    cmd = [
        "rtl_fm", "-f", FREQUENCY, "-M", "fm",
        "-s", FM_BANDWIDTH, "-r", SAMPLE_RATE_OUT,
        "-g", GAIN, "-l", "0", "-E", "dc",
    ]
    if RTL_PPM != "0":
        cmd.extend(["-p", RTL_PPM])
    cmd.append("-")
    return cmd


async def _rtl_fm_stderr_reader(proc: asyncio.subprocess.Process):
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        if text:
            print(f"[rtl_fm] {text}")


async def rtl_fm_reader(
    audio_queue: asyncio.Queue | None,
    recorder: PcmRecorder | None = None,
):
    cmd = _build_rtl_fm_cmd()
    print(f"[rtl_fm] 起動: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    asyncio.create_task(_rtl_fm_stderr_reader(proc))
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            if recorder:
                recorder.write(chunk)
            if audio_queue is not None:
                await audio_queue.put(chunk)
    finally:
        proc.terminate()
        if audio_queue is not None:
            await audio_queue.put(None)


async def record_only(path: Path, duration: float):
    """rtl_fm 出力のみを記録し、復調パラメータの確認に使う。"""
    _print_rx_params()
    recorder = PcmRecorder(path)
    recorder.open()
    print(f"[録音] {duration:.0f}秒間記録します…")

    cmd = _build_rtl_fm_cmd()
    print(f"[rtl_fm] 起動: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    asyncio.create_task(_rtl_fm_stderr_reader(proc))

    try:
        end_at = asyncio.get_running_loop().time() + duration
        while asyncio.get_running_loop().time() < end_at:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                print("[rtl_fm] 出力が途切れました")
                break
            recorder.write(chunk)
    finally:
        proc.terminate()
        await proc.wait()
        recorder.close()

# ===== エントリーポイント =====
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RTL-SDR FM受信 → AmiVoice → Gemini → ブラウザ"
    )
    parser.add_argument(
        "--record", metavar="FILE",
        help="受信音声を WAV に同時記録（通常起動時）",
    )
    parser.add_argument(
        "--record-only", metavar="FILE",
        help="rtl_fm 出力のみを記録して終了（APIキー不要）",
    )
    parser.add_argument(
        "--duration", type=float, default=30,
        help="--record-only の録音秒数（既定: 30）",
    )
    return parser.parse_args()


async def main(record_path: Path | None = None):
    _validate_api_keys()
    _print_rx_params()

    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    recorder = None
    if record_path:
        recorder = PcmRecorder(record_path)
        recorder.open()
        print(f"[録音] 同時記録: {record_path}")

    browser_server = await websockets.server.serve(
        browser_handler, "localhost", BROWSER_WS_PORT)
    print(f"[Server] ws://localhost:{BROWSER_WS_PORT}  Gemini: {BUFFER_SECONDS//60}分ごと")
    try:
        await asyncio.gather(
            rtl_fm_reader(audio_queue, recorder),
            amivoice_stream(audio_queue),
            gemini_summarizer(),
        )
    finally:
        if recorder:
            recorder.close()
        browser_server.close()
        await browser_server.wait_closed()


if __name__ == "__main__":
    args = parse_args()
    if args.record_only:
        asyncio.run(record_only(Path(args.record_only), args.duration))
    else:
        record = Path(args.record) if args.record else None
        asyncio.run(main(record))
