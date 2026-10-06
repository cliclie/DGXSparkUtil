# DGXSparkUtil

DGX Spark のモニタリングとdocker内のvLLMの切替を行う

## 目的

クライアント(別のWindows PC)からブラウザでアクセスし、API経由で以下を行う仕組みを構築する。

1. **システム状態のモニタリング**(DGX Dashboard に相当)
   - メトリクスをビジュアルで表示(半円ゲージ、詳細は「表示スタイル(ゲージ)」を参照)、値の更新はAPI経由
   - モニタリング項目:
     - CPU負荷
     - CPU温度
     - 統合メモリの使用率
     - GPU負荷
     - GPU温度
     - ストレージの使用率(使用量・空き容量)
     - ストレージの負荷(読込/書込)
     - ネットワークの負荷(下り/上り)
2. **dockerで作動中のvLLMの状態モニタリング**
   - 稼働中のモデルコンテナの状態(稼働/停止・稼働時間・健全性)を表示
   - APIの健全性(`/health`)、サーブ中のモデル名(`/v1/models`)、
     vLLM組み込みメトリクス(`/metrics`: リクエスト数・キュー・KVキャッシュ使用率・スループット等)を表示
      (SGLang は `--enable-metrics` 指定時、llama.cpp は `--metrics` 指定時に `/metrics` を使用、
      TabbyAPI は `/metrics` が無いため docker logs のリクエスト毎統計を解析)
   - 直近ログの表示
3. **モデルの切替**
   - Web UI から稼働モデルを切替(サーバ側で既存の切替スクリプトを呼ぶ)
   - 既存スクリプト参照: `/home/cliclie/llm/compose/switch_models.sh`(参照のみ。調査フェーズでは実行しない)
4. **稼働モデルに与えているパラメータ値の表示・編集**
   - コンテキストサイズ等のパラメータ値を表示
   - 一部項目は編集可能。編集後はコンテナを再作成して反映

- 対象機材: GIGABYTE AI TOP ATOM (NVIDIA GB10 / Grace Blackwell Superchip) および
  WhitebearATOM2 (Intel i9-12900KF + AMD Radeon AI PRO R9700 / ROCm)。
  プラットフォームは `api/config.py` の自動検出で両環境同一コードで動作する(実装メモ 2026-10-03 参照)
- 仕組み: クライアントからブラウザでアクセス → ビジュアル表示 → 値の更新はAPI経由

## 表示スタイル(ゲージ)

監視項目はすべて、DGX Dashboard の「System Memory」と同様の**半円ゲージ(半円アーチ)**で表示する。

共通スタイル:
- ゲージ上部に項目名(タイトル)
- 半円アーチを緑(低) → オレンジ(中) → 赤(高)のゾーンで区分
- ゲージ中央に現在値を大きく表示(例: `101.08 GiB`。現在 `font-size: 16px`)
- 現在値の直下に総量/容量を表示(例: `121.62 GiB total`)
- 2 値の項目(Storage I/O の読込/書込、Network の下り/上り)は、1 つのゲージ内に
  外側(=A)/内側(=B)の 2 つのアーチ(ストーク間隔 1px)で別々に表示し、2 行の値は
  **ゲージの直下**に表示する(合計値ではない。中央配置では縮小後 canvas で内側アーチと
  交差するためゲージ下に配置 — 実装メモ(2026-09-27)・実装メモ(2026-10-05)参照)

項目ごとのレイアウト:

| 項目 | ゲージタイトル | 中央(現在値) | 下部(総量/容量) |
|---|---|---|---|
| CPU負荷 | CPU Load | 使用率 % | コア数(例: `20 cores`) |
| CPU温度 | CPU Temperature | 現在温度(例: `38°C`) | 上限(例: `100°C`) |
| 統合メモリの使用率 | 統合メモリ | 使用率 %(例: `75.2%`) + 2行目に空き量(例: `空き 20.5 GiB`) | 使用/総量(例: `101.08 / 121.62 GiB`) |
| GPU負荷 | GPU Load | 使用率 % | — |
| GPU温度 | GPU Temperature | 現在温度(例: `38°C`) | 上限(例: `100°C`) |
| ストレージの使用率 | Storage | 使用量(例: `663 GiB`) | 容量(例: `3.6 TiB total`) |
| ストレージの負荷 | Storage I/O | 読込/書込速度を2値表示(例: 読込 `120 MB/s` / 書込 `45 MB/s`) | 上限(例: `5 GB/s`) |
| ネットワークの負荷 | Network | 下り/上り速度を2値表示(例: 下り `1200 Mbps` / 上り `300 Mbps`) | 上限=**実測リンク速度**(例: `10 Gbps`、実装メモ 2026-10-06 参照) |

- 色ゾーンの閾値は設定可能(デフォルト: 緑 < 60%、オレンジ 60〜85%、赤 > 85%)
- ストレージの使用率は、下部に空き容量を2行目で表示可能(例: `free 2.8 TiB`)

## 表示レイアウト(4段構成)

ダッシュボード全体は上下 4 段の構成とする。

### 一段目: 現在値ゲージ行

- 各項目のゲージ(半円アーチ)を**横方向に 1 行へ並べて表示**する(左 → 右: CPU Load, CPU Temperature, 統合メモリ, GPU Load, GPU Temperature, GPU Power, GPU Clock, Storage, Storage I/O, Network の順)
- 表示領域(ウィンドウ幅)が狭くなれば、**自然に折り返して次の行へ表示する**(1 行に無理に詰めない)
  - 実装イメージ: CSS の flexbox + `flex-wrap: wrap`、各ゲージに固定幅
    **`.gauge { flex: 0 0 176px }`** を割り当て、幅が足りなくなったら折り返す
    (176px × 7枚 + gap 10px × 6 = 1292px で main 内側 1360px に収まる → **7枚/行**。
    実装メモ(2026-10-05)参照)
- 折り返し時にも各ゲージの幅は揃え、間隔を一定にする
- 各ゲージは「表示スタイル(ゲージ)」に定義した共通スタイル(タイトル・色ゾーン・中央に現在値・直下に総量/容量)をそのまま使用する

### 二段目: 時系列グラフ

一段目の下段に、**各項目の時系列値を折れ線グラフとして表示**する(参考: 添付の gpu load 時系列スクショと同様のスタイル)。

- 横軸: 時間(timestamp)、目盛りは「月 日, 年, 時刻」形式(例: `October 8, 2023, 12:00 AM`)
- 縦軸: 測定値(単位は項目ごとに % / °C / GiB / MB/s など)、軸ラベルに単位を明記(例: `gpu load [%]`)
- グラフタイトルは項目名(例: `gpu load`)
- **各項目の線色を変える**(例: CPU Load=緑, CPU Temperature=紫, 統合メモリ=赤, GPU Load=青 など。色は項目ごとに固定しスクショ風の明るい色調とする)
- **凡例を表示する**(各線の先頭に丸マーカー + 項目名を横に並べる)
- **測定値の点と点はスプライン補間で滑らかに表示する**(直線ではなくカーブで結ぶ)
- 描画ライブラリ: **Chart.js**(MIT ライセンス)の line chart を使用
  - スプライン補間は `tension` プロパティ、凡例・線色分け・軸ラベルは内蔵機能で実現する
  - クライアント PC が LAN 内・オフラインでも動作するため、CDN は使わず
    単一ファイルビルド(`chart.umd.js`)を `front/` に配置しローカル参照する
  - 2 秒毎ポーリングで新データ点を追記し `chart.update()` で差分描画する
- 表示は各項目ごとに独立したグラフパネルとし、一段目と同様に横並び・折り返しで配置する(または 1 グラフに複数項目を重ねる場合は縦軸スケールが混ざるため、項目単位のパネル分割を基本とする)
- 表示データは監視開始からのセッション内データでよい(永続履歴は不要)。保持はクライアント側(例: 直近 N 点のリングバッファ)
- 更新は一段目と同様に 2 秒毎ポーリングで、新データ点を追記して描画し直す

### 三段目: vLLM 状態モニタリング

docker で作動中の vLLM の状態を常時表示するセクション(「目的」2. / 「vLLM関連機能の設計」を参照)。

- 表示内容:
  - 稼働中のモデルコンテナの状態(稼働/停止・稼働時間・再起動回数)
  - API 健全性(`/health`)、サーブ中のモデル名(`/v1/models`)
  - vLLM 組み込みメトリクス(`/metrics`: リクエスト数・キュー・KV キャッシュ使用率・スループット等)
    (SGLang: `--enable-metrics` 指定時、llama.cpp: `--metrics` 指定時に `/metrics` を使用、
    TabbyAPI は `/metrics` が無いため docker logs のリクエスト毎統計を解析、実装メモ 2026-09-14 参照)
  - 直近ログの表示
- セクションヘッダーに「モデル切替・ログ・パラメータ編集」ボタンを配置
  - ボタン押下で**ポップアップ(モーダル)表示**する(常設のフォーム欄は持たない)
  - ポップアップ内には以下を収める:
    - **モデル切替**: 対象モデル(profile)の選択 + 「切替」ボタン(サーバ側で `switch_models.sh <profile>` を実行)
    - **実行ログ**: 稼働モデルの docker logs を三段構成(タイトル/ログ表示/閉じる)のモーダルで表示。
      検索(行フィルタ)・再読み込み・`.log` ダウンロードに対応
    - **パラメータ表示・編集**: 稼働モデルのパラメータ値の一覧表示、編集可能項目の編集 + 「保存」ボタン(編集後はコンテナ再作成して反映)
  - 切替・保存は時間がかかる処理のため、ポップアップ内に進捗/状態を返し、完了後に閉じる(結果は三段目の状態表示に反映)
- 三段目の状態表示は 2 秒毎ポーリングで更新する(モデル切替中は「切替中」状態を明示)

### 四段目: RAG (sociax-rag) 起動/停止

RAG 環境(embedding vLLM + Qdrant)の状態を常時表示し、起動/停止を操作できるセクション。
対象ディレクトリはプラットフォーム別(dgx-spark `/home/cliclie/llm/compose/sociax-rag/`、
atom2 `/home/cliclie/RAG/compose/`、`api/config.py` 参照)。atom2 では VRAM のため
LLM と RAG embedding が排他(実装メモ 2026-10-03 参照)。

- 表示内容:
  - Embedding (8010) / Qdrant (6333/6334) それぞれの状態
    (稼働中 (API応答) / 起動中… (API応答なし) / 停止中)
  - 稼働時間
- セクションヘッダーに「起動」「停止」ボタンを配置
  - 押下で確認モーダル表示、実行後はジョブログをモーダル内に自動更新し、
    完了時にモーダルを自動閉じる(三段目の停止モーダルと同一パターン)
  - 起動: 完全に停止中かつジョブ実行中でないときのみ有効
  - 停止: 稼働中かつジョブ実行中でないときのみ有効
- 状態表示は 2 秒毎ポーリングで更新する(起動直後は embedding のモデルロード完了まで
  「起動中… (API応答なし)」を表示)

## 調査結果(2026-08-22 実機確認)

**結論: 全項目のモニタリングは可能。**
(read-only なコマンドによる検証。ファイル修正・docker の起動停止は行っていない)

### 機材の特殊性

GPU は **NVIDIA GB10** であり、CPU(Grace)とGPU(Blackwell)が **128GB の統合メモリを共有**する構成。
このため `nvidia-smi` の `memory.total / memory.used` は `[N/A]` となり、独立した VRAM 使用量は取得できない。
→ 統合メモリ(RAM/VRAM)は「統合メモリ使用率」として 1 本で表示するのが実態に忠実。
(vLLM は `--gpu-memory-utilization 0.7 --kv-cache 24G` 等でこのプールを大半占有しているため、
使用率が高めに出るのが正常。必要に応じて vLLM プロセスの RSS を「アプリ占有」として分離表示も可能)

### 各項目の取得方法と実測値

| 項目 | 取得経路 | 実測値(確認時) | 判定 |
|---|---|---|---|
| CPU負荷 | `/proc/stat` の idle 差分(または psutil) | loadavg 0.31 | OK |
| CPU温度 | `/sys/class/thermal/thermal_zone0-6` (acpitz) | 36.8〜39.8℃ | OK |
| 統合メモリの使用率 | `/proc/meminfo` (MemTotal − MemAvailable) | 121Gi 中 約102Gi 使用 | 統合メモリとして表示 |
| GPU負荷 | `nvidia-smi --query-gpu=utilization.gpu` | 0% | OK |
| GPU温度 | `nvidia-smi --query-gpu=temperature.gpu` | 37〜38℃ | OK |
| GPU電力/クロック(追加) | `nvidia-smi power.draw / clocks.sm` | 11.4W / 2398MHz | OK |
| ストレージ使用率 | `shutil.disk_usage` (nvme0n1p2) | 3.6T / 使用 663G(20%) / 空き 2.8T | OK |
| ストレージ負荷 | `/proc/diskstats` 差分 or `iostat` | kB_read/s, kB_wrtn/s, IOPS, 応答時間 | OK |

### 環境確認事項

- OS: Ubuntu 24.04 (aarch64, 20 CPU, 128GB 統合メモリ, zram swap)
- ホスト IP: `192.168.0.110` (enP7s7)
- 監視系サービスは未導入(Prometheus/Grafana/DCGM なし)。`psutil 5.9.8` はホストに既導入、`pynvml` は未導入(nvidia-smi の subprocess 呼び出しで代替可)
- `nvidia-smi` 単発実行は約 24ms と軽量 → 2 秒毎ポーリングで十分
- 監視UI用の候補ポート 8080 / 8090 / 9090 / 3000 / 9000 は全て空き
- 既存サービスポート: vLLM 8000〜8007(確認時は 8006 の qwen38bf16 が稼働中)、sociax-rag 8010 / 6333 / 6334
- ファイアウォール無効(ufw 無効・iptables 空) → 同 LAN のクライアント PC から直接アクセス可能

