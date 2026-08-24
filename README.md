# DGXSparkUtil

DGX Spark のモニタリングとdocker内のvLLMの切替を行う

## 目的

クライアント(別のWindows PC)からブラウザでアクセスし、API経由で以下を行う仕組みを構築する。

1. **システム状態のモニタリング**(DGX Dashboard に相当)
   - メトリクスをビジュアルで表示(半円ゲージ、詳細は「表示スタイル(ゲージ)」を参照)、値の更新はAPI経由
   - モニタリング項目:
     - CPU負荷
     - CPU温度
     - System Memoryの使用率
     - GPU負荷
     - GPU温度
     - ストレージの使用率(使用量・空き容量)
     - ストレージの負荷
2. **dockerで作動中のvLLMの状態モニタリング**
   - 稼働中のモデルコンテナの状態(稼働/停止・稼働時間・健全性)を表示
   - APIの健全性(`/health`)、サーブ中のモデル名(`/v1/models`)、
     vLLM組み込みメトリクス(`/metrics`: リクエスト数・キュー・KVキャッシュ使用率・スループット等)を表示
   - 直近ログの表示
3. **モデルの切替**
   - Web UI から稼働モデルを切替(サーバ側で既存の切替スクリプトを呼ぶ)
   - 既存スクリプト参照: `/home/cliclie/llm/compose/switch_models.sh`(参照のみ。調査フェーズでは実行しない)
4. **稼働モデルに与えているパラメータ値の表示・編集**
   - コンテキストサイズ等のパラメータ値を表示
   - 一部項目は編集可能。編集後はコンテナを再作成して反映

- 対象機材: GIGABYTE AI TOP ATOM (NVIDIA GB10 / Grace Blackwell Superchip)
- 仕組み: クライアントからブラウザでアクセス → ビジュアル表示 → 値の更新はAPI経由

## 表示スタイル(ゲージ)

監視項目はすべて、DGX Dashboard の「System Memory」と同様の**半円ゲージ(半円アーチ)**で表示する。

共通スタイル:
- ゲージ上部に項目名(タイトル)
- 半円アーチを緑(低) → オレンジ(中) → 赤(高)のゾーンで区分
- ゲージ中央に現在値を大きく表示(例: `101.08 GiB`)
- 現在値の直下に総量/容量を表示(例: `121.62 GiB total`)

項目ごとのレイアウト:

| 項目 | ゲージタイトル | 中央(現在値) | 下部(総量/容量) |
|---|---|---|---|
| CPU負荷 | CPU Load | 使用率 % | コア数(例: `20 cores`) |
| CPU温度 | CPU Temperature | 現在温度(例: `38°C`) | 上限(例: `100°C`) |
| System Memoryの使用率 | System Memory | 使用量(例: `101.08 GiB`) | 総量(例: `121.62 GiB total`) |
| GPU負荷 | GPU Load | 使用率 % | — |
| GPU温度 | GPU Temperature | 現在温度(例: `38°C`) | 上限(例: `100°C`) |
| ストレージの使用率 | Storage | 使用量(例: `663 GiB`) | 容量(例: `3.6 TiB total`) |
| ストレージの負荷 | Storage I/O | 読み書き速度(例: `120 MB/s`) | — |

- 色ゾーンの閾値は設定可能(デフォルト: 緑 < 60%、オレンジ 60〜85%、赤 > 85%)
- ストレージの使用率は、下部に空き容量を2行目で表示可能(例: `free 2.8 TiB`)

## 表示レイアウト(3段構成)

ダッシュボード全体は上下 3 段の構成とする。

### 一段目: 現在値ゲージ行

- 各項目のゲージ(半円アーチ)を**横方向に 1 行へ並べて表示**する(左 → 右: CPU Load, CPU Temperature, System Memory, GPU Load, GPU Temperature, Storage, Storage I/O の順)
- 表示領域(ウィンドウ幅)が狭くなれば、**自然に折り返して次の行へ表示する**(1 行に無理に詰めない)
  - 実装イメージ: CSS の flexbox + `flex-wrap: wrap`、各ゲージに固定幅(例: 220〜260px)を割り当て、幅が足りなくなったら折り返す
