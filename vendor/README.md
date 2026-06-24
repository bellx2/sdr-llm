# vendor/

外部ライブラリを同梱するためのディレクトリ。PyPI で配布されていないものをここに置く。

## amivoice-wrp/

AmiVoice 公式 Wrp クライアントライブラリ（Python 版）。

- Origin: https://github.com/advanced-media-inc/amivoice-api-client-library
- License: MIT（[amivoice-wrp/LICENSE](amivoice-wrp/LICENSE) を参照）
- Copyright (c) 2019-2025 Advanced Media, Inc.

同梱理由: PyPI 公開がないため、リポジトリのクローンと `uv sync` だけでセットアップが完了するように同梱している。