## 既存資産(参照のみ・実行・変更しない)

- `/home/cliclie/llm/compose/switch_models.sh [profile]`
  - 稼働モデルを切替する既存スクリプト
  - 動作: 対象が既に起動中で健全なら何もしない → 既存のモデルコンテナを全停止 →
    `docker compose --profile <profile> up -d --force-recreate <service>` →
    `/health` 応答を待機(タイムアウト 1800 秒、起動ログをライブ表示) → API URL とモデル名を報告
  - プロファイル: `nemotron | qwen38 | qwen38bf16 | qwen38nvfp4 | qwen38sglang | qwen38sglangeagle | qwen38sglangdflash | qwen38sglangdspark | qwen38flashnextexl3 | qwen38flashnextexl3_3p05 | qwen38flashnextexl3_4p05 | qwen38swift | qwen38swift_dflash | muse | mimo9b | glm53flash | qwen38flashnext_nvfp4`
  - 同時に稼働できるモデルは 1 つ(切替時は現モデルを停止)
- `/home/cliclie/llm/compose/docker-compose.yml`
  - モデルサービス 17 種(vLLM 7 + SGLang 6 + llama.cpp 1 + TabbyAPI 3)と起動パラメータの定義
- `/home/cliclie/llm/compose/sociax-rag/collect-dgx-info.sh`
  - 既存の read-only 収集スクリプト(hostname / docker / GPU / ポート / 稼働コンテナ / ストレージ)

### モデル一覧と主要パラメータ(docker-compose.yml より)

| プロファイル | コンテナ | ポート | ランタイム | コンテキストサイズ | GPUメモリ使用率 | KVキャッシュ |
|---|---|---|---|---|---|---|
| nemotron | vllm-nemotron120b | 8000 | vLLM 26.08 | 262144 | 0.80 | 4G (fp8_e4m3) |
| muse | llama-muse-glimmer | 8004 | llama.cpp | 131072 | - | - |
| qwen38 | vllm-qwen38-27b | 8005 | vLLM 26.08 | 262144 | 0.70 | 16G (fp8_e4m3) |
| qwen38bf16 | vllm-qwen38-27b-bf16 | 8006 | vLLM 26.08 | 262144 | 0.70 | 24G (fp8_e4m3) |
| qwen38nvfp4 | vllm-qwen38-27b-nvfp4 | 8007 | vLLM 26.08 | 262144 | 0.70 | 16G (fp8_e4m3) |
| qwen38sglang | sglang-qwen38-27b-nvfp4 | 8008 | SGLang | 262144 (ネイティブ) | 0.65 | fp8_e4m3 |
| qwen38sglangeagle | sglang-qwen38-27b-nvfp4-eagle | 8008 | SGLang | 262144 (ネイティブ) | 0.65 | fp8_e4m3 |
| qwen38sglangdflash | sglang-qwen38-27b-nvfp4-dflash | 8008 | SGLang | 1000000 (YaRN) | 0.65 | fp8_e4m3 |
| qwen38sglangdspark | sglang-qwen38-27b-nvfp4-dspark | 8008 | SGLang | 1000000 (YaRN) | 0.65 | fp8_e4m3 |
| qwen38flashnextexl3 | tabbyapi-flashnext | 8009 | TabbyAPI (ExLlamaV3) | 1048576 (YaRN) | - | Q8 |
| qwen38flashnextexl3_3p05 | tabbyapi-flashnext-3p05 | 8011 | TabbyAPI (ExLlamaV3) | 1048576 (YaRN) | - | Q8 |
| qwen38flashnextexl3_4p05 | tabbyapi-flashnext-4p05 | 8016 | TabbyAPI (ExLlamaV3) | 1048576 (YaRN) | - | Q8 |
| qwen38swift | vllm-swift-qwen38-27b | 8012 | vLLM 26.08 | 262144 | 0.70 | 24G (BF16) |
| qwen38swift_dflash | sglang-swift-qwen38-27b | 8012 | SGLang | 262144 | 0.65 | fp8_e4m3 |
| mimo9b | vllm-mimo-9b | 8014 | vLLM 26.08 | 1010000 (YaRN) | 0.70 | 16G (fp8_e4m3) |
| glm53flash | sglang-glm53-flash | 8013 | SGLang (GLM-5.3-Flash NVFP4) | 262144 | 0.65 | bfloat16 |
| qwen38flashnext_nvfp4 | vllm-qwen38-flashnext-nvfp4 | 8015 | vLLM (qwen38-flash-dgx:v0.30-tp1) | 262144 | 0.72 | NVFP4 |

- コンテキストサイズ: vLLM は `--max-model-len`、SGLang は `--context-length`
  (sglang dflash / dspark と TabbyAPI の qwen38flashnextexl3 系は YaRN で 1M 拡張、
  mimo9b は YaRN で 1,010,000)、llama.cpp (muse) は `--ctx-size`
- 共通: vLLM は `--max-num-seqs 1`・`--enable-chunked-prefill`、qwen38 系は MTP 推測デコード
  (`--speculative-config`)。SGLang は `--mem-fraction-static 0.65`・`--enable-metrics`、
  muse は `--temp / --top-p / --top-k`

## 技術基盤(実装アーキテクチャ)

```
[クライアント Windows PC]
      │ ブラウザ http://192.168.0.110:8080
      ▼
┌──────────────────────────────────────────────────────┐
│  AI TOP ATOM (192.168.0.110)                          │
│                                                       │
│  ① 収集 + API  (api/ : Python + FastAPI)  │
│     GET  /api/platform      → プラットフォーム情報(表示切替) │
│     GET  /api/metrics        → ホストメトリクス JSON  │
│     GET  /api/history        → 直近30分履歴(バックフィル) │
│     GET  /api/vllm/status    → コンテナ状態/健全性/   │
│                                モデル名/metrics       │
│     GET  /api/vllm/params    → 現在のパラメータ値     │
│     GET  /api/vllm/cline     → Cline設定値(稼働モデル) │
│     GET  /api/vllm/log       → 実行ログ(docker logs)  │
│     GET  /api/vllm/job       → 切替/編集ジョブ進捗    │
│     POST /api/vllm/switch    → モデル切替             │
│     POST /api/vllm/stop      → モデル停止             │
│     POST /api/vllm/params    → パラメータ編集+再作成  │
│     GET  /api/rag/status     → RAG状態(embedding/Qdrant) │
│     POST /api/rag/start|stop → RAG 起動/停止(ジョブ)  │
│     GET  /api/rag/job        → RAG ジョブ進捗         │
│     データ源: /proc/stat, /proc/meminfo,              │
│      /sys/class/thermal/*, /proc/diskstats,           │
│      nvidia-smi(subprocess) / amdgpu sysfs,           │
│      docker CLI(ps/inspect/logs), vLLM /health,       │
│      /v1/models, /metrics, /proc/net/dev (差分),      │
│      /sys/class/net/<if>/speed (NIC 実リンク速度)     │
│  ② 表示: front/ (単一 index.html + JS)          │
│     4段レイアウト(表示レイアウト(4段構成)参照)   │
│     一段目: 現在値ゲージ行 / 二段目: 時系列グラフ  │
│     三段目: vLLM状態 + 「モデル切替・パラメータ   │
│      編集」ボタン(押下でポップアップ表示)          │
│     四段目: RAG (sociax-rag) 状態 + 起動/停止      │
│     fetch を 2秒毎ポーリングしてビジュアル表示     │
│     (既存 vLLM/Qdrant に依存しない独立ポート)      │
└──────────────────────────────────────────────────────┘
```

### フォルダ構成

- `/home/cliclie/DGXSparkUtil/front/` — ブラウザから参照する表示側(静的ファイル: 単一 index.html + JS、時系列グラフ用の Chart.js 単一ファイル `chart.umd.js`)
- `/home/cliclie/DGXSparkUtil/api/` — モニタリング結果を返却するAPI群(Python + FastAPI)

### vLLM関連機能の設計

- UI 配置: 三段目(vLLM 状態モニタリング)に状態を常時表示。「モデル切替」・「パラメータ表示・編集」は
  三段目の「モデル切替・パラメータ編集」ボタンから**ポップアップ(モーダル)表示**する(詳細は「表示レイアウト(4段構成)」参照)

**状態モニタリング**

| 情報 | 取得源 |
|---|---|
| コンテナ稼働/停止・稼働時間・再起動回数 | `docker ps` / `docker inspect` |
| API健全性 | `GET http://<host>:<port>/health` |
| サーブ中モデル名 | `GET /v1/models` |
| リクエスト数・キュー・KVキャッシュ使用率・スループット等 | `GET /metrics` (vLLM 組み込み Prometheus 形式) |
| 同上(TabbyAPI) | `docker logs` (リクエスト毎統計の解析、実装メモ 2026-09-14 参照) |
| 同上(llama.cpp) | `GET /metrics` (`--metrics` 指定時、Prometheus 形式) + `GET /slots` (KVキャッシュ使用率) |
| 直近ログ | `docker logs --tail` |
| ホストへの影響(統合メモリ・GPU負荷) | ホストメトリクス(調査結果参照) |

- llama.cpp は `--metrics` 指定時に `/metrics` (実行/待機・スループット) + `/slots` (KVキャッシュ使用率) を利用。
  `--metrics` 未有効時は `/slots` のみで基本状態を取得。E2E/TTFT は v0.5.0 では未公開(実装メモ 2026-10-04 参照)
- TabbyAPI は `/metrics` が無いが、docker logs のリクエスト毎統計から実行/待機・KVCache(推定)・
  E2E・TTFT・入力/出力スループットを取得する(実装メモ 2026-09-14 参照)
- **E2E・TTFT・入力/出力スループットの前回値保持はフロント側で実施**: `api/vllm.py` は算出値(0/None を含む)をそのまま返す。
  `front/index.html` の `pickMetric()` が直近の非ゼロ値を保持し、API が 0/None を返した間は前回値を維持表示する(実装メモ 2026-10-06)。
  稼働モデル消失・モデル切替(profile 変化)で保持値をリセット。スループットの白/灰は「API が非ゼロの新しい計測値を返した」側を白とする相対判定

**モデル切替**
- 三段目のポップアップ内の「切替」ボタン → サーバ側で `switch_models.sh <profile>` を実行
- 切替時は現在稼働中のモデルを停止し、新モデルをロード(時間がかかる。スクリプトが所要時間を報告)
- 同時稼働は 1 モデル(既存スクリプトの挙動)
- ポップアップ内のジョブログ(`log_tail`)は実行中**最新フレーム(ヘッダー1行+直近4ログ行)のみ**を表示する。
  `switch_models.sh` の `display_frame()` は端末上書き表示のためヘッダー+ログ4行を ANSI カーソル移動付きで
  毎秒出力し、API 側は stdout をファイルにリダイレクトするため、`api/vllm.py` の `job_status()` が
  最後の `=== ... ===` ヘッダー行以降の1フレーム分のみを返す(実装メモ 2026-08-30)

**パラメータ表示・編集**
- 表示: 稼働コンテナの起動引数(`docker inspect` の Config.Cmd/Args)または docker-compose.yml の定義を解析
- 編集対象の例: コンテキストサイズ(`--max-model-len` / `--ctx-size`)、`--max-num-seqs`、
  `--max-num-batched-tokens`、`--gpu-memory-utilization`、`--kv-cache-memory-bytes`、
  muse の `--temp / --top-p / --top-k`
- 注意: vLLM / llama.cpp のパラメータは起動時に確定し、実行中は変更できない
  → 編集後はコンテナ再作成が必要(切替スクリプトの `--force-recreate` フローを流用)
- 編集は対象サービスの command を書き換えてから再作成する

**モデル実行ログ**
- 三段目の「ログ」ボタン → `GET /api/vllm/log?profile=<profile>` で `docker logs --tail 500` を取得
- 三段構成モーダル: 一段目は「<モデル名>の実行ログ」タイトル + 検索/再読み込み/ダウンロード、
  二段目はログ表示(縦スクロール・横折り返し・初期表示は末尾)、三段目は「閉じる」
- 検索: キーワードでの行フィルタ / ダウンロード: `<profile>.log` として保存

**Cline設定値**
- 三段目の「Cline設定値」ボタン → `GET /api/vllm/cline` で Cline に設定すべきモデル情報を表示
- モーダル表示: API Configuration(API Provider / Base URL / API Key / ModelID) +
  MODEL CONFIGURATION(Supports Images / Context Windows Size / Max Output Tokens /
  Temperature / Reasoning Effort)
- 各値をコピー可能(個別コピー + 「すべてコピー」)。推奨/最大値(Context Windows Size /
  Max Output Tokens)は数値を個別コピーでき、Reasoning Effort も各選択肢を個別コピーできる
- データ源: 動的取得(モデル名・ポート・hostname・コンテキスト・温度・画像対応) +
  モデル別静的推奨値テーブル(`api/vllm.py` の `CLINE_MODEL_TABLE`)

### RAG関連機能の設計

- UI 配置: 四段目(RAG (sociax-rag) 起動/停止)に状態を常時表示。「起動」「停止」は
  確認モーダル表示(詳細は「表示レイアウト(4段構成)」参照)
- 対象: `/home/cliclie/llm/compose/sociax-rag/` の Docker Compose プロジェクト
  (embedding vLLM: Qwen3-Embedding-0.6B / ポート 8010、Qdrant v1.18.2 / ポート 6333・6334)
- 目的: WhitebearATOM2 の RAG 環境との共存。本機の RAG を停止して
  GPU メモリ(embedding は `--gpu-memory-utilization 0.10`)等を解放する

**状態モニタリング**

| 情報 | 取得源 |
|---|---|
| コンテナ稼働/停止・稼働時間・再起動回数 | `docker compose ps` / `docker inspect` |
| Embedding API健全性 | `GET http://localhost:8010/v1/models` |
| Qdrant API健全性 | `GET http://localhost:6333/` |