- 折り返し時にも各ゲージの幅は揃え、間隔を一定にする
- 各ゲージは「表示スタイル(ゲージ)」に定義した共通スタイル(タイトル・色ゾーン・中央に現在値・直下に総量/容量)をそのまま使用する

### 二段目: 時系列グラフ

一段目の下段に、**各項目の時系列値を折れ線グラフとして表示**する(参考: 添付の gpu load 時系列スクショと同様のスタイル)。

- 横軸: 時間(timestamp)、目盛りは「月 日, 年, 時刻」形式(例: `October 8, 2023, 12:00 AM`)
- 縦軸: 測定値(単位は項目ごとに % / °C / GiB / MB/s など)、軸ラベルに単位を明記(例: `gpu load [%]`)
- グラフタイトルは項目名(例: `gpu load`)
- **各項目の線色を変える**(例: CPU Load=緑, CPU Temperature=紫, System Memory=赤, GPU Load=青 など。色は項目ごとに固定しスクショ風の明るい色調とする)
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
  - 直近ログの表示
- セクションヘッダーに「モデル切替・パラメータ編集」ボタンを配置
  - ボタン押下で**ポップアップ(モーダル)表示**する(常設のフォーム欄は持たない)
  - ポップアップ内には以下を収める:
    - **モデル切替**: 対象モデル(profile)の選択 + 「切替」ボタン(サーバ側で `switch_models.sh <profile>` を実行)
    - **パラメータ表示・編集**: 稼働モデルのパラメータ値の一覧表示、編集可能項目の編集 + 「保存」ボタン(編集後はコンテナ再作成して反映)
  - 切替・保存は時間がかかる処理のため、ポップアップ内に進捗/状態を返し、完了後に閉じる(結果は三段目の状態表示に反映)
- 三段目の状態表示は 2 秒毎ポーリングで更新する(モデル切替中は「切替中」状態を明示)

## 調査結果(2026-08-22 実機確認)

**結論: 全項目のモニタリングは可能。**
(read-only なコマンドによる検証。ファイル修正・docker の起動停止は行っていない)

### 機材の特殊性

GPU は **NVIDIA GB10** であり、CPU(Grace)とGPU(Blackwell)が **128GB の統合メモリを共有**する構成。
このため `nvidia-smi` の `memory.total / memory.used` は `[N/A]` となり、独立した VRAM 使用量は取得できない。
→ System Memory(RAM/VRAM)は「統合メモリ使用率」として 1 本で表示するのが実態に忠実。
(vLLM は `--gpu-memory-utilization 0.7 --kv-cache 24G` 等でこのプールを大半占有しているため、
使用率が高めに出るのが正常。必要に応じて vLLM プロセスの RSS を「アプリ占有」として分離表示も可能)

### 各項目の取得方法と実測値

| 項目 | 取得経路 | 実測値(確認時) | 判定 |
|---|---|---|---|
| CPU負荷 | `/proc/stat` の idle 差分(または psutil) | loadavg 0.31 | OK |
| CPU温度 | `/sys/class/thermal/thermal_zone0-6` (acpitz) | 36.8〜39.8℃ | OK |
| System Memoryの使用率 | `/proc/meminfo` (MemTotal − MemAvailable) | 121Gi 中 約102Gi 使用 | 統合メモリとして表示 |
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
  - プロファイル: `nemotron | qwen | laguna | qwen36 | qwen38 | qwen38bf16 | qwen38nvfp4 | muse`
  - 同時に稼働できるモデルは 1 つ(切替時は現モデルを停止)
- `/home/cliclie/llm/compose/docker-compose.yml`
  - モデルサービス 8 種(vLLM 7 + llama.cpp 1)と起動パラメータの定義
- `/home/cliclie/llm/compose/sociax-rag/collect-dgx-info.sh`
  - 既存の read-only 収集スクリプト(hostname / docker / GPU / ポート / 稼働コンテナ / ストレージ)

### モデル一覧と主要パラメータ(docker-compose.yml より)

