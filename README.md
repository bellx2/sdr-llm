# SDR-LLM

RTL-SDR で受信した FM 無線を、AmiVoice Cloud でリアルタイム文字起こし、Gemini で要約・交信ログ抽出し、ブラウザに表示するシステム。

```
rtl_fm (FM復調)
  → server.py (asyncio)
      ├─ AmiVoice Cloud (音声 → テキスト)
      ├─ Gemini API    (要約 + 交信ログ抽出)
      └─ WebSocket     → index.html
```

## 必要なもの

- macOS
- RTL-SDR ドングル (`brew install rtl-sdr`)
- [uv](https://docs.astral.sh/uv/) / Python 3.12+
- AmiVoice Cloud と Gemini の API キー

## セットアップ

```bash
uv sync
cp .env.sample .env
# .env に AMIVOICE_APP_KEY と GEMINI_API_KEY を設定
```

## 実行

```bash
uv run server.py
# ブラウザで http://localhost:8765 を開く
```

周波数は起動引数で指定できる（末尾の `M` は省略可）。

```bash
uv run server.py -f 145.000      # 145.000 MHz
uv run server.py -f 433.10M      # 433.10 MHz
```

## 受信パラメータの確認

API キー不要で rtl_fm の出力だけを WAV に保存できる。

```bash
uv run server.py --record-only test.wav --duration 30
```

録音終了時に `peak` / `rms` が表示される。`peak` が極端に低ければ無音または弱信号。

詳細な設定・アーキテクチャは [CLAUDE.md](CLAUDE.md) を参照。