- ポートは `.env` (EMBEDDING_PORT / QDRANT_REST_PORT) から動的取得
- 起動直後はコンテナ稼働中だが embedding のモデルロード完了まで API 未応答
  (healthcheck `start_period: 180s`) → 「起動中… (API応答なし)」で表示

**起動/停止**

- 起動: `docker compose --env-file .env -f compose.yaml up -d` (バックグラウンドジョブ)
  - コマンド自体は数秒で完了。モデルロードはコンテナ内でバックグラウンド実行
  - 既に稼働中の場合は何もしない(冪等)
  - 排他環境 (atom2 / VRAM 32GB) では `switch_models.sh rag` を経由し、稼働中 LLM を停止してから
    embedding を起動する(スクリプトが API 応答まで待機)→ 完了後 `up -d` で Qdrant を補完起動
- 停止: `docker compose --env-file .env -f compose.yaml stop` (バックグラウンドジョブ)
  - Qdrant のデータは volume (`QDRANT_STORAGE_DIR`) に永続化済みなので安全
  - `docker stop` は `restart: unless-stopped` を無効化するため、停止後自動再起動しない
  - `down` (コンテナ削除) は使わない
- ジョブ状態は `api/vllm.py` のジョブと独立(`api/rag.py` 独自) →
  モデル切替と RAG 起動/停止の並行実行を許可。RAG 起動/停止の同時実行は拒否(409)
- ジョブログ(`log_tail`)はモデル切替と同一の最新フレーム表示(ヘッダー1行+直近4ログ行)。
  `api/rag.py` の `job_status()` が `switch_models.sh rag` の上書き表示フレームから
  最後の `=== ... ===` ヘッダー行以降の1フレーム分のみを返す(実装メモ 2026-10-06)
- API: `GET /api/rag/status` / `POST /api/rag/start` / `POST /api/rag/stop` / `GET /api/rag/job`

### 実装方式の選択肢

| 方式 | 構成 | 評価 |
|---|---|---|
| **A(推奨)** | ホスト直 Python+FastAPI を常駐 | 軽量・sysfs 直接アクセスで確実・既存 docker に影響なし。今回の要件に最適 |
| B | 独立 docker-compose | 整理されるが GB10 の GPU アクセス+sysfs バインドが複雑化 |
| C | Prometheus+node_exporter+Grafana | 履歴グラフは強いが今回の「軽量なブラウザ表示」には過剰 |

方式 A の場合、API群は `api/` に、表示(単一HTML)は `front/` に配置し、
既存ファイルは変更せず、別ポート(8080)で独立起動する。

### 注意点

- LAN 外に公開する場合のみ Basic 認証/リバースプロキシを追加
- vLLM のモデル API ポート(8000〜8007)とは別に、監視UIは独立ポート(例: 8080)を使用
- 調査フェーズでは、本 README 以外のファイル変更・docker の起動停止・切替スクリプトの実行は行っていない

## 実行

### systemd サービス（本番・ブート時自動起動）

初回導入（1回だけ sudo 実行）:

```bash
sudo bash /home/cliclie/DGXSparkUtil/api/install_service.sh
```

導入後の管理:

```bash
sudo systemctl status dgx-spark-api    # 状態確認
sudo systemctl restart dgx-spark-api  # 再起動（コード変更後）
sudo systemctl stop dgx-spark-api     # 停止
sudo systemctl disable --now dgx-spark-api  # 自動起動解除＋停止
tail -f /home/cliclie/DGXSparkUtil/api/server.log  # ログ確認
```

- ユニットファイル: `/home/cliclie/DGXSparkUtil/api/dgx-spark-api.service`
  (テンプレート: `__API_DIR__` / `__USER__` プレースホルダを `install_service.sh` が
  sed で置換して `/etc/systemd/system/` にインストール)
- 電源投入時に自動起動（`WantedBy=multi-user.target`）、クラッシュ時は自動再起動（`Restart=always`）
- venv が存在しない場合は `ExecStartPre` で自動作成する
- ログ: `/home/cliclie/DGXSparkUtil/api/server.log`（追記モード）

### 手動起動（開発・デバッグ用）

```bash
cd /home/cliclie/DGXSparkUtil/api
./run.sh          # フォアグラウンド起動
./run.sh -d       # バックグラウンド起動 (ログ: api/server.log)
```

- 初回実行時に venv を自動作成し依存(fastapi, uvicorn)を導入する
- アクセス: 同 LAN 内クライアントから `http://192.168.0.110:8080` (ローカルは `http://localhost:8080`)
- ポート変更: `PORT=8090 ./run.sh`

## 実装メモ(2026-08-24)

- 本 README の設計どおり `api/` (FastAPI) + `front/` (単一 index.html + Chart.js) で実装
- vLLM 26.07 の `/metrics` は全系列がラベル付き(`engine=`, `model_name=`)のため、ラベルを除去して系列名で集約してパースする
- vLLM の `/health` は本文が空 → 健全性は HTTP ステータス(200 系)で判定
- e2e レイテンシは `vllm:request_inference_time_seconds` の sum/count 平均、KV キャッシュは `vllm:kv_cache_usage_perc`
- モデル切替・パラメータ編集はバックグラウンドジョブとして実行し、`GET /api/vllm/job` で進捗(ログ末尾)を取得。同時実行は 1 件
- パラメータ編集は `/home/cliclie/llm/compose/docker-compose.yml` の対象サービス command の値部分を書き換えた後 `docker compose up -d --force-recreate`
- 動作検証時は稼働中の qwen38bf16 への影響を避けるため、切替・コンテナ再作成は実行していない(読み取り系 API のみ検証済み)
- 一段目は半円ゲージ 10 種(Canvas 自前描画)。外側細リングにゾーン帯(緑 <60% / 橙 60〜85% / 赤 >85%)、内側に値アーチ、中央に現在値を表示。IOPS は廃止しストレージ負荷を読込/書込 MB/s の 2 ゲージに分割(上限 5 GB/s)
- ゲージの 100% 基準: GPU 電力は 140 W(GB10 TDP、`nvidia-smi` の power.limit が [N/A] のため固定値)、GPU クロックは実測の `clocks.max.sm` = 3003 MHz。調整は `index.html` の `GAUGE_DEFS[].max` と `NORM` のみ
- 二段目は全 9 系列を 0〜100% に換算して 1 枚の横長統合グラフに集約(CPU 温度=100°C / GPU 電力=140 W / ストレージ I/O=5 GB/s で割って倍率 100)。横軸は「直近30分 / 直近3時間 / すべて」で切替可能
- 時系列バッファは `MAX_POINTS = 4500`(2 秒間隔×3 時間)のリングバッファ。範囲切替は `ts` 基準で配列先頭を切る方式
- `GET /api/metrics` に `cpu_cores`(`os.cpu_count()`)を追加
- タブの favicon は `front/meter-dgx.ico`(`/static/` マウント経由で配信)

## 実装メモ(2026-08-26)

- API サーバを systemd サービス(`dgx-spark-api.service`)として本番運用に移行。電源投入時の自動起動とクラッシュ時自動再起動(`Restart=always`)を実現した
- 初回導入は `sudo bash api/install_service.sh` でユニットファイル設置・enable・起動を一元実行。以降は `sudo systemctl restart dgx-spark-api` でコード変更を反映
- サービスは `User=cliclie`・`WorkingDirectory=api/`・venv 内 python で uvicorn を起動し、8080 へバインド。起動前(`ExecStartPre`)に venv が無い場合は自動作成
- 既存の vLLM(8010)・SGLang(8008, ローカル LLM)とはポートが異なるため衝突しない

## 実装メモ(2026-08-27)

- モデル切替・モニタリングのプロファイル対応表をハードコード(`PROFILES` 辞書)から廃止し、
  `api/vllm.py` の `_load_profiles()` が **docker-compose.yml を動的にパース**して構築するようにした(モデル追加に自動追従)
- パース対象は各サービスブロックの `profiles:` / `container_name:` / `ports:`(ホスト側ポート)のみで、
  **両方を持つサービスのみに限定**。regex 解析のため新規依存ゼロ(PyYAML 不使用・`<<: *vllm-common` の YAML アンカーにも依存しない)。
  ファイルの mtime が変わったときのみ再パースするキャッシュ付き(2 秒ポーリングでも低コスト)
- これにより compose に追加済みの **SGLang 4 種**(qwen38sglang / eagle / dflash / dspark、ポート 8008 共有)も
  モデル一覧に表示され切替可能になった。修正前は SGLang が稼働中でも `active` が検出されず三段目が空表示になる不具合があった(検証で確認)
- トークンスループットは `/metrics` にトークンカウンタ(またはゲージ)が存在する場合のみ算出する
  (vLLM: カウンター差分、SGLang: `--enable-metrics` 時カウンター差分、llama.cpp: `--metrics` 時ゲージ直接値)。
  取得できない項目は null → フロントで "-" 表示
- **制約**: `switch_models.sh` は case 文で profile をハードコードしたまま(今回は変更しない方針)。
  compose に新規モデルを追加した場合、モニタリング・UI 表示は自動追従するが、**切替を実行するには switch_models.sh 側にも
  該当 profile の case を手動追加する必要がある**(未定義の場合ジョブログに usage エラーが出る)
- 検証: `_load_profiles()` で 12 profile(旧 8 + SGLang 4)の抽出確認、`GET /api/vllm/status` で全コンテナ表示確認、
  無効 profile への切替要求は既知プロファイル一覧付きエラーで拒否されることを確認。モデル切替・コンテナ再作成は実行していない

- 二段目の時系列グラフに **GPU クロック** を追加(10 系列目)。3003 MHz(HW 上限)を 100%、0 MHz を 0% とする
  `NORM.gpuClock = 3003` で換算。線色はグレー `#8b949e`(一段目の GPU クロックゲージのラベル左マーカーと同色)
- GPU クロックゲージのゾーン閾値を運用実測値に合わせて変更: **緑 ≤2520 MHz / 黄 ≤2700 MHz / 赤 >2700 MHz**。
  ゲージ共通の `ZONE_STOPS` に加え、ゲージ定義に `zoneStops` を上書き可能にし(`g-gpu-clock` のみ `[2520/3003, 2700/3003]`)、
  他ゲージは従来どおり 60/85% のまま。API は `gpu_clock_mhz` を既に返していたためフロント(`front/index.html`)のみ変更(再起動不要)
- **SGLang メトリクス対応**: SGLang はデフォルトで `/metrics` を公開しない(`enable_metrics` デフォルト False、server_args.py で確認)ため、
  docker-compose.yml の SGLang 4 サービス全てに `--enable-metrics` を追加した。**コンテナ再作成後に有効化される**(性能への影響は CPU 側の
  Prometheus 集計のみで無視できるレベル)。有効化後は `api/vllm.py` の `_sglang_metrics()` が
  `sglang:num_running_reqs` / `num_queue_reqs` / `token_usage`(0-1 比率)×100 / `e2e_request_latency_seconds`・`time_to_first_token_seconds`
  (sum/count 平均) / トークンカウンタ差分を vLLM と同じ構造にマッピングし、三段目の全項目が取得可能になる。
  系列名はイメージ内の `sglang/srt/observability/metrics_collector.py` を直接確認して検証済み
- **`/get_load` フォールバック**: `/metrics` が無い場合(再作成前の SGLang 等)は `_sglang_get_load()` が
  SGLang の `/get_load` から実行中・待機リクエスト数を取得(dp_rank 毎のリストを合計)。これもない場合は従来どおり `metrics: null` → "-"。
  llama.cpp は `_is_llamacpp()` により `/v1/models` の `owned_by` で検出し、`/slots` から基本状態を取得(実装メモ 2026-10-04 参照)
- 注意: 稼働中の SGLang コンテナは再作成まで `--enable-metrics` なしのままなので、**再作成までは三段目が実行中/待機リクエストのみ表示**になる
- **SGLang スループットが常に 0.0 tok/s になる不具合の修正**: `sglang:prompt_tokens_total` /
  `generation_tokens_total` はラベル `is_streaming=true/false` で2系列に分かれる(実機 `/metrics` で確認)が、
  `_parse_vllm_metrics()` が同名系列を初出のみ採用していたため差分計算が false 側(非ストリーミング=稀)だけを使い常に 0 になっていた。
  **カウンタ(名前の末尾が `_total`)のみラベル違いの系列を合計**し、ゲージは従来どおり初出採用に変更した(vLLM の単一エンジン構成では挙動不変)。
  カウンタはリクエスト**完了時**にのみ増加するため(`observe_one_finished_request` 参照)、実行中の1件のみで長期生成中は 0 になるのは仕様通り。
  検証: ストリーミング短リクエスト3件を流し `GET /api/vllm/status` をポーリングしたところ約 290 tok/s を正しく返却(修正前は常に 0.0)
- **スループットの前回値保持**: トークンカウンタはリクエスト完了時のみ増加するため、完了が無い間差分が 0 になり表示が 0.0 に落ちる問題に対し、
  `api/vllm.py` の `_apply_tps()` が**直近の非ゼロ tok/s をモジュール変数に保持**し、差分が 0/None の間は前回値を返すようにした(vLLM・SGLang 両パス共通)。
  メトリクス無効のエンジンに切替えたときは `_reset_token_state()` で保持値も初期化し、旧モデルの値が残らないようにしている
- **線グラフの30分バックフィル**: ページリロードで時系列グラフがクリアされる問題に対し、`api/main.py` がサーバー側で5秒間隔・最大30分(360点)の
  ホストメトリクス履歴を保持し `GET /api/history` を公開。フロントは初回ロード時にこれを取得してリングバッファにシードするため、
  モデル稼働中ならリロード直後に最大30分まで遡ったグラフが表示される(ブラウザ未開の間もサーバー側で蓄積される)