| プロファイル | コンテナ | ポート | ランタイム | コンテキストサイズ | GPUメモリ使用率 | KVキャッシュ |
|---|---|---|---|---|---|---|
| nemotron | vllm-nemotron120b | 8000 | vLLM 26.07 | 262144 | 0.80 | 4G (fp8_e4m3) |
| qwen | vllm-qwen72b | 8001 | vLLM 26.07 | 32768 | 0.70 | 6G (fp8_e4m3) |
| laguna | vllm-laguna72b | 8002 | vLLM 26.07 | auto | 0.80 | 6G (fp8_e4m3) |
| qwen36 | vllm-qwen36-35b | 8003 | vLLM 26.07 | 262144 | 0.70 | 16G (fp8_e4m3) |
| muse | llama-muse-glimmer | 8004 | llama.cpp | 131072 | - | - |
| qwen38 | vllm-qwen38-27b | 8005 | vLLM (local build) | 262144 | 0.70 | 16G (fp8_e4m3) |
| qwen38bf16 | vllm-qwen38-27b-bf16 | 8006 | vLLM (local build) | 262144 | 0.7 | 24G |
| qwen38nvfp4 | vllm-qwen38-27b-nvfp4 | 8007 | vLLM (local build) | 262144 | 0.70 | 32G (fp8_e4m3) |

- コンテキストサイズ: vLLM は `--max-model-len`、llama.cpp (muse) は `--ctx-size`
- 共通: `--max-num-seqs 1`、`--enable-chunked-prefill`。
  qwen38 系は MTP 推測デコード(`--speculative-config`)、muse は `--temp / --top-p / --top-k`

## 技術基盤(実装アーキテクチャ)

```
[クライアント Windows PC]
      │ ブラウザ http://192.168.0.110:8080
      ▼
┌──────────────────────────────────────────────────────┐
│  AI TOP ATOM (192.168.0.110)                          │
│                                                       │
│  ① 収集 + API  (api/ : Python + FastAPI, psutil)  │
│     GET  /api/metrics        → ホストメトリクス JSON  │
│     GET  /api/vllm/status    → コンテナ状態/健全性/   │
│                                モデル名/metrics       │
│     GET  /api/vllm/params    → 現在のパラメータ値     │
│     POST /api/vllm/switch    → モデル切替             │
│     POST /api/vllm/params    → パラメータ編集+再作成  │
│     データ源: /proc/stat, /proc/meminfo,              │
│      /sys/class/thermal/*, /proc/diskstats,           │
│      nvidia-smi(subprocess), docker CLI(ps/inspect/  │
│      logs), vLLM /health, /v1/models, /metrics        │
│  ② 表示: front/ (単一 index.html + JS)          │
│     3段レイアウト(表示レイアウト(3段構成)参照)   │
│     一段目: 現在値ゲージ行 / 二段目: 時系列グラフ  │
│     三段目: vLLM状態 + 「モデル切替・パラメータ   │
│      編集」ボタン(押下でポップアップ表示)          │
│     fetch を 2秒毎ポーリングしてビジュアル表示     │
│     (既存 vLLM/Qdrant に依存しない独立ポート)      │
└──────────────────────────────────────────────────────┘
```

### フォルダ構成

- `/home/cliclie/DGXSparkUtil/front/` — ブラウザから参照する表示側(静的ファイル: 単一 index.html + JS、時系列グラフ用の Chart.js 単一ファイル `chart.umd.js`)
- `/home/cliclie/DGXSparkUtil/api/` — モニタリング結果を返却するAPI群(Python + FastAPI)

### vLLM関連機能の設計

- UI 配置: 三段目(vLLM 状態モニタリング)に状態を常時表示。「モデル切替」・「パラメータ表示・編集」は
  三段目の「モデル切替・パラメータ編集」ボタンから**ポップアップ(モーダル)表示**する(詳細は「表示レイアウト(3段構成)」参照)

**状態モニタリング**

| 情報 | 取得源 |
|---|---|
| コンテナ稼働/停止・稼働時間・再起動回数 | `docker ps` / `docker inspect` |
| API健全性 | `GET http://<host>:<port>/health` |
| サーブ中モデル名 | `GET /v1/models` |
| リクエスト数・キュー・KVキャッシュ使用率・スループット等 | `GET /metrics` (vLLM 組み込み Prometheus 形式) |
| 直近ログ | `docker logs --tail` |
| ホストへの影響(統合メモリ・GPU負荷) | ホストメトリクス(調査結果参照) |

- llama.cpp (muse) は `/metrics` が無い場合があるため、健全性/モデル名の表示にとどめる

