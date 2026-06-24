# CLAUDE.md — RTL-SDR FM受信 + AmiVoice + Gemini

## プロジェクト概要

RTL-SDRドングルでFM無線を受信し、AmiVoice ACP2でリアルタイム音声認識、
Geminiで3分ごとに要約・交信ログ抽出を行い、ブラウザに表示するシステム。

```
rtl_fm (FM復調)
  → server.py (asyncio中継サーバー)
      ├─ AmiVoice Wrp クライアント (音声→テキスト)
      ├─ Gemini API (3分ごとに要約+交信抽出)
      └─ ブラウザ向け WebSocket (ws://localhost:8765)
           → index.html (リアルタイム表示)
```

## ファイル構成

```
server.py       メインサーバー
index.html      ブラウザUI
pyproject.toml  プロジェクト定義・依存関係
uv.lock         依存ロックファイル
.env.sample     環境変数テンプレート
.env            環境変数（git管理外）
vendor/         AmiVoice 公式 Wrp クライアントライブラリ（Python）
CLAUDE.md       このファイル
```

## 環境・依存

- macOS
- [uv](https://docs.astral.sh/uv/)（Pythonパッケージ管理）
- Python 3.12+（`.python-version` で固定）
- rtl-sdr: `brew install rtl-sdr`

### 初回セットアップ

```bash
# 依存関係のインストール（.venv を自動作成）
uv sync

# 環境変数の設定
cp .env.sample .env
# .env を編集して APIキーを設定
```

Pythonパッケージ（`pyproject.toml` で管理）:
- `httpx` — Gemini API 呼び出し
- `websockets` — ブラウザ向け WebSocket
- `python-dotenv` — `.env` 読み込み

AmiVoice 連携は [公式 Wrp クライアントライブラリ](https://github.com/advanced-media-inc/amivoice-api-client-library) を `vendor/amivoice-wrp/` に同梱して使用する（PyPI 非公開のため）。

## APIキーの設定

APIキーは `.env` に設定する（コードへの直書きはしない）。

```bash
cp .env.sample .env
```

| 変数 | 必須 | 説明 |
|------|------|------|
| `AMIVOICE_APP_KEY` | はい | [AmiVoice Cloud Console](https://console.amivoice.com/) で取得 |
| `AMIVOICE_SERVER_URL` | いいえ | Wrp 接続先（省略時: `wss://acp-api.amivoice.com/v1/recognize`） |
| `AMIVOICE_CODEC` | いいえ | 音声コーデック（省略時: `LSB16K`） |
| `AMIVOICE_GRAMMAR` | いいえ | グラマファイル名（省略時: `-a-general`） |
| `GEMINI_API_KEY` | はい | [Google AI Studio](https://aistudio.google.com/) で取得 |
| `GEMINI_MODEL` | いいえ | Geminiモデル名（省略時: `gemini-2.5-flash-lite`） |

未設定の必須変数があると `server.py` 起動時にエラーで終了する。

## 起動

```bash
uv run server.py
# → ブラウザで index.html を開く
```

### 復調パラメータの確認（録音）

```bash
# rtl_fm 出力のみ 30 秒録音（APIキー不要）
uv run server.py --record-only recordings/test.wav

# 録音秒数を指定
uv run server.py --record-only recordings/test.wav --duration 60

# 通常起動しながら同時に WAV 記録
uv run server.py --record recordings/live.wav
```

録音終了時に `peak` / `rms` を表示する。`peak` が数百未満なら無音または弱信号の可能性がある。

`.env` の `FM_BANDWIDTH` / `RTL_GAIN` / `RTL_PPM` を変えて複数録音し、聴き比べると復調設定の調整に使える。

## 主要パラメータ

`server.py` 先頭の定数、または `.env` で変更する。

| 変数 | 場所 | デフォルト | 説明 |
|------|------|-----------|------|
| `FREQUENCY` | `server.py` | `"433.000M"` | 受信周波数 (Hz表記、例: `"433.000M"`) |
| `GAIN` | `.env` `RTL_GAIN` | `"0"` | RTL-SDRゲイン (dB)。`"0"` で自動ゲイン |
| `FM_BANDWIDTH` | `.env` | `"25k"` | NBFM帯域 (`12.5k` / `25k`) |
| `RTL_PPM` | `.env` | `"0"` | 周波数PPM補正 |
| `BUFFER_SECONDS` | `server.py` | `60` | Gemini解析の間隔（秒） |
| `CHUNK_BYTES` | `server.py` | `3200` | AmiVoiceへ送る単位（16kHz 16bit 100ms分） |
| `BROWSER_WS_PORT` | `server.py` | `8765` | ブラウザ向け WebSocket ポート |
| `GEMINI_MODEL` | `.env` | `gemini-2.5-flash-lite` | Geminiモデル名 |

狭帯域FM（アマチュア無線）に切り替える場合は `rtl_fm` の `-s` と `-M` を変更する：

```python
# server.py の rtl_fm_reader 内
cmd = ["rtl_fm", "-f", FREQUENCY, "-M", "fm", "-s", "15k", ...]
```

## アーキテクチャの詳細

### asyncioタスク構成

`main()` で3つのコルーチンを `asyncio.gather()` で並走させる：

| タスク | 役割 |
|--------|------|
| `rtl_fm_reader` | サブプロセスの rtl_fm から PCM を読んで `audio_queue` へ投入 |
| `amivoice_stream` | キューからPCMを取り出してAmiVoice ACP2にストリーミング送信。確定テキストを `text_buffer` へ積む |
| `gemini_summarizer` | `BUFFER_SECONDS` ごとに `text_buffer` をGeminiへ送り、結果をブラウザへブロードキャスト |

### audio_queue

- `maxsize=50`（約5秒分）でバックプレッシャーを制御
- rtl_fmが詰まったときにメモリが無限に膨らまないようにするため

### AmiVoice Wrp クライアント

[公式 Wrp ライブラリ](https://github.com/advanced-media-inc/amivoice-api-client-library) は同期 API のため、専用スレッドで `connect()` → `feedDataResume()` → `feedData()` を実行する。認識結果は `WrpListener` コールバック（`resultUpdated` / `resultFinalized`）で受け取り、`asyncio.run_coroutine_threadsafe` でブラウザへブロードキャストする。

### AmiVoice コーデック指定

`LSB16K` = 16kHz / 16bit / モノラル / リトルエンディアン signed PCM。
rtl_fm の `-r 16000` 出力がそのままこの形式なので再エンコード不要。

### Gemini JSON抽出スキーマ

`responseMimeType: "application/json"` でJSON出力を強制。返却スキーマ：

```json
{
  "summary": "200字以内の要約",
  "topics": ["トピック1", "トピック2"],
  "calls": [
    {
      "type": "CQ" または "個別",
      "from": "発信者（不明はnull）",
      "to":   "受信者（CQ・不明はnull）",
      "time": "HH:MM:SS",
      "note": "補足（なければnull）"
    }
  ]
}
```

交信種別の判定ルール：
- 発話に「CQ」を含む → `"CQ"`、`to` は `null`
- それ以外 → `"個別"`
- 発信者：「こちらは〇〇」の〇〇
- 受信者：「〇〇、こちらは〜」の冒頭〇〇（個別交信のみ）

### ブラウザ向けWebSocketメッセージ型

| `type` | 用途 | 主なフィールド |
|--------|------|---------------|
| `status` | 接続状態の通知 | `text` |
| `result` | AmiVoice認識結果 | `text`, `final`, `time` |
| `ai_thinking` | Gemini処理開始通知 | `text` |
| `ai_summary` | Gemini解析結果 | `summary`, `topics`, `calls`, `from`, `to`, `count` |
| `error` | エラー通知 | `text` |

## よくあるトラブル

**RTL-SDRが認識されない**
```bash
rtl_test   # デバイス検出確認
# "Found 1 device(s)" が出ればOK
# 出ない場合は brew reinstall librtlsdr
```

**AmiVoiceが無音を返す**
- `GAIN` を上げる（例: `"50"`）か `"0"`（自動）を試す
- `CHUNK_BYTES` を `6400`（200ms）に増やして安定性を上げる

**GeminiがJSONを返さない**
- `responseMimeType: "application/json"` が効かないモデルの場合は
  `SYSTEM_PROMPT` の末尾に「必ずJSONのみで返答し、それ以外は出力しないこと」を追記する

**ブラウザに結果が出ない**
- `uv run server.py` のターミナルで `[確定]` ログが出ているか確認
- ブラウザのコンソールで WebSocket 接続エラーがないか確認
- `ws://localhost:8765` のポートが他プロセスと競合していないか確認
  ```bash
  lsof -i :8765
  ```

**環境変数エラーで起動できない**
- `.env` が存在するか確認（`cp .env.sample .env`）
- `AMIVOICE_APP_KEY` と `GEMINI_API_KEY` が空でないか確認