## 実装メモ(2026-08-28)

- モニターの数値を等幅数字(tabular figures)化: `body` に `font-variant-numeric: tabular-nums` を適用。
  現在のフォントスタック(Segoe UI / Hiragino Sans / Noto Sans JP)はすべて tabular figures に対応しており、
  全箇所(ゲージ・vLLM 指標・時計・テーブル)の数値が更新時に横ブレしなくなった。
  Chart.js のグラフ目盛りは canvas 描画のためこの CSS の対象外
- vLLM/モデル行の値を項目ごとに揃えた: 各値要素(`#v-*`)に `ch` 単位の `min-width`(表示サイズ 15px での半角「0」幅が基準)を指定し、
  揃えも項目ごとに設定(稼働モデル=左寄せ fit-content・最小 10ch / ポート・稼働時間=中央寄せ / その他=右寄せ)。
  ラベルが値ボックスより広い項目でも値は常に項目の右端に揃う
- vLLM 行に「ポート」表示を追加。モデル切替リストを横方向ラップレイアウト(fit-content)に変更し、
  停止中・切替中のメタ情報を赤字表示。ジョブログは折り返しなし(`white-space: pre`)に変更

## 実装メモ(2026-08-29)

- 一段目ゲージパネルの幅を固定化: `.gauge` を `flex: 1 1 200px; min-width: 190px`(行数に応じて可変)から
  **`flex: 0 0 218px`(固定幅)** に変更。これにより 1 行に何枚並んでも全パネルが同一幅になる(修正前は
  6 枚行と 4 枚行で幅が異なっていた)。218px は旧 max-width 基準で 6 枚/行 + gap がちょうど収まる値。
  幅変更は `.gauge` の `flex-basis` 1 箇所のみで調整可能
- `main` から **`max-width: 1400px; margin: 0 auto` を削除**し、コンテンツがブラウザ幅いっぱいに広がるようにした。
  左右 20px のパディングは維持。これにより画面が広い環境では一段目に 6 枚より多く並ぶことがある(固定幅の flex-wrap 挙動)

## 実装メモ(2026-08-30)

- **モデル切替ジョブログのヘッダー行重複表示を修正**: 切替モーダルの実行画面で
  「=== 起動中です。直近4件のログを表示中 ... ===」行が2本表示される不具合に対し、`api/vllm.py` の `job_status()` で
  **最後の `=== ... ===` ヘッダー行以降の1フレーム分(最大5行)のみを返す**ようにした。
  原因は `switch_models.sh` の `display_frame()` が端末の上書き表示のため1秒ごとに5行ブロック
  (ヘッダー+ログ4行)を ANSI カーソル移動付きで繰り返し出力し、API 側が stdout をファイルにリダイレクトしているため
  カーソルコード除去後・各フレームが新規行として追記され、末尾12行の窓に約2フレーム分のコピーが残ること。
  修正後はポーリングごとに最新フレーム(ヘッダー1行+直近4ログ行)のみが表示される。
  ジョブ完了時は「=== 起動完了: ... ===」ブロックがそのまま表示され、`===` 行の無い recreate ジョブは挙動不変。
  スクリプト側は変更していない。検証: 実物ログと同形式(ANSI/`\r` 込み)のダミーログによる隔離テスト3ケース +
  実際のジョブログファイルでの読み取り専用チェック。実切替時の動作確認は未実施
- **モデル停止機能を追加**: vLLM 行のボタン列に「停止」ボタンを右端に追加(暗めの赤で控えめなスタイル)。
  押下で確認ダイアログを表示し、承諾すると `POST /api/vllm/stop` → `api/vllm.py::stop_model()` が
  `docker stop --time 30 <container>` をバックグラウンドジョブ(kind=`"stop"`)として実行する(既存の
  ジョブ機構・同時実行ガード・進捗ポーリングを再利用)。フロントは `/api/vllm/job` をポーリングして
  ダイアログ内に経過時間/ログを表示し、**停止完了でダイアログを自動閉じる**。
  コンテナ状態表示は「稼働中 (API応答)」→「停止中…」(赤)→コンテナ終了後 active が null になり
  「停止中」(灰・`.v.down`)と遷移する。停止ボタンは稼働中モデルがありジョブ実行中でないときのみ有効で、
  停止したモデルはモデル切替から再起動できる。検証: `_load_profiles`/`_container_state`/`_start_job` を
  モックにした隔離テスト5ケース(エラー系・コマンド内容・同時実行拒否・エンドポイント登録/409マッピング) +
  フロントの id 参照整合性チェック。実停止時の動作確認は未実施
- **実環境で動作確認済み**(2026-08-30):
  1. モデル切替ジョブログのヘッダー行重複表示修正 — 実切替時に「=== 起動中です。直近4件のログを表示中 ... ===」が1行だけ表示されること
  2. モデル停止機能 — ボタンの有効/無効化、確認ダイアログ→進捗表示→自動閉じ、状態遷移(稼働中→停止中…(赤)→停止中(灰))
  3. 切替中の動作状況のメイン画面同期 — 実切替中にvLLM行「コンテナ状態」が「切替中…」(赤)に表示され、完了後に通常表示へ戻る
- **ジョブログ表示の調整**: `.job-log` の `overflow: auto` を
  **`overflow-x: hidden; overflow-y: auto`** に変更し、長いログ行でも横スクロールバーが出ず折り返さずに
  オーバーフロー分をクリップする(縦スクロールは不変)。また切替モーダルのログ領域(`#switch-log`)は
  **常時表示・7行固定高**(line-height 14px × 7行 + padding/border = 116px)に変更し、初期表示時と
  切替完了後の非表示化を撤廃した。内容が空のときは `.job-log:empty::before` で灰色のプレースホルダー
  「ログ」を表示する(再作成/停止のログは従来どおりジョブ実行時のみ表示)。
- **切替中の動作状況をメイン画面に同期**: `renderVllm()` で切替ジョブ実行中
  (`st.switching.kind === "switch"`) は、vLLM行「コンテナ状態」を**「切替中…」(赤・`.hot`)** に表示する。
  これまでは旧モデル停止済み・新モデルAPI未応答の遷移期間に「稼働中 (API応答なし)」/「停止中」と
  誤解を招く表示になるケースがあった。active の有無に関わらず切替中表示が優先され、完了後は既存の
  2秒ポーリングで自動的に通常表示へ戻る。フロントのみ変更(API側は `switching` 情報を
  `/api/vllm/status` で既に返しているため不変)。

## 実装メモ(2026-08-31)

- **モデル実行ログダイアログを追加**: vLLM 行のボタン列に「ログ」ボタンを追加(モデル切替と
  パラメータ表示・編集の間)。押下で三段構成のモーダルを表示:
  一段目は「<モデル名>の実行ログ」(左)+「検索」「再読み込み」「ダウンロード」(右)、
  二段目はログ表示(縦スクロール・横折り返し・初期表示は末尾)、三段目は「閉じる」(右)。
  モーダル幅はブラウザ画面幅に追従する(`.log-box` の `width: 100vw`)。
  API 側は `GET /api/vllm/log?profile=<profile>` を追加(`api/vllm.py::get_log()` が
  `docker logs --tail 500 <container>` を実行、stdout+stderr を取得)。
  検索はキーワードでの行フィルタ、ダウンロードは `<profile>.log` として保存。
  稼働中モデルがあるときのみ有効(切替中も表示可)。
  検証: エンドポイントの動作確認(正常プロファイルはJSON返却/不明プロファイルは400)、
  配信HTMLの tag 整合性(script/div 数)と幅指定の反映確認。
- **スループットを入力/出力に分離 + held値を灰色表示**: vLLM 行の「スループット」1項目を
  「入力スループット」(prompt) /「出力スループット」(generation)の2項目に分離(合計値 `tokens_per_s` は廃止)。
  API 側 `api/vllm.py` は `vllm:prompt_tokens_total` / `generation_tokens_total`
  (SGLang 相当系列も)のカウンター差分を**入力/出力別に**算出する(`_prev_metrics` / `_last_tps` を
  key 別に分離、vLLM・SGLang 両パス共通のヘルパ `_tps_of()` を新設)。
  `/api/vllm/status` の metrics は `prompt_tokens_per_s` / `generation_tokens_per_s` と
  それぞれの `*_tps_held` フラグを返す(従来の `tokens_per_s` 合計は削除)。
  片方の系列が欠落する場合は該当側のみ算出し、他側は前回保持値を維持(held)。
  フロントは `*_tps_held=true`(今回非計測で前回非ゼロ値を引き継いでいる)の値を灰色
  (`.v.stale`、muted)で表示し、最新計測値は従来どおり白。値が null のみ `-`。
  検証: モック `/metrics` による隔離テスト(初回 null / 非ゼロ差分 held=false / 差分0 で前値維持
  held=true / 片側系列欠落で他側保持) + 実機 API で新フィールドの確認。
- **vLLM 行 2 段目のラベル短縮・幅圧縮**: 項目数を 6→4 に圧縮し、各値ボックスの `min-width`
  も縮めた分だけ縮小した(フロントのみ変更、API・データ形式は不変)。
  - 「実行中リクエスト」+「待機リクエスト」を統合して「実行/待機」1項目に(値は `0 / 0` 形式、
    双方 null のときのみ `-`、id は `v-running`/`v-waiting` → `v-rw`)
  - 「KVキャッシュ使用率」→「KVCache」(`#v-kv` 12ch→7ch)
  - 「E2Eレイテンシ (平均)」→「E2E」(`#v-e2e` 12ch→7ch)
  - 「TTFT (平均)」→「TTFT」(`#v-ttft` 6ch 維持、値幅は既に最小)
  検証: HTML の id 参照整合(setV 呼び出し id が全て定義済み)・tag 整合性チェック。
- **スループットの白/灰表示基準を「絶対 held 判定」から「相対最終更新判定」へ変更**: 従来の
  「`*_tps_held=true` なら灰色」ではリクエストが無いとページ表示直後に両方が即座に灰色になるため、
  基準を「どちらがより更新されたか」に変更(フロントのみ)。
  「更新」= API が `*_tps_held=false` かつ非 null を返した場合(null 値は更新とみなさない)。
  フロントは各側の最終更新時刻(`tpsLastUpdate`、モジュール変数)を保持し、新しい側が白
  (`.v.stale` 付与なし)/ 古くなった側が灰色。同じポーリングで両方更新された場合(同一時刻)は
  次の更新まで両方白、どちらの更新もなければ前回の色を維持。初期状態(更新なし)は「-」+ 灰色。
  検証: HTML の tag / id 整合性チェック + 配信 HTML での反映確認。

## 実装メモ(2026-09-03)

- **タイトルヘッダーに GPU Performance State を表示**: `api/metrics.py` の `_read_gpu()` が
  nvidia-smi のクエリに `pstate` を追加し、`/api/metrics` が `gpu_pstate`(例: `"P0"`)を返すようになった。
  フロントはヘッダーの現在日時(`#clock`)の右に `#pstate` span を新設し、ポーリング時に
  `renderPstate()` で値と色を設定する。色分けは P0=緑(`--green`)、
  P2 / P3 / P8 / P12 / P15=黄(`--yellow`)、それ以外=赤(`--red`)。取得できない場合は非表示。
  実環境で P0 の表示を確認済み。uvicorn は `--reload` なしで起動しているため、バックエンド
  (`metrics.py`) の変更は `sudo systemctl restart dgx-spark-api` による再起動が必要。
  フロント(`index.html`)の変更はブラウザのリロードのみで反映される。
- **vLLM パネルの文言短縮と列幅圧縮(フロントのみ、API・データ形式は不変)**:
  - ボタン文言「モデル切替」→「モデル」、「パラメータ表示・編集」→「パラメータ」
  - 値ボックスの `min-width`: 実行/待機 `#v-rw` 7ch→5ch、E2E `#v-e2e` 7ch→3ch、
    TTFT `#v-ttft` 6ch→5ch

## 実装メモ(2026-09-13)

- **compose 側のモデル追加・削除に追従(コード変更なし)**: `~/llm/compose` 側で TabbyAPI
  (Qwen3.8 Flash-Next EXL3、`qwen38flashnextexl3`、ポート 8009)を追加し、qwen(72B NVFP4) /
  laguna(72B NVFP4) / qwen36(35B FP8)の 3 モデルを削除した変更に対し、本ユーティリティは
  `api/vllm.py::_load_profiles()` が docker-compose.yml を動的にパースするため**コード変更なしで
  自動追従**した。削除モデルは一覧から自動で消え、TabbyAPI は一覧・切替・監視の対象に自動で
  追加される(稼働中 API で `active` 検出・`health=true`・`model_name` 取得を確認済み)。
  TabbyAPI は `/metrics` が 404 のためメトリクス系は「-」表示(muse/llama.cpp と同じ既存挙動。
  2026-09-14 にログ解析による取得を追加、下記実装メモ参照)。
- **unsloth モデル(`qwen38flashnextgguf`)は追従対象外**: ホストプロセス方式で
  docker-compose.yml に存在しないため、`_load_profiles()` の対象外となり一覧にも現れない。
  当該モデルは今後の削除予定のため今回は対応しない(将来 docker コンテナ化された場合は
  compose への追加だけで自動追従する)。
- **README のモデル一覧・プロファイル列挙を現状に更新**: 上記の追加・削除を反映し、
  モデル一覧テーブルを現在の 10 種(vLLM 4 + SGLang 4 + llama.cpp 1 + TabbyAPI 1)に更新。
  vLLM イメージは 26.07 → 26.08 に移行済み。

## 実装メモ(2026-09-14)