**モデル切替**
- 三段目のポップアップ内の「切替」ボタン → サーバ側で `switch_models.sh <profile>` を実行
- 切替時は現在稼働中のモデルを停止し、新モデルをロード(時間がかかる。スクリプトが所要時間を報告)
- 同時稼働は 1 モデル(既存スクリプトの挙動)

**パラメータ表示・編集**
- 表示: 稼働コンテナの起動引数(`docker inspect` の Config.Cmd/Args)または docker-compose.yml の定義を解析
- 編集対象の例: コンテキストサイズ(`--max-model-len` / `--ctx-size`)、`--max-num-seqs`、
  `--max-num-batched-tokens`、`--gpu-memory-utilization`、`--kv-cache-memory-bytes`、
  muse の `--temp / --top-p / --top-k`
- 注意: vLLM / llama.cpp のパラメータは起動時に確定し、実行中は変更できない
  → 編集後はコンテナ再作成が必要(切替スクリプトの `--force-recreate` フローを流用)
- 編集は対象サービスの command を書き換えてから再作成する

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

```
cd /home/cliclie/DGXSparkUtil/api
./run.sh          # フォアグラウンド起動
./run.sh -d       # バックグラウンド起動 (ログ: api/server.log)
```

- 初回実行時に venv を自動作成し依存(fastapi, uvicorn)を導入する
- アクセス: 同 LAN 内クライアントから `http://192.168.0.110:8080` (ローカルは `http://localhost:8080`)
- ポート変更: `PORT=8090 ./run.sh`
- 常駐は `run.sh -d` (nohup) で運用。systemd 化は必要になったら別途

## 実装メモ(2026-08-24)

- 本 README の設計どおり `api/` (FastAPI) + `front/` (単一 index.html + Chart.js) で実装
- vLLM 26.07 の `/metrics` は全系列がラベル付き(`engine=`, `model_name=`)のため、ラベルを除去して系列名で集約してパースする
- vLLM の `/health` は本文が空 → 健全性は HTTP ステータス(200 系)で判定
- e2e レイテンシは `vllm:request_inference_time_seconds` の sum/count 平均、KV キャッシュは `vllm:kv_cache_usage_perc`
- モデル切替・パラメータ編集はバックグラウンドジョブとして実行し、`GET /api/vllm/job` で進捗(ログ末尾)を取得。同時実行は 1 件
- パラメータ編集は `/home/cliclie/llm/compose/docker-compose.yml` の対象サービス command の値部分を書き換えた後 `docker compose up -d --force-recreate`
- 動作検証時は稼働中の qwen38bf16 への影響を避けるため、切替・コンテナ再作成は実行していない(読み取り系 API のみ検証済み)
- 一段目は半円ゲージ 10 種(Canvas 自前描画)。外側細リングにゾーン帯(緑 <60% / 橙 60〜85% / 赤 >85%)、内側に値アーチ、中央に現在値を表示。IOPS は廃止しストレージ負荷を読込/書込 MB/s の 2 ゲージに分割(上限 12 GB/s)
- ゲージの 100% 基準: GPU 電力は 140 W(GB10 TDP、`nvidia-smi` の power.limit が [N/A] のため固定値)、GPU クロックは実測の `clocks.max.sm` = 3003 MHz。調整は `index.html` の `GAUGE_DEFS[].max` と `NORM` のみ
- 二段目は全 9 系列を 0〜100% に換算して 1 枚の横長統合グラフに集約(CPU 温度=100°C / GPU 電力=140 W / ストレージ I/O=12 GB/s で割って倍率 100)。横軸は「直近30分 / 直近3時間 / すべて」で切替可能
- 時系列バッファは `MAX_POINTS = 4500`(2 秒間隔×3 時間)のリングバッファ。範囲切替は `ts` 基準で配列先頭を切る方式
- `GET /api/metrics` に `cpu_cores`(`os.cpu_count()`)を追加
- タブの favicon は `front/meter-dgx.ico`(`/static/` マウント経由で配信)
- 一段目ゲージのタイトルには二段目グラフの凡例色と同じ色のマーカー(■)を付与し、系列と対応付けやすくしている(GPU クロックはグラフに系列がないためグレー)