- **TabbyAPI(`tabbyapi-flashnext`)のメトリクス表示(実行/待機・KVCache・E2E・TTFT・
  入力/出力スループット)**: TabbyAPI(commit de76ff88)は `/metrics` が 404 で `/get_load` も
  無いため、従来は 6 項目すべて「-」表示だった。docker logs にリクエスト毎の統計が出力される
  こと(`common/gen_logging.py`、例: `#3932 chat/completions (stream): 986 tokens generated at
  67.7 T/s · prompt 200,725 tokens, 99% cached, 1,557 new in 2.20 s (708 T/s) · first token
  2.20 s, total 16.8 s`)を利用し、`api/vllm.py` でログ解析するメトリクス取得を追加した。
  フロントは `active.metrics` を汎用的に描画しているため**変更なし**。
  - 判定: `/.well-known/serviceinfo` の本文に "TabbyAPI" が含まれるか(`_is_tabbyapi()`)。
    `get_status()` の分岐は vLLM → SGLang → **TabbyAPI(新規)** → `/get_load` フォールバック。
    TabbyAPI 以外では `_reset_tabby_state()` でログ追跡状態をクリアする。
  - 実行/待機: ログの req_id(`#NNNN`)を追跡し「開始ログあり・完了ログなし」= 実行中
    (`_tabby_in_flight`、ポーリング間で保持)。`max_batch_size`(=1、`/v1/model` から取得)
    超過分は待機。1 時間超の残留エントリは破棄(クラッシュしたリクエストの安全策)。
  - KVCache: **推定値** = (直近完了リクエストの prompt+gen トークン) / `cache_size`(=262144)
    × 100。TabbyAPI に直接使用率 API は無いが、KV キャッシュは直近会話分を保持するため
    精度は高い(例: prompt 204,123 + gen 633 → 約 78%)。
  - E2E / TTFT: 直近完了リクエストの `total` / `first token`(= queue + prefill)。
  - 入力スループット: **全プロンプトトークン換算** = `prompt_tokens / prompt_time`
    (vLLM の `prompt_tokens_per_s` と同一セマンティクス、キャッシュヒット含む)。
    出力スループット: 直近完了の `gen T/s`。
  - スループットは既存の held ロジックに準拠: 直近ポーリングで新規完了(req_id 増加)があれば
    `held=false`(白)、なければ前回値を `held=true`(灰)で維持。req_id は単調増加のため
    「新完了」判定に使用(`_tabby_last`)。
  - ログパース: loguru の折返し(継続行は先頭が空白)をタイムスタンプ行で結合してから
    開始/完了に正規表現マッチ(`_tabbyapi_parse_logs()`)。「parsed N tool call」等の
    中間ログは無視。`docker logs --tail 300` を使用(リクエストログは疎のため十分)。
  - 検証: 稼働中 API で `active.metrics` に KVCache/E2E/TTFT/スループットが入ること、
    テストリクエスト送信で新リクエストの値に更新され `held=false` になることを確認済み。
- **Cline設定値ボタン追加**: 三段目の「パラメータ」と「停止」の間に「Cline設定値」ボタンを追加。
  押下でモーダル表示し、Cline に設定すべきモデル情報を表示(各値を個別コピー + 「すべてコピー」)。
  - API: `GET /api/vllm/cline`(`api/vllm.py::get_cline_config()`)。稼働中モデルが無ければ 409。
  - 表示項目: API Configuration(API Provider / Base URL / API Key / ModelID) +
    MODEL CONFIGURATION(Supports Images / Context Windows Size / Max Output Tokens /
    Temperature / Reasoning Effort)。
  - データ源(ハイブリッド): 動的取得(モデル名 `/v1/models`・ポート・hostname・コンテキスト
    `--max-model-len`/`--context-length`/`--ctx-size`・温度 `--temp`・画像対応フラグ) +
    compose に無い値はモデル別静的テーブル `CLINE_MODEL_TABLE` で補完。
  - Base URL は `http://<hostname>.local:<port>/v1/`(外部利用想定、hostname は動的取得)。
    API Key は各サービスが認証なしのため「認証なし(任意の値でOK)」表示。
  - 静的テーブルの Max Output Tokens(8192/32768)・温度(0.6)・Context 最大はモデル知識に基づく
    仮定値。`CLINE_MODEL_TABLE` で修正可能。
  - 検証: 稼働中モデル(qwen38flashnextexl3/TabbyAPI)で `get_cline_config()` が正しい値を
    返すことを確認済み(Base URL `http://WhitebearATOM1.local:8009/v1/`・ModelID
    `qwen3.8_flashnext_exl3_2p05bpw`・画像対応 true・Context 524288)。

## 実装メモ(2026-09-17)

- **ストレージ負荷(読込/書込)ゲージの 100% 基準を 12 GB/s → 5 GB/s に変更**:
  `front/index.html` の `GAUGE_DEFS` `g-disk-read`/`g-disk-write` `max: 12000` → `5000`、
  サブテキスト「上限 12 GB/s」→「上限 5 GB/s」、統合時系列グラフの `NORM.diskIO: 12000` → `5000`
  (ストレージ読込 % / 書込 % 系列の 100% 換算)。ゾーン帯は比率ベースのため自動で追従
  (緑 <3000 / 橙 3000〜4250 / 赤 >4250 MB/s)。5000 MB/s 超の値は 100% でクリップ(従来と同じ挙動)。

## 実装メモ(2026-09-19)

- **Cline設定値コピーの `navigator.clipboard` 未定義エラーを修正**:
  LAN PC から平文 HTTP(`http://<host>:<port>`)でアクセスしたとき、ブラウザの
  セキュリティコンテキスト外のため `navigator.clipboard` が `undefined` になり、
  Cline設定値モーダルのコピーボタン(個別・すべて)で
  `TypeError: Cannot read properties of undefined (reading 'writeText')` が thrown されていた。
  既存の `.catch()` は例外が同期で thrown されるため到達しなかった。
  `front/index.html` の `copyClineText` を修正し、`navigator.clipboard.writeText`
  が存在する場合のみ最新 Clipboard API を使用、それ以外(非セキュリティコンテキスト)は
  非セキュリティコンテキストでも動作する legacy の `document.execCommand('copy')`
  にフォールバックした。
  - 判定: `navigator.clipboard && typeof navigator.clipboard.writeText === "function"`
  - フォールバック: 非表示 textarea にテキストを挿入 → `focus()`/`select()` →
    `document.execCommand("copy")` → 成功なら「コピー済み」表示、失敗なら `prompt` 提示。
  - 個別コピーと「すべてコピー」は両方この関数を呼ぶため、1箇所修正で両方直る。
  - 反映: ブラウザのリロードのみで反映(サーバ/モデル再起動不要)。
  - 検証: LAN PC から `http://192.168.0.110:8080` でCline設定値モーダルを開き、
    個別コピー・すべてコピーの両方でクリップボードに反映されることを確認済み。

## 実装メモ(2026-09-25)

- **MiMo-V2.6 系 2 モデル(`mimo_flash` / `mimo9b`)の Cline 設定値テーブル追加**:
  `~/llm/compose` 側に MiMo-V2.6-Flash(309B-A24B, TabbyAPI ExLlamaV3 1.5.1, ポート 8013)と
  MiMo-V2.6-Flash-Instruct-2511(Distill-Qwen3.5-9B, vLLM, ポート 8014)が追加された。
  モデル一覧・切替・監視は `_load_profiles()` が compose を動的パースするため**自動追従**し、
  追加したのは `api/vllm.py` の `CLINE_MODEL_TABLE` の 2 エントリのみ。
  - `mimo_flash`: `images=False`(テキスト専用)・`context_max=262144`
    (KV cache_size。ネイティブ 1M は Q8 KV で MemAvailable 1.4GB まで低下するため 262K 運用)
  - `mimo9b`: `images=True`(マルチモーダル)・`context_max=1010000`(YaRN factor 4.0)
  - 反映: `CLINE_MODEL_TABLE` は import 時に読まれるため `sudo systemctl restart dgx-spark-api`
    で再起動。再起動後 `active`(mimo_flash)で `context_max=262144`・`supports_images=false` を確認済み。
- **README のモデル一覧・プロファイル列挙を現状に更新**: 上記 2 モデルに加え、
  2026-09-18 の `qwen38flashnextexl3_3p05` と 2026-09-23 の `qwen38swift` /
  `qwen38swift_dflash` を反映し、モデル一覧テーブルを現在の 15 種
  (vLLM 6 + SGLang 5 + llama.cpp 1 + TabbyAPI 3)に更新。
  `qwen38flashnextexl3` のコンテキストは YaRN 1M 化(2026-09-18)を反映。
- **Cline設定値モーダルで設定に貼る値(数値など)のみをコピー可能に**:
  従来は個別コピーボタンが値の全文をコピーするため、「Context Windows Size」などで
  `16384 (推奨) / 32768 (最大)` のような文章全体がコピーされ、Cline の設定へそのまま
  ペーストできなかった。
  - 変更: `front/index.html` の `renderCline()` で行の値を「パーツ」のリスト
    (`{ text, copy }`)に一般化し、各パーツに個別コピーボタンを配置(そのパーツの
    `copy` のみコピー)。単一値は 1 パーツ(表示=コピー)で従来どおり。
  - Context Windows Size / Max Output Tokens: 推奨/最大を 2 パーツに分割し、
    各コピーボタンが数値のみをコピー。
  - Reasoning Effort: 各選択肢(None/Low/Medium/High/XHigh)を個別コピー可能に。
  - CSS: `.cline-value` を flex 容器化し `.cline-part` / `.cline-sep` を追加
    (長い値の折り返しは `.cline-part` の `word-break: break-all` で維持)。
  - 「すべてコピー」ボタンは参照用の全文コピーを維持。
  - 検証: HTML の tag 整合性チェック + `renderCline` の JS 構文チェック(esprima) +
    配信 HTML に新コードが反映されていることを確認済み。

## 実装メモ(2026-09-27)

- **ストレージ負荷(読込/書込)を 1 メータ内に 2 値表示に統合**:
  従来は `g-disk-read`(読込)と `g-disk-write`(書込)の独立した 2 ゲージだったが、
  1 つの半円ゲージ(`g-disk-io`「ストレージ負荷」)内に**外側アーチ=読込(緑 #56d364) /
  内側アーチ=書込(黄 #e3b341)**として 2 値を別々に表示するよう変更(合計値ではない)。
  中央値も 2 行(読込 / 書込)に分割し、各行を対応色で表示。100% 基準は従来どおり 5 GB/s。
- **ネットワーク負荷(下り/上り)ゲージを新規追加**:
  1 つの半円ゲージ(`g-net`「ネットワーク負荷」)内に**外側アーチ=下り(青 #58a6ff) /
  内側アーチ=上り(紫 #bc8cff)**として 2 値を別々に表示(合計値ではない)。
  **10 Gbps = 100%** とし `max: 10000`(Mbps)で換算(10^6 bits/s 基準)。
  ※ 上限の固定値運用は 2026-10-06 に実測リンク速度基準へ変更(下記 実装メモ(2026-10-06) 参照)。
  - API 側(`api/metrics.py`): `/proc/net/dev` の rx/tx バイト差分で `net_down_mbps` /
    `net_up_mbps` を算出。計測対象は `/proc/net/route` のデフォルトルートIF
    (実機では `enP7s7`)のみ。docker の `br-*`/`veth*`/`docker0` は内部トラフィックのため
    二重計上になるため除外。
- **`Gauge` クラスを dual(2 値)対応に拡張**:
  `dual: true` のゲージ定義は `getA`/`getB`/`colorA`/`colorB`/`labelA`/`labelB` を持ち、
  外側アーチ=A / 内側アーチ=B を固定色で描画(単一ゲージは従来どおりゾーン色)。
  色ゾーン帯(緑/橙/赤)は外側に維持。CSS に `.gval-dual` / `.gval-line` を追加。
  一段目のゲージ数は 10 → 10 で維持(読込/書込を 1 つに統合し、ネットワークを 1 つ追加)。
- **2 値表示の中央値が内側アーチと重なる問題の修正**:
  2 行の中央値は `top: 50%` 配置だと内側アーチの頂点と重なるため、`.gval-dual` を
  `top: 77%`(下方移動)+ `font-size: 12px` / `line-height: 1.3` に変更。
  2 行の文字列(幅約 90〜100px)が内側アーチ(半径 63px)内に収まるには下方移動だけでは
  canvas 下端超過するため文字縮小も必要。併せてネットワークの `fmt` を `toFixed(0)` に
  変更(最大値 "10000.0" の 7 文字が幅広になるため "10000" の 5 文字に短縮)。
- 反映: API 変更のため `sudo systemctl restart dgx-spark-api` で再起動が必要
  (フロントはブラウザのリロードのみ)。
- 検証: `metrics.collect()` で `net_down_mbps`/`net_up_mbps` が正しく返ること
  (デフォルトIF=`enP7s7`)、JS 構文チェックを確認済み。

## 実装メモ(2026-09-29)

- **統合メモリゲージ(`g-mem`)の表示改善**:
  - ゲージタイトル「System Memory」→「統合メモリ」に変更(GB10 の CPU/GPU 共有メモリ構成に合わせる)。
  - 中央の使用率に % 単位を追加(`unit: ""` → `"%"`、例: `75.2` → `75.2%`)。
  - メーター内に空きメモリの行(使用率の 2 行目、例: `空き 29.7 GiB`)を追加。
    空き量はフロント側で `mem_total_gib - mem_used_gib` を算出(API 変更なし)。
  - 下部サブ行は使用/総量(例: `91.8 / 121.6 GiB`)に戻した。
- **`Gauge` クラスに `subVal` フックを追加(単一値ゲージのメーター内 2 行目)**:
  単一値ゲージで `subVal(m)` を定義すると中央値の直下に 2 行目を描画する。
  CSS `.gval-sub` は `position: absolute; top: 100%` で 2 行目をメイン値の下端に吊り下げるため、
  メイン値は左右の 1 行ゲージと同一の高さに揃う(2 行ブロックの中央揃えで
  メイン値が上にずれる問題の回避)。文字色は `var(--text)`(白系)。
- 反映: フロントのみ変更で API 変更なし。ブラウザのリロードのみで反映。
- 検証: JS 構文(テンプレートリテラルの整合)と CSS 位置確認(2 行目は canvas 内
  63〜77px 付近でアーチと重ならない)を確認済み。

## 実装メモ(2026-10-02)

- **RAG環境 (sociax-rag) の起動/停止 Web UI 追加**(WhitebearATOM2 の RAG 環境との共存のため):
  - 四段目に新セクション「RAG (sociax-rag)」を追加。Embedding (8010) / Qdrant (6333/6334)
    の状態(稼働中 (API応答) / 起動中… (API応答なし) / 停止中)と稼働時間を 2 秒毎ポーリングで表示。
    「起動」「停止」ボタンは確認モーダル(三段目の停止モーダルと同一パターン)で実行し、
    ジョブログを自動更新・完了時にモーダル自動閉じる。
  - 新規 `api/rag.py`: `docker compose --env-file .env -f compose.yaml up -d` / `stop` を
    バックグラウンドジョブとして実行。vllm.py と同じジョブパターン(log_tail 付き)だが
    ジョブ状態は独立 `_job` → モデル切替と RAG 操作の並行実行を許可。
    停止はコンテナが 1 つも稼働していない場合のみ 409 拒否。起動は冪等(`up -d` が何もしない)。
  - 新規 API: `GET /api/rag/status` / `POST /api/rag/start` / `POST /api/rag/stop` /
    `GET /api/rag/job`。
  - 状態判定: `docker compose ps` + `docker inspect`(稼働/稼働時間/再起動回数) +
    HTTP 健全性(embedding `:8010/v1/models`、qdrant `:6333/`)。ポートは `.env` から動的取得。
  - 起動直後は embedding のモデルロード完了まで API 未応答(healthcheck `start_period: 180s`)。
    UI は「起動中… (API応答なし)」(赤)で表示し、応答後に「稼働中 (API応答)」(白)へ遷移する。
  - 停止は `docker stop` のため `restart: unless-stopped` が無効化され、停止後自動再起動しない
    (WhitebearATOM2 側 RAG 使用期間中に停止を維持できる)。Qdrant データは volume に永続化済み。
- 検証: 停止ジョブ実行 → 両コンテナ停止(status API で running=false 確認、停止中での
  再停止は 409)→ 起動ジョブ実行 → 両コンテナ再起動 → `check-services.sh` 4 項目
  (embedding モデル一覧 / 1024 次元ベクトル / Qdrant REST / gRPC)全て通過 →
  status API で running=true, healthy=true を確認済み。
- **下部余白の縮小(縦スクロールバー出現の緩和)**:
  ブラウザの表示領域を狭めたとき、下部に余白があるのに縦スクロールバーが出る現象を緩和するため、
  最下部の余白を縮小した。`main` の `padding-bottom` を 32px→12px、
  `section:last-child { margin-bottom: 0; }` を追加(最下段セクションの 22px を潰す)。
  最下部余白は 54px(22+32)→12px に縮小。セクション間の 22px は維持。
- **RAG 稼働時間の起点を `State.StartedAt` に修正**:
  稼働時間が 1246 時間(約 52 日)と実際より長く表示されていた。原因は起点に
  `docker inspect` の `Created`(コンテナの**作成**日時、再起動しても更新されない)を使っていたこと。
  `State.StartedAt`(**最後に起動した日時**、再起動毎に更新)に変更した。
  検証: 停止→起動テスト後のコンテナで `00:39:13`(実際の最終起動から約 39 分)を正しく表示。
  注: `api/vllm.py` の `_container_state` も同様に `Created` を起点にしているが、
  vLLM のモデル切替は `--force-recreate` でコンテナを再作成するため `Created` が新しめになり
  顕在化しにくい。今回は RAG のみ修正(vllm.py は変更していない)。

## 実装メモ(2026-10-03)

- **両環境対応(WhitebearATOM2 / AMD R9700 追加)** — 本リポジトリを DGX Spark と
  whitebearatom2 の両方で動かすリファクタ。プラットフォーム固有値を新規 `api/config.py` に集約し、
  自動検出 (nvidia-smi あり → `dgx-spark` / amdgpu sysfs あり → `atom2`。
  環境変数 `DGXUTIL_PLATFORM` / `DGXUTIL_COMPOSE_DIR` / `DGXUTIL_RAG_DIR` で上書き可):
  - `COMPOSE_DIR`: dgx-spark `/home/cliclie/llm/compose` / atom2 `/home/cliclie/LLM/compose`
  - `RAG_DIR`: dgx-spark `/home/cliclie/llm/compose/sociax-rag` / atom2 `/home/cliclie/RAG/compose`
  - `MEMORY_MODE`: unified(統合メモリ 1 枚)/ discrete(RAM + VRAM 2 枚)、`NET_MAX_MBPS` 10000/1000
    (2026-10-06 以降は**リンク速度が取れない時のフォールバック既定値**に格下げ)、
    `EXCLUSIVE_LLM_RAG`(atom2 のみ True: VRAM 32GB のため LLM ⇔ RAG embedding 排他)
- **`api/metrics.py` GPU バックエンド分割**: `_read_gpu_nvidia`(現行) / `_read_gpu_amdgpu`
  (rocm-smi 未インストールのため sysfs 直接読み: `gpu_busy_percent`、hwmon `temp*_input`
  の label=junction 優先、`power1_average`/`power1_cap`(µW)、`freq*_input` label=sclk +
  `pp_dpm_sclk` フォールバック、`mem_info_vram_used/total`、`mem_info_gtt_*`)。
  返却 JSON に `platform` / `memory_mode` / `gpu_vendor` / `vram_*` / `gtt_*` /
  `gpu_power_cap_w` / `gpu_clock_max_mhz` / `gpu_temp_crit_c` を追加。
  ディスク計測対象は `/proc/mounts` のルートソースから親デバイスを自動導出(nvme0n1p2 → nvme0n1)。
  CPU温度は acpitz → x86_pkg_temp フォールバック。
- **新規 API `GET /api/platform`**: フロントの表示切替用(ゲージ構成・上限値・排他注記)。
- **フロント両対応** (`front/index.html`): ゲージ/グラフの生成をプラットフォーム取得後に実行
  (`buildHostCards()` / `buildMainChart()`)。discrete 時は「統合メモリ」ゲージの位置に
  **RAM + VRAM の 2 ゲージ**(VRAM=シアン #39c5cf、書式は統合メモリと同じ: 使用率% +
  空き GiB / 使用・総量)、時系列グラフにも「VRAM使用率 %」系列を追加。
  GPU 電力/クロック/温度の上限はプラットフォーム既定値から実測値 (`*_cap_w` /
  `*_max_mhz` / `*_crit_c`) へ自動補正。ネットワーク上限は 1Gbps/10Gbps 切替
  (2026-10-06 にプラットフォーム既定値 → 実測リンク速度へ変更)。
  バックエンド未提供の項目 (`undefined`) はゲージ自体を非表示。
  ヘッダータイトルは dgx-spark が「DGX Spark Monitor」、atom2 が `<hostname> Monitor`。
- **LLM ⇔ RAG 排他 (atom2)**:
  - `rag.py` の起動は `switch_models.sh rag`(稼働中 LLM を停止して embedding 起動、
    API 応答まで待機) → 続けて `docker compose up -d`(Qdrant 補完起動) を 1 ジョブとして実行。
    dgx-spark は従来どおり `compose up -d` のみ。停止は両環境とも `compose stop`(embedding+Qdrant 全体)。
  - `/api/rag/status` に `llm_running` / `blocked_by_llm`(LLM 稼働中による embedding 停止) を追加。
    UI は「停止中 (LLM 排他)」表示 + セクションヘッダーに「※ LLM ⇔ RAG は VRAM 排他」注記。
    起動ボタンは atom2 では embedding 基準(Qdrant 常時稼働でも押せる)。
  - `/api/vllm/status` に `rag_running` を追加し、モデル切替モーダルに
    「稼働中の RAG embedding を停止して切り替えます (VRAM 排他)」警告を表示。
- **`vllm.py`**: CLINE_MODEL_TABLE に atom2 用 3 プロファイル
  (qwen38radiance / qwen38sglangrocm / qwen38gguf、context 262144) を追加。
  プロファイル対応表は従来どおり docker-compose.yml から動的パース。
- **デプロイ汎用化**: `install_service.sh` はスクリプト位置から API_DIR を導出、
  実行ユーザーは SUDO_USER。unit ファイルは `__API_DIR__` / `__USER__` プレースホルダを
  sed で置換してインストール(サービス名・ポート 8080 は両環境共通)。
- 検証 (whitebearatom2 実機): `/api/platform`=atom2 確認。`/api/metrics` で
  VRAM 31.86 GiB 総量・embedding 起動時 10.08 GiB / LLM 起動時 29.17 GiB 使用を実測。
  RAG 起動 → LLM 切替(embedding 自動停止・radiance health OK・/metrics 取得)→
  RAG 再起動(LLM 自動停止)→ RAG 停止 の排他サイクル全通過。
  JS 構文チェック(node --check 全ブロック OK)。dgx-spark 側は nvidia 経路が
  デフォルト動作のまま(統合メモリ 1 枚・10Gbps・GB10 上限値)で変更なし。
- **README 同期**: アーキテクチャ図の API 一覧に欠落エンドポイント
  (`/api/platform` / `/api/history` / `/api/vllm/stop` / `/api/vllm/job` / `/api/rag/*`)
  を追加し、3段→4段構成の旧記述を修正。データ源に amdgpu sysfs を追記、
  未使用の psutil 記述を削除。RAG セクションの対象ディレクトリをプラットフォーム別に、
  ユニットファイルのテンプレート化を注記。
- **GPU 電力上限 PPT0 210W (whitebearatom2 / R9700)**: モデルロード時に GPU 電力が
  300W (stock) まで到達し、持続 LLM 負荷で 100°C になる問題への恒久対策。
  systemd サービス `llm-gpu-powercap.service` (oneshot / enabled) で起動時に
  `amd-smi set -g 0 -o ppt0 210` を実行 (unit は実機の `/etc/systemd/system/` に配置、
  リポジトリ外)。PPT0 の設定可能範囲は 210〜300W (sysfs `power1_cap_min/max`)。
  - 初版は `After=local-fs.target` のみで、起動時に amdgpu モジュール未ロードのため
    `amd-smi` が "driver not initialized" で失敗し、再起動後 cap が 300W に戻っていた。
  - 修正: `After=systemd-modules-load.service` 追加 + `ExecStartPre` で
    `power1_cap` sysfs の出現 (amdgpu ロード完了) を最大 120 秒待機。
  - 検証: 推論 (4096 トークン) / モデルロード (vLLM 再起動) の両方で電力 210W に
    キャップ (一時的オーバーシュート 212W / 235W は 0.5 秒サンプル 1 回分)、
    温度 56〜58°C。再起動後サービス active・`power1_cap`=210000000 (210W) を確認。
  - Trade-off: compute-heavy な prefill は THROTTLED (低速化)、モデルロードも低速化。

- **llama.cpp メトリクス対応 (2026-10-04)**: llama.cpp ネイティブサーバー (`llama-cpp-vulkan-r9700:latest`,
  v0.5.0-dev build 11339) が TabbyAPI ではなく llama-server だったため、三段目のメトリクスが全項目 "-"
  になる問題の修正。
  - **docker-compose.yml**: `qwen38_27b_gguf` の command に `--metrics` フラグを追加
    (Prometheus 形式の `/metrics` を有効化)
  - **`api/vllm.py` に llama.cpp 対応関数を追加**:
    - `_is_llamacpp(base)`: `/v1/models` の `owned_by: "llamacpp"` で llama.cpp を検出
    - `_llamacpp_slots_kv(base)`: `/slots` から `n_prompt_tokens / n_ctx × 100` で KV キャッシュ使用率を算出
    - `_llamacpp_metrics(pm, base)`: llama.cpp の Prometheus 系列を vLLM と同じ構造にマッピング
  - **`get_status()` の分岐追加**: vLLM → SGLang → **llama.cpp (新規)** → TabbyAPI → `/get_load`
    - `llamacpp:` プレフィックスあり → `_llamacpp_metrics(pm, base)`
    - `_is_llamacpp()` 検出 → `_llamacpp_metrics({}, base)` (`/slots` のみで基本状態)
  - **系列名マッピング** (実機 `/metrics` で検証済み):
    - `llamacpp:requests_processing` (gauge) → `requests_running`
    - `llamacpp:requests_deferred` (gauge) → `requests_waiting`
    - `llamacpp:prompt_tokens_seconds` (gauge) → `prompt_tokens_per_s` (直接値・カウンター差分不要)
    - `llamacpp:predicted_tokens_seconds` (gauge) → `generation_tokens_per_s` (直接値)
    - KV キャッシュ使用率 → `/slots` の `n_prompt_tokens / n_ctx × 100`
    - E2E / TTFT → v0.5.0 では未公開 (None → "-" 表示)
  - **検証** (whitebearatom2 実機): リクエスト後 `generation_tokens_per_s: 49.3 tok/s`、
    `kv_cache_usage_pct: 22.0%` を確認。アイドル時はスループット 0・実行/待機 0 が正しく表示。

## 実装メモ(2026-10-04)

- **atom2 側改修の atom1 (WhitebearATOM1 / DGX Spark) への反映**: 親リポジトリの更新
  (0c77106 → aeb60f0、3 コミット: llama.cpp メトリクス対応 / RAG ジョブ完了モーダル修正 /
  README 整備) を pull し、`dgx-spark-api` を再起動して反映した。
  - 再起動方法: sudo が利用できないため旧 uvicorn プロセスを kill し、systemd の
    `Restart=always` による自動再起動に委ねた (active (running) を確認)。
  - 検証: `/api/vllm/status` (稼働中モデル qwen38flashnextexl3_3p05・メトリクス正常返却)、
    `/api/rag/status`、`/api/metrics` とも 200 OK。venv の python で llama.cpp 対応新関数
    (`_is_llamacpp` / `_llamacpp_slots_kv` / `_llamacpp_metrics`) の import を確認。
  - atom1 への影響: llama.cpp プロファイル `muse` (llama-muse-glimmer) の compose
    (/home/cliclie/llm/compose、リポジトリ外) は `--metrics` フラグ未有効のため、稼働時は
    `_is_llamacpp()` 分岐で `/slots` のみから基本状態 (KV キャッシュ使用率) を取得する。
    全メトリクス表示には command に `--metrics` を追加してコンテナ再作成が必要。
  - RAG モーダル修正はブラウザの再読み込みで反映。

## 実装メモ(2026-10-05)

- **表示領域の 80% 縮小** (フロントのみ、API 変更なし)。縮小後のピクセル値は 4 の倍数。
  - 一段目メーター: `.gauge` `flex: 0 0 218px` → **`176px`**、`.gcanvas` `height: 96px` → **`76px`**。
    176px × 7 枚 + gap 10px × 6 = 1292px で main 内側 (1360px) に収まるため **7 枚/行** (従来 6 枚/行)。
    カード高さは 154.8px → 134.8px (border-box 実測換算)。
  - 二段目グラフ: `.chart-card .cwrap` `height: 360px` → **`288px`**。凡例 (font 11・1 行) と
    x 軸ラベル (font 10) がプロット領域 (~243px) に重ならない高さを確保。あわせて
    `scales.x.ticks.maxTicksLimit` を 12 → **10** に削減 (ラベル密度低下)。
  - 中央値フォント: `.gval` `font-size: 22px` → **`16px`**、`.gval .unit` `12px` → **`11px`**。
    150×76 の canvas では 22px 文字の隅 (径 ≈56px) が外側アーチ帯 (54〜65px) と交差するため、
    16px (隅の径 ≈53.3px) に縮小してアーチ帯外に回避。
- **二重メーター (ストレージ負荷 / ネットワーク負荷) の値表示をゲージ下へ移動**:
  縮小後の canvas (150×76) では 2 行テキスト (幅 90px 前後) が内側アーチと交差する領域を
  幾何的に回避できない (テキストが中心軸を跨ぐため径 3〜53px 全域を占める)。
  - `.gauge.dual .gcanvas { height: 56px }` を追加 (単一メーターとカード高さを揃える 145.4px)。
    R = min(150/2, 56) - 6 = 50 → アーチ帯 (外 38〜46 / 内 29〜35 / ゾーン 47〜50) が canvas 内に収まる。
  - `Gauge` コンストラクタで dual は値要素を `.gcanvas` ではなくカード内 (`.gsub` の直前) に
    `insertBefore` し、クラス **`gval-dual-out`** (font 11px / line-height 1.3 / margin-top 2px) を適用。
    従来の `.gval-dual` (`top: 77%`) は削除。`.unit` / `.gval-line` の各セレクタに
    `.gval-dual-out` 系を追加。`buildHostCards()` は dual カードに `dual` クラスを付与。
  - 内外アーチの間隔 (gapAB) は 3px → **1px** に縮小 (テキストが canvas 外に出たため交渉なし。
    外側帯 38〜46 / 内側帯 31〜37 / ゾーン帯 47〜50、ストーク間隔 1px)。
- 仕様側の記述も整備: 「表示スタイル(ゲージ)」の 2 値項目の値位置 (ゲージ直下・アーチ間隔 1px)
  と現在値フォント 16px、「表示レイアウト(4段構成) 一段目」の固定幅 (176px / 7枚/行) を
  現行実装に更新 (旧「220〜260px」の実装イメージ例は削除)。
- 検証: 単一メーター (150×76) と二重メーター (150×56) でアーチ帯と文字バウンディングボックスの
  交差を径方向計算で確認 (単一: 値 16px の最大径 53.3px < 外側帯内縁 54px、サブ行 48.4px < 54px /
  二重: 全アーチ帯が canvas 内で、頂部 y=4px、テキストは canvas 下)。
  稼働中の `dgx-spark-api` (port 8080) から配信される HTML に新値 (176px / 76px / 56px / 288px /
  `gval-dual-out`) が含まれることを curl で確認。フロントはブラウザのリロードのみで反映。

## 実装メモ(2026-10-06)

- **RAG 起動ジョブログのヘッダー行重複表示を修正**: 「RAG 起動」モーダルの実行画面で
  「=== 起動中です。直近4件のログを表示中 ... ===」行とログが複数フレーム分重複表示される不具合を修正。
  2026-08-30 に `api/vllm.py` の `job_status()` へ入れた「最後の `=== ... ===` ヘッダー行以降の
  1フレーム分(最大5行)のみを返す」ロジックが `api/rag.py` の `job_status()` に入っておらず、
  排他環境 (atom2) の RAG 起動は `switch_models.sh rag` を経由するため同じ原因
  (`display_frame()` が端末上書き用にヘッダー+ログ4行のフレームを ANSI カーソル移動付きで毎秒出力し、
  stdout リダイレクト後は各フレームが新規行として追記される)で末尾12行窓に複数コピーが残っていた。
  `api/rag.py` に vllm.py と同一のロジックを追加(スクリプト側・フロント側は変更なし)。
  完了時は「=== 起動完了: rag ===」+ 起動処理時間 + API/Models 行のブロックが表示され、
  `===` 行のない `compose up -d` のみの従来ジョブ(dgx-spark 環境)は挙動不変。
  検証: 実物ログと同形式(ANSI/`\r` 込み)のダミーログによる隔離テスト3ケース(起動中3フレーム/
  完了ブロック+compose 出力/ヘッダーなし) + 実環境での実切替(atom2: qwen38gguf 切替 → RAG 起動)で
  ポーリングごとにヘッダー1行+直近4ログ行のみとなることを確認。RAG は healthy に復旧済み。

- **ネットワーク負荷ゲージの上限を実測リンク速度に変更**: 従来は 100% 相当の上限が
  `api/config.py` のプラットフォーム固定値 (dgx-spark 10000 / atom2 1000 Mbps) で、
  atom2 は 1Gbps 固定表示になっていた。これを NIC の実リンク速度基準へ変更する。
  - `api/metrics.py`: `link_speed_mbps()` を新規追加。デフォルトルート IF
    (既存 `_default_iface()` = `/proc/net/route` から導出、`br-*`/`veth*`/`docker0` は対象外) について
    1. `/sys/class/net/<iface>/speed` を優先読取 (root 権限不要。link down 時は 0/エラーなのでその場合は次へ)
    2. 取れなければ `ethtool <iface>` の `Speed:` 行をパース (`1000Mb/s` / `2.5Gb/s` 形式に対応)
    3. どちらも失敗時 `None`
    2 秒間隔のポーリングで毎回 ethtool を叩かないよう **TTL 10 秒のキャッシュ**付き
    (リンク再ネゴシエーションには最大 10 秒で追従)。併せて `net_gauge_max_mbps()`
    (実測値、無ければ `config.NET_MAX_MBPS` にフォールバック) を追加。
  - `/api/metrics` の返却に `net_link_mbps` (実測値、不明時 `null`) と
    `net_max_mbps` (ゲージ上限として使う値 = 実測 or 既定値) を追加。
  - `api/main.py` の `GET /api/platform` は `config.info()` の `net_max_mbps` を
    実測リンク速度で上書きして返す (初回描画前の表示も実測基準に合わせる)。
  - `api/config.py`: `NET_MAX_MBPS` の値自体は据え置きとし、コメントを
    「実リンク速度が取れない時のフォールバック既定値」に更新。
  - `front/index.html`: 既存の「上限の実測値補正」機構 `tuneGauge()` に `g-net` を追加
    (`m.net_max_mbps` が来たら `d.max` を更新して再描画)。ゲージ下のラベルは
    `sub: () => ...` (クロージャで固定値 `netMax` を参照) から `sub() { ... this.max ... }`
    のメソッド記法に変更し、上限変更表示に追随させる。
  - 検証 (whitebearatom2 実機): `ethtool enx8ca682716b1e` = `Speed: 10000Mb/s` に対し
    `/api/metrics` が `net_link_mbps=10000` / `net_max_mbps=10000`、`/api/platform` が
    `net_max_mbps=10000` を返すことを確認 (atom2 は実 10G リンクのため 100% = 10 Gbps 表示に)。
    API 変更のため `sudo systemctl restart dgx-spark-api` 後 active を確認。
    フロントは `<script>` 全ブロック (7 本) の構文チェックを gjs (SpiderMonkey) で実施。
    ※ 作業途中、`g-net` 定義行のオブジェクト閉じ `}` を欠落させページがほぼ空白に
    (構文エラーで `<script>` 全体が未実行 → `PLAT is not defined`) なったため、
    フロント編集後は必ず構文チェックとブラウザ表示確認を行うこと。

## Magnitude (magn) 切替運用 (2026-10-06)

- Magnitude はホストインストールの deb (systemd user unit `magn-headless.service` の headless serve、port 10100)。
  compose プロファイルではないため `~/LLM/compose/docker-compose.yml` にプレースホルダ service
  (`magn`、alpine sleep、port 10100) を置き、profiles 対応表に `magn` を表示する。実稼働は systemd serve で、コンテナは立たない
- 稼働判定は API 応答のみ (`api/vllm.py` の `_magn_active()`)。api service は root で稼働するため
  `systemctl --user` は cliclie の unit を見ない。`/health` と `/metrics` は Magnitude に存在しない
  → 健全性は `/inference/v1/models` + API key、サーブ中モデル名は `MAGN_MODEL_ID` (既定 `qwen3.8-27b:gguf:q6`)
- 切替は `switch_models.sh magn` が systemd serve 起動 + `magnitude models load MAGN_MODEL_ID` を行う
  (Q6 のみ運用。Q4/gemma は gfx1201 Vulkan の repack qualify を通さないため削除済み)
- `.env` の MAGN_* (MAGN_PORT / MAGN_BASE_PATH / MAGN_API_KEY / MAGN_MODEL_ID / MAGN_SYSTEMD_UNIT) を
  `api/config.py` が読む。切替完了判定は `sw.ready` (API 応答)。Magnitude は running が常に false なので
  フロントの切替対象表示は `sw.ready` を優先する (`front/index.html` 1006行附近)
- 検証 (2026-10-06): `GET /api/vllm/status` で active=magn (health true、model_name=qwen3.8-27b:gguf:q6)、
  `POST /api/vllm/switch {profile:magn}` で切替完了 (switching None → active=magn) を確認

## Magnitude (magn) 表示の不整合修正 (2026-10-06)

qwen3.8-27b:gguf:q6 (magn) を追加後、vLLM/モデル段の表示に不整合があったため修正。

- **稼働モデル名称の整合**: モデルボタン押下後の一覧は profile 名 (`magn`) を出す一方、稼働モデル表示は
  `model_name` (`qwen3.8-27b:gguf:q6`) を出していて不一致だった。`front/index.html` の稼働モデル表示
  (`v-model`)・停止ダイアログ (`stop-msg`)・ログタイトル (`log-title`) を **profile 名に統一**し、
  モデルボタン一覧と同名にした。
- **稼働時間**: Magnitude はコンテナを立てないため `docker inspect` の uptime が常に None だった。
  `api/vllm.py` の `_magn_uptime_str()` が systemd user unit `magn-headless.service` の
  `ActiveEnterTimestamp` から稼働時間を算出する。api service は cliclie で稼働するがユーザセッションバスが
  無いため、`dgx-spark-api.service` に `Environment=XDG_RUNTIME_DIR=/run/user/1000` を追加し
  `systemctl --user` / `journalctl --user` が接続できるようにした (テンプレート + インストール済み unit)。
- **ログボタン**: Magnitude はコンテナの docker logs が無い。`api/vllm.py` の `get_log()` と
  `get_status()` の magn 分岐で `journalctl --user -u magn-headless.service` を読むようにした。
- **実行/待機 ～ 出力スループット**: Magnitude に `/metrics` は無い。`api/vllm.py` の `_magn_metrics()` が
  GPU 負荷 (`metrics._read_gpu()` の `gpu_load_pct` が閾値 10% 超) から実行中 (1/0) を推定し、待機は単一要求で 0。
  KVCache/E2E/TTFT は取得源なし (None → フロント "-" 表示)。スループットはカウンタ/ゲージが無いため
  **held ロジック (`_apply_tps`) で前回値を維持** (atom1 と同じ挙動)。新規計測は None。
- 検証 (2026-10-06): `GET /api/vllm/status` で active=magn (uptime_str=00:2x:xx、running=1/0 が推論中の
  GPU 負荷 80-100% で 1、logs 30 件)。`GET /api/vllm/log?profile=magn` が journal 行を返すことを確認。
  フロントは `<script>` 全 7 ブロックを gjs (SpiderMonkey) で構文チェック OK。api service 再起動後 active。

## Magnitude (magn) の Cline 接続・外部公開設定 (2026-10-06)

qwen3.8-27b:gguf:q6 を Cline で使う際 `http://whitebearatom2.local:10100/v1/` が反応しなかった。原因は 3 つ。

1. **base path**: Magnitude は `/inference/v1/` でサーブする (`/v1/` は 404)。`api/vllm.py` の
   `get_cline_config()` は magn に対して base_url を `http://<host>.local:<port>/v1/` で生成していた
   → `config.MAGN_BASE_PATH` (`inference/v1`) を使うよう修正。
2. **API key**: `network.requireApiKey=true` で **ループバック以外の機器からのアクセスは API key 必須**
   (`Authorization: Bearer <key>`)。Cline 設定は「認証なし(任意の値でOK)」を返していた → magn は
   `config.MAGN_API_KEY` を返すよう修正 (localhost は key 不要、LAN 機器は必須)。
3. **Host ヘッダ検証**: Magnitude は Host ヘッダを検証し `whitebearatom2.local` を **421 Invalid Host
   header** で拒否 (許可: IP・ローカル名・`*.ts.net`・`config.json` の `network.allowedHosts`)。
   → `~/.magnitude/config.json` の `network` に `allowedHosts: ["whitebearatom2.local"]` を追加し、
   `systemctl --user restart magn-headless.service` で反映。`.local` が Host として通るようになった。
   - バインドは `0.0.0.0` (IPv4 のみ)。`.local` は mDNS で IPv6 にも解決されるが IPv6 listen は無い
     (IPv4 経由で Host ヘッダ検証を通す)。
4. **context**: compose の command にコンテキストフラグが無い magn は `/inference/v1/models` の
   `context_length` (262144) を Cline 設定に補完 (`_magn_context_length()`)。

Cline 設定値 (稼働モデル=magn) の正しい値:
- API Provider: OpenAI Compatible
- Base URL: `http://whitebearatom2.local:10100/inference/v1/`
- API Key: `MAGN_API_KEY` (Bearer)。LAN 機器からのアクセスでは必須
- Model ID: `qwen3.8-27b:gguf:q6` / context 262144

検証 (2026-10-06): `GET /api/vllm/cline` が base_url=`.../inference/v1/`・api_key=MAGN_API_KEY・
context_max=262144 を返すことを確認。`POST http://whitebearatom2.local:10100/inference/v1/chat/completions`
(Bearer key) が 200 で生成を返すことを確認 (Host 検証 421 → allowedHosts 追加後 200)。

### API key の指定 (2026-10-06)

Magnitude の API key は `~/.magnitude/config.json` の `network.apiKey` に保存される (自動生成ではなく編集可能)。
指定値 `c3f3437cb524c2564fb014919d09d868` に変更し、`systemctl --user restart magn-headless.service` で反映。
併せて `~/LLM/compose/.env` と `magn.env.example` の `MAGN_API_KEY` を同一値に更新 (DGXSparkUtil の健全性
判定・Cline 設定が読む)。検証: 旧キー 401 / 新キー 200、`GET /api/vllm/cline` が新キーを返す。

## Magnitude (magn) の KVCache・E2E・TTFT・スループット表示 (2026-10-06)

Magnitude に `/metrics` は無いため KVCache・E2E・TTFT・入出力スループットが全て "-" だった。
リクエスト統計が `~/.magnitude/serving-usage.sqlite` の `usage` 表 (`input/cached/output/generation_ms/
first_token_ms/completed_at/model`) に保存されていることを発見し、そこから算出するよう `api/vllm.py` を拡張。

- `config.MAGN_USAGE_DB` (既定 `~/.magnitude/serving-usage.sqlite`、`MAGN_USAGE_DB` で上書き可) を追加。
- `_magn_usage_stats(model_id)`: usage 表の直近 `complete=1` の完了リクエストを読み取り専用で取得。
- `_magn_metrics(port, model_id)` の算出:
  - E2E = first_token_ms + generation_ms (秒)
  - TTFT = first_token_ms (秒)
  - 入力スループット = input / (first_token_ms/1000) (プリフィル=初回トークンまでの時間)
  - 出力スループット = output / (generation_ms/1000)
  - KVCache 使用率 = (input + output) / context_length * 100 (context_length は API から)
  - 実行中 = GPU 負荷 (gpu_load_pct > 10%)、待機 = 0 (単一要求)
  - 新規完了 (`completed_at` 変化) が無ければ `*_tps_held=True` → フロント灰色 (atom1 と同じ保持挙動)
- 検証 (2026-10-06): 稼働モデル=magn で `GET /api/vllm/status` が kv/e2e/ttft/tps_in/tps_out を返す
  (例: prompt 9425+output 1871 で kv 4.31%・e2e 56.2s・ttft 2.05s・入力 4608 tok/s・出力 34.5 tok/s)。
  新規完了時に値が更新され held が切り替わることを確認。api service 再起動後 active、server.log にエラーなし。

## E2E・TTFT・スループットの前回値保持をフロント側へ移行 (2026-10-06)

運用要求: E2E・TTFT・入力スループット・出力スループットは、ポーリング時の更新値が 0 や空値(null)のとき
前回値を保持して表示する。従来はスループットのみバックエンド(`api/vllm.py` の `_apply_tps` と `*_tps_held`)で
保持していた。E2E・TTFT に拡張しつつ、保持をフロント側へ移し、バックエンドの保持ロジックを撤去した。

- **バックエンド(生値返却)**:
  - `_last_metrics` / `_apply_metric`(および従来の `_last_tps` / `_apply_tps`)を削除。`_tps_of()` は差分 tok/s の
    生値(0/None を含む)を返すだけ。`*_tps_held` / `*_held` フラグは返さない。
  - vLLM / SGLang / Magnitude / TabbyAPI / llama.cpp の各メトリクス生成で E2E・TTFT・入出力スループットを
    算出値そのまま返す。使われなくなった `_magn_last_completed_at`(global)・`last_id_before` と held 言及の docstring を掃除。
  - `_reset_token_state()` はトークンカウンタ差分のみ初期化(保持値の概念は廃止)。
- **フロント(前回値保持)**:
  - `front/index.html` に `heldMetrics`(e2e/ttft/in/out)と `heldProfile` を追加。`pickMetric(cur, key)` は
    算出値が非ゼロなら保持値を更新して返す。0/null は保持値を返す(4 値共通)。
  - `renderVllm` で E2E・TTFT・入力/出力スループットを `pickMetric` 経由で表示。稼働モデル消失時と稼働 profile 変化
    (モデル切替)時に保持値・白/灰状態(`tpsLastUpdate`)をリセットし、エンジン間の非連続な値が混ざらないようにする。
  - スループットの白/灰判定は「API が非ゼロの新しい計測値を返した」側を白(相対最終更新判定)。`*_tps_held` 依存を撤去。
- **トレードオフ**: フロント側保持はブラウザ内存のみ。ページリロードで保持値はリセットされる(バックエンド保持と異なり
  リロード後も残らない)。API を直接見るツール(curl 等)は 0/null の生値が見える。保持は表示層にのみ効く。
- **置き換える従来メモ**: 実装メモ 2026-08-27 の「スループットの前回値保持(`_apply_tps`)」、2026-08-31 の
  「held値を灰色表示(`*_tps_held`)」、2026-09-14 の TabbyAPI held、2026-10-06 の Magnitude `*_tps_held` は
  いずれもバックエンド保持に基づく記述であり、本メモでフロント側保持へ置き換えられた(`*_tps_held` は API から消滅)。
- **検証 (2026-10-06)**: バックエンドは SGLang 完了無しで e2e/ttft=None・throughput=0.0 を返し `*_held` キーを持たないことを
  実測(モック pm で `_sglang_metrics` / `_llamacpp_metrics` を呼んで確認)。フロントの波括弧対応を目視検証(`pickMetric` と
  `renderVllm` の閉じ)。JS エンジン(node/deno/bun)は環境に無く、フロント実行検証は未実施。api service は未稼働(手動起動時に反映)。

## 実装メモ(2026-10-07)

- **`qwen38flashnextexl3_4p05`(EXL3 4.05bpw h6/ng6・ポート 8016)追加**:
  `~/llm/compose` 側に TabbyAPI サービス `tabbyapi_flashnext_4p05` が追加された
  (`switch_models.sh` の case と排他停止リスト、`download_models.sh` のダウンロード分岐、
  `compose/tabbyapi/config_4p05.yml`、`compose/qwen38flashnextexl3_4p05.sh` と bashrc alias)。
  設定は 3p05 と**完全同一**(YaRN factor 4.0/1,048,576・`cache_size 1048576`・`cache_mode Q8`・
  `chunk_size 4096`・`max_batch_size 1`・`ngram_ram false`・`vision true`・`reasoning true`・
  `tool_format qwen3_coder`・MTP `draft_num_tokens 8` + `dynamic_draft true`)。量子化は
  `bits 4.05 / head_bits 6 / mtp_bits 4`。vision は 6bit 量子化で `vision_k6.safetensors` に
  分離格納(ExLlamaV3 はモデルディレクトリの `*.safetensors` を glob 走査するため自動読込)。
  `download_models.sh` は 4.05bpw ブランチに欠落している
  `mtp_hyper_connection_mixer_patch.safetensors` を 3p05 からコピーし、YaRN factor 4.0 を
  `config.json` にローカル適用する(3p05 と同一設定にするため必須)。
- **`CLINE_MODEL_TABLE` に 1 エントリ追加**: モデル一覧・切替・監視は `_load_profiles()` が
  compose を動的パースするため**自動追従**し、フロントは profile 非依存なので
  `front/index.html` の変更は不要。追加したのは `api/vllm.py` の 1 エントリのみ。
  - `qwen38flashnextexl3_4p05`: `images=True`(vision: true)・`context_max=1048576`(YaRN 4.0)、
    `max_output_recommended=8192`・`max_output_max=32768`・`temperature=0.6`
  - 反映: `CLINE_MODEL_TABLE` は import 時に読まれるため `sudo systemctl restart dgx-spark-api` が必要
- **単一行 `command:` のパラメータ解析追加(`_service_command_lines`)**: TabbyAPI 系サービスは
  `command: main.py --host 0.0.0.0 --port 5000` の単一行形式であり、従来は折り返しブロック
  (`command: >`)しか解析せず `get_params()` が「command が見つかりません」で失敗していた
  (TabbyAPI 3 サービス共通の既存挙動)。単一行を flag/value ペアへ分解する分岐を追加。
  検証: `qwen38flashnextexl3_4p05` / `qwen38flashnextexl3_3p05` / `qwen38flashnextexl3` で
  `get_params()` が成功(`main.py`・`--host 0.0.0.0`・`--port 5000`)、`qwen38swift` / `mimo9b`
  (折り返しブロック)の出力は変更なし。TabbyAPI のフラグは `EDITABLE_FLAGS`(vLLM/SGLang 用)に
  含まれないため `editable=false`。`get_cline_config()` は単一行に `--max-model-len` 等が無いため
  従来どおり静的テーブルで補完される(動作変化なし)。
- **README のモデル一覧・プロファイル列挙を 17 種へ更新**: `qwen38flashnextexl3_4p05`(ポート 8016)を
  追加し、モデルサービス数を 17 種(vLLM 7 + SGLang 6 + llama.cpp 1 + TabbyAPI 3)に更新。
  併せて `~/llm/compose` の現状を反映: 2026-09-25 に削除された `mimo_flash` をモデル一覧から除去し、
  `glm53flash`(sglang-glm53-flash・8013)と `qwen38flashnext_nvfp4`(vllm-qwen38-flashnext-nvfp4・8015)を
  追加した(プロファイル列挙も同一)。両者は `_load_profiles()` の自動追従で一覧・切替・監視に出るが、
  `CLINE_MODEL_TABLE` のエントリは未追加(`get_cline_config()` は `context_max` を null で返す)。
- **メモリ制約(運用前提)**: 4.05bpw は本体 63.6GiB + ngram ng6 36.4GiB で 3.05bpw 比 +21.5GiB。
  **RAG を別 PC で運用している状態でのみ** MemAvailable 26.1 GiB(ウォーム直後)で起動する。
  RAG を本機で併走させると 6 GiB 前後になり危険域(10 GiB 未満では NVRM OOM 実績あり)。
  `context_max=1048576` の根拠: needle テスト 823,282 token で PASS(prefill 1,317 T/s)。
  実測: decode 47.71 T/s(ウォーム)・prefill 1,032〜1,076 T/s(短文)・MTP 受入 57.7%・coding 8/8。
- **検証 (2026-10-07)**: 稼働モデル `qwen38flashnextexl3_4p05` で `sudo systemctl restart dgx-spark-api` を
  実行し、API 実測で以下を確認。
  - `GET /api/vllm/cline` → `base_url=http://WhitebearATOM1.local:8016/v1/`・
    `model_id=qwen3.8_flashnext_exl3_4p05bpw`・`supports_images=true`・
    `context_recommended=1048576`・`context_max=1048576`・`max_output_recommended=8192`・
    `max_output_max=32768`・`temperature=0.6`（再起動前は `supports_images=false`・
    `context_recommended/context_max=null` だった）
  - `GET /api/vllm/params?profile=qwen38flashnextexl3_4p05` → 単一行 `command` を
    `main.py`・`--host 0.0.0.0`・`--port 5000`（`editable=false`）として表示（従来は 409）
  - `GET /api/vllm/status` → `active=qwen38flashnextexl3_4p05`・port 8016・`health=true`・
    `model_name=qwen3.8_flashnext_exl3_4p05bpw`。`containers` は 17 プロファイルで
    `qwen38flashnextexl3_4p05` を含む（フロントのモデル一覧に自動表示）
  - venv の python で `vllm._load_profiles()` は compose から
    `qwen38flashnextexl3_4p05 → service=tabbyapi_flashnext_4p05 /
    container=tabbyapi-flashnext-4p05 / port=8016` を自動認識し、`get_status()` は
    `health=true`・metrics に needle 実行の prefill 1,317 T/s を返す。
    `get_params()` は 4p05 / 3p05 / 2p05 で成功、`qwen38swift` / `mimo9b`（折り返しブロック）の
    出力は変更なし（回帰なし）。
- **未修正の既知の staleness(今回変更せず)**: `CLINE_MODEL_TABLE` の `qwen38flashnextexl3` と
  `qwen38flashnextexl3_3p05` は `context_max=524288` のまま。両者は YaRN factor 4.0 で 1,048,576
  運用(`config.yml`/`config_3p05.yml` の `cache_size 1048576`、README のモデル一覧は 1048576)。

