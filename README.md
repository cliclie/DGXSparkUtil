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
     - ストレージの負荷(読込/書込。タイトル `DISK(B/s)`)
     - ネットワークの負荷(上り/下り。タイトル `NW(bps)`)
2. **dockerで作動中のvLLMの状態モニタリング**
   - 稼働中のモデルコンテナの状態(稼働/停止・稼働時間・健全性)を表示
   - APIの健全性(`/health`)、サーブ中のモデル名(`/v1/models`)、
     vLLM組み込みメトリクス(`/metrics`: リクエスト数・キュー・KVキャッシュ使用率・スループット等)を表示
      (SGLang は `--enable-metrics` 指定時、llama.cpp は `--metrics` 指定時に `/metrics` を使用、
      TabbyAPI は `/metrics` が無いため docker logs のリクエスト毎統計を解析。
      WSL2 はコンテナを立てないため docker logs の代わりに起動ログファイル末尾を解析)
   - 直近ログの表示
3. **モデルの切替**
   - Web UI から稼働モデルを切替(サーバ側で既存の切替スクリプトを呼ぶ)
   - 既存スクリプト参照: `/home/cliclie/llm/compose/switch_models.sh`(参照のみ。調査フェーズでは実行しない)
   - WSL2 は docker compose を使わないため、汎用ランチャー
     `/home/cliclie/scripts/model-ctl.sh`(14 モードを VRAM 排他切替)を呼ぶ
4. **稼働モデルに与えているパラメータ値の表示・編集**
   - コンテキストサイズ等のパラメータ値を表示
   - 一部項目は編集可能。編集後はコンテナを再作成して反映
     (編集は compose 環境のみ。WSL2 は設定形式がエンジンごとに異なるため**表示のみ**)
5. **GPU サーマルガード**(温度連動の GPU 制限・常駐)
   - GPU 温度に応じて **GB10 は GPU クロック**(`nvidia-smi -lgc`)、**R9700 は GPU 電力上限**
     (`sysfs power1_cap`) を 3 段で制御する
   - root 特権は `thermal-guard.service` だけに限局し、API は `/api/thermal` で状態を読むだけ
   - 詳細は「GB10 サーマルガード」「atom2 サーマルガード」の各節を参照
   - WSL2 は GPU がホスト (Windows) 側ドライバ管理で `-lgc` / `power1_cap` が効かないため
     **導入しない**(`install_service.sh` が自動でスキップ)

- 対象機材: GIGABYTE AI TOP ATOM (NVIDIA GB10 / Grace Blackwell Superchip) および
  WhitebearATOM2 (Intel i9-12900KF + AMD Radeon AI PRO R9700 / ROCm)、
  **Whitebear2026 (WSL2 Ubuntu + NVIDIA RTX 5090 / model-ctl.sh 運用)**。
  プラットフォームは `api/config.py` の自動検出で全環境同一コードで動作する(実装メモ 2026-10-03 参照)。
  GPU サーマルガードも `api/thermal_guard.sh` の自動判定(clock / power)で両機同一スクリプト(2026-10-17 参照)
  - WSL2 はモデル管理に docker compose を使わないため、`backend` キーで
    `compose`(dgx-spark / atom2) と `modelctl`(WSL2: 汎用ランチャー `model-ctl.sh`) を切り替える
    (「WSL2 モデル切替バックエンド modelctl」節参照)
- 仕組み: クライアントからブラウザでアクセス → ビジュアル表示 → 値の更新はAPI経由

## 表示スタイル(ゲージ)

監視項目はすべて、DGX Dashboard の「System Memory」と同様の**半円ゲージ(半円アーチ)**で表示する。
描画は**二段目の時系列グラフと同じ Chart.js**(`front/chart.umd.js`、v4.4.9 をローカル参照)の
`doughnut` で行う(canvas 2D への直接描画は行わない — 実装メモ(2026-10-20)参照)。

共通スタイル:
- ゲージ上部に項目名(タイトル)
- 半円アーチを緑(低) → オレンジ(中) → 赤(高)のゾーンで区分
- ゲージ中央に現在値を大きく表示(例: `101.08 GiB`。現在 `font-size: 13px`)
- 現在値の直下に総量/容量を表示(例: `121.62 GiB total`)
- 2 値の項目(`DISK(B/s)` の R=読込/W=書込、`NW(bps)` の UP=上り/DL=下り)は、1 つのゲージ内に
  外側(=A)/内側(=B)の 2 つのアーチ(ストーク間隔 1px)で別々に表示し、値は**ゲージ中央に 2 行**
  (1 行目=A、2 行目=B。合計値ではない)。数値は単一メーターと同じ 13px 白、ラベルは対応リングの色、
  接頭語(M/G)は 11px グレー。従来は縮小後 canvas(44px) で内側アーチと交差するためゲージ下に出していたが、
  2026-10-26 に canvas を単一と同じ 60px へ拡大して中央配置へ戻した
  (実装メモ(2026-09-27 / 2026-10-05 / 2026-10-26)参照)

Chart.js doughnut での再現方式(実装メモ(2026-10-20)):

- `circumference:180` / `rotation:-90` / `cutout` は穴比率で指定。**dataset 1 本 = 1 同心リング**で
  `index 0` が最外周。`weight` がリング厚み(1 weight ≈ 1px)
- `ds0` = 閾値ゾーン帯(緑/橙/赤ベタ・`borderWidth:0`・厚み 3px)、`ds1` = 単一ゲージの値アーチ
  (ゾーン色)または dual の外側 A(固定色)、`ds2` = dual のみ 内側 B(固定色)
- 値リングの `data=[値%, 100-値%]` の**残り分を `ARC_BG`(背景アーチ色)**にすることで、
  canvas 版の「背景アーチ + 値アーチ」を 1 本のリングで再現する
- リング間 1px は `borderWidth:1` + `borderColor=カード背景 #161b22` の**擬似余白**で出す
  (`spacing` はリング端が引っ込むため不使用)
- 中央の現在値・単位・2 行目は canvas に描かず** HTML オーバーレイ**(`.gval` / `.gsub`)で重ねる。
  canvas は `responsive:false` + 属性サイズ **118×60 固定(単一・dual 共通)**。dual の 2 行値も単一と
  同じ `.gval` に載せ、**数値は 13px 白(`.gnum`)、接頭語は 11px グレー(`.unit`)、ラベル(`.glabel`)は
  対応リングの色**(DISK: R=緑/W=黄、NW: UP=青/DL=紫)。2 行目は RAM/VRAM の `subVal` と同じ
  `.gval-sub` を使うので、1 行目の中心は canvas 内 30px・2 行目は 47px で単一メーターと同じ位置に揃う
  (2026-10-26 に dual の canvas を 44px → 60px へ拡大し、ゲージ下へ出していた `.gval-dual-out` は廃止)

項目ごとのレイアウト:

| 項目 | ゲージタイトル | 中央(現在値) | 下部(総量/容量) |
|---|---|---|---|
| CPU負荷 | `CPU 負荷` | 使用率 % | コア数(例: `24 cores`) |
| CPU温度 | `CPU 温度` | 現在温度(例: `38°C`) | 上限(例: `上限 100°C`) |
| メモリ使用率 | `統合メモリ`(GB10) / `RAM` + `VRAM`(atom2・WSL2) | 使用率 % + 2行目に空き量(例: `空き 74.7 GiB`) | 使用/総量(例: `7.7 / 82.5 GiB`) |
| GPU負荷 | `GPU 負荷` | 使用率 % | — |
| GPU温度 | `GPU 温度` | 現在温度(例: `40°C`) | 上限(例: `上限 95°C`) |
| GPU電力 | `GPU 電力` | 現在消費電力(例: `51.5W`) | 上限(例: `上限 600 W`) |
| GPUクロック | `GPU クロック` | 現在クロック(例: `1245 MHz`) | 最大=**HW 上限**(`clocks.max.sm`。例: `最大 3090 MHz`。実装メモ 2026-10-30。サーマルガード発動中のみ `実 X / ロック Y MHz`) |
| ストレージの使用率 | `ストレージ使用率` | 使用率 % | 使用/総量(例: `232 / 1007 GiB`) |
| ストレージの負荷 | **`DISK(B/s)`** | 読込/書込を 2 行(1 行目 `R 120.0M` / 2 行目 `W 45.2M`。数値 13px 白、接頭語 M/G 11px グレー、ラベルは対応リング色) | 上限(DGX Spark / atom2 = `上限 5 GB/s`、WSL2 = `上限 7 GB/s`。実装メモ 2026-10-30) |
| ネットワークの負荷 | **`NW(bps)`** | 上り/下りを 2 行(1 行目 `UP 300.5M` / 2 行目 `DL 1.2G`) | 上限=**実測リンク速度**(例: `上限 10 Gbps`、実装メモ 2026-10-06 参照) |

- 色ゾーンの閾値は設定可能(デフォルト: 緑 < 60%、オレンジ 60〜85%、赤 > 85%)
- ストレージの使用率の下部は使用/総量(例: `232 / 1007 GiB`。総量 1024 GiB 超は `1.20 / 1.82 TiB`)

## 表示レイアウト(4段構成)

ダッシュボード全体は上下 4 段の構成とし、**4 段は 2 つのタブに分割して表示する**(実装メモ 2026-10-11)。
タブバーはヘッダ行に統合し、ヘッダ行の高さ込みで 40px に圧縮している(実装メモ 2026-10-26)。

- **システム**タブ(既定): 一段目(現在値ゲージ行) + 二段目(時系列グラフ)
- **モデル**タブ: 三段目(vLLM / モデル) + 四段目(RAG)
- タブバーは **ヘッダ行内の `.tabs`**(テキスト + アクティブ下線)。`.tab-panel` を `display` で切り替える
  (header 直下の独立行では高さ 45px を専有していたため、2026-10-26 にヘッダ行へ統合した)
- ヘッダ行のモデルタブの横に **モデル選択プルダウン `#hdr-model-select`** と **切替ボタン `#hdr-switch-btn`** を置く。
  プルダウンは LLM 一覧 + **RAG を最下部**(`rag_enabled=false` の WSL では RAG を出さない)。選択値は
  モデルタブ常設一覧の `selectedProfile` と共有する。選択中が**ロード済(現在稼働中)のモデルなら
  ボタンは「停止」**になり、RAG 選択時も稼働中なら「停止」(停止中なら「起動」)になる。
  展開時の一覧で**重み未導入(ロード済みの重みが無い)項目はモデルタブ一覧と同じ灰色 `--muted`** にして
  色で識別できるようにした(実装メモ 2026-10-27)
- プルダウンは**稼働中モデルへ自動追随**する(2026-10-26)。ページを開いたときも、ターミナル側で
  切替わったときも、毎ポーリングの追随で稼働中モデルが選択表示される(稼働中なしは「モデルを選択」)。
  ただし**プルダウン選択 / 一覧クリック / 切替実行から 10 秒間(`HDR_MANUAL_MS`)は追随を止める**ので、
  選んだ対象のまま切替操作ができる。猶予を過ぎると(実効は次のポーリング、最大 2 秒後)稼働中モデルへ
  表示を戻す。選択値はモデルタブ常設一覧と共有しているため、一覧側の選択も同じ条件で戻る
- ヘッダの「切替」ボタンは**表示中のタブを維持したまま**切替ジョブを開始する(2026-10-26: 従来は
  モデルタブへ遷移していた)。進行中はボタンラベルが「切替中/停止中/再作成中」(最大 4 文字・
  省略記号なし)になり、完了で通常のラベルへ戻る(進捗ログ自体はモデルタブのパネルに出る)。ボタン幅は
  `min-width: calc(4em + 18px)`(4 文字分のテキスト + padding 8×2 + border 1×2)+ `white-space: nowrap`
  で固定し、ラベルが変わってもヘッダ行は揺れない
- 非表示タブ(display:none)中のゲージは値が変わっても `chart.update()` が走るだけで視覚的に
  反映されない。システムタブへ戻る際は `redrawSystemTab()` がゲージのキャッシュ(`_last`)を
  無効化して全ゲージを `update()` し直す。ゲージは `responsive:false`(canvas 属性サイズが
  論理サイズ)なのでサイズ復元は不要で、`resize()` は二段目の `mainChart` のみ行う

### 一段目: 現在値ゲージ行

- 各項目のゲージ(半円アーチ)を**横方向に 1 行へ並べて表示**する(左 → 右: CPU 負荷, CPU 温度,
  RAM / VRAM(atom2・WSL2 は 2 枚、GB10 は「統合メモリ」1 枚), GPU 負荷, GPU 温度, GPU 電力,
  GPU クロック, ストレージ使用率, DISK(B/s), NW(bps) の順。末尾に vLLM 情報のテキストカード 1 枚)
- 表示領域(ウィンドウ幅)が狭くなれば、**自然に折り返して次の行へ表示する**(1 行に無理に詰めない)
  - 実装イメージ: CSS の flexbox + `flex-wrap: wrap`、各ゲージに固定幅
    **`.gauge { flex: 0 0 140px }`** を割り当て、幅が足りなくなったら折り返す
    (140px × 10枚 + gap 10px × 9 = 1490px で main 内側 1560px に収まる → **10枚/行**。
    176px → 140px は 80% 縮小。実装メモ(2026-10-05 / 2026-10-26)参照)
- 折り返し時にも各ゲージの幅は揃え、間隔を一定にする
- 各ゲージは「表示スタイル(ゲージ)」に定義した共通スタイル(タイトル・色ゾーン・中央に現在値・直下に総量/容量)をそのまま使用する
- ゲージ行の末尾に **vLLM 情報のテキスト専用カード**(`.gauge.vinfo-card`)を 1 枚追加する。
  ゲージ描画(canvas)は持たず、**文字のみ**で次を表示する:
  `モデル名(値のみ・ラベルなし)` / `ポート` / `状態` / `prefill` / `decode`
  - 値は三段目の `#v-*` と同じ `setV()` 経由で `#vi-*` へミラーする(`V_MIRROR` 対応表)。
    稼働モデルなしの `"-"` クリア、スループットの白/灰(held)、`up`/`down` の色分けも両側で同期される
  - カード内にタイトルは置かない。モデル名はラベルを省略して値だけを表示し、140px のカード幅に
    収めるため 1 行省略(`white-space:nowrap` + `overflow:hidden` + `text-overflow:ellipsis`)、
    フル名は `title` に入れる(`setV()` が設定)。5 行でも既存ゲージカードと高さが揃う
    (flex の stretch で全カード同高になる)
- **GPU サーマルガード発動中**は、監視対象と制限対象のカード枠線を暗め黄 / 暗め赤に着色する。
  対象パネルはプラットフォームで異なる(2026-10-17):
  - atom2 (R9700 / power モード): 監視「GPU 温度」+ 制限「GPU 電力」(250W=黄 / 210W=赤)
  - atom1 (GB10 / clock モード): 監視「CPU 温度」または「GPU 温度」のうち**制限値に
    引っかかった方**(`temp_c` は `max(acpitz, GPU)` なので、その max を生んだセンサー)+
    制限「GPU クロック」(1665MHz=黄 / 1000MHz=赤)。ヘッダのインジケータも
    `クロック制限 1665MHz (CPU 92℃)` のように犯人側を明記する
  復帰 (上限=最大 / クロック解除) と unit 停止時は灰色 (`--border`) に戻る
  (各節の「フロント表示」参照)

### 二段目: 時系列グラフ

一段目の下段に、**各項目の時系列値を折れ線グラフとして表示**する(参考: 添付の gpu load 時系列スクショと同様のスタイル)。

- 横軸: 時間(timestamp)、目盛りは「月 日, 年, 時刻」形式(例: `October 8, 2023, 12:00 AM`)
- 縦軸: 測定値(単位は項目ごとに % / °C / GiB / MB/s など)、軸ラベルに単位を明記(例: `gpu load [%]`)
- グラフタイトルは項目名(例: `gpu load`)
- **各項目の線色を変える**(例: CPU Load=緑, CPU Temperature=紫, 統合メモリ=赤, GPU Load=青 など。色は項目ごとに固定しスクショ風の明るい色調とする)
- **凡例を表示する**(各線の先頭に丸マーカー + 項目名を横に並べる)。現行ラベルは
  `CPU % / CPU ℃ / RAM % / VRAM % / GPU % / GPU ℃ / GPU W / GPU MHz / DISK % / DISK Read / DISK Write`
  (GB10 は RAM/VRAM の代わりに `メモリ %`)。2026-10-26 に `%` を削除したのち、
  **項目名 + 空白 + 単位**の短縮表記へ変更 (値は従来どおり 100% 換算。実装メモ(2026-10-26)参照)
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
  - 稼働モデルなし(`active=null`)のときは「稼働モデル=稼働中のモデルなし」「コンテナ状態=停止中」を表示し、
    ポート・稼働時間・実行/待機・KVCache・E2E・TTFT・prefill/decode は `"-"` にクリアする
    (前回値保持はモデル稼働中のみ。実装メモ 2026-10-08)
  - スループットの表示ラベルは **`prefill`(入力) / `decode`(出力)**。API 側のキー名は
    `prompt_tokens_per_s` / `generation_tokens_per_s` のまま(表示のみ改名。実装メモ 2026-10-11)
- セクションヘッダーに「ログ・パラメータ・Cline設定値・停止」ボタンを配置(押下で**ポップアップ(モーダル)表示**)
  - **実行ログ**: 稼働モデルの docker logs を三段構成(タイトル/ログ表示/閉じる)のモーダルで表示。
    検索(行フィルタ)・再読み込み・`.log` ダウンロードに対応
  - **パラメータ表示・編集**: 稼働モデルのパラメータ値の一覧表示、編集可能項目の編集 + 「保存」ボタン(編集後はコンテナ再作成して反映)
  - **モデル切替はモーダルを持たない**(一覧を vLLM 行の下に常設表示するため重複。実装メモ 2026-10-11):
    `renderModelList()` が `#panel-model-list` へ `.model-item` を描画し、対象モデル(profile)を選択して
    常設の「切り替え実行」(`#panel-switch-go`)でサーバ側 `switch_models.sh <profile>` を実行する。
    実行ボタンは ジョブ実行中 / 未選択 / すでに稼働中のモデル では無効。VRAM 排他警告は `#panel-switch-warn`、
    進捗は `#panel-switch-log`(従来モーダルの 7 行固定高さを踏襲)に出し、完了で非表示化する。
    一覧は毎ポーリングで更新する(現在稼働中=緑 `current`、`switching`/`stopping` も表示)
  - 切替・保存は時間がかかる処理のため、進捗/状態を返し、完了後に閉じる(結果は三段目の状態表示に反映)
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
│     GET  /api/thermal        → GPU サーマルガード状態   │
│                                (/run/thermal/state を読むだけ) │
│     データ源: /proc/stat, /proc/meminfo,              │
│      /sys/class/thermal/*, /proc/diskstats,           │
│      nvidia-smi(subprocess) / amdgpu sysfs,           │
│      docker CLI(ps/inspect/logs), vLLM /health,       │
│      /v1/models, /metrics, /proc/net/dev (差分),      │
│      /sys/class/net/<if>/speed (NIC 実リンク速度)     │
│      magnitude CLI(models status), ~/.magnitude/      │
│      serving-usage.sqlite, journalctl --user          │
│      [modelctl] wsl_modelctl_bridge.sh → model-ctl.sh │
│       (list/switch/stop/params) + 起動ログファイル     │
│  ② 表示: front/ (単一 index.html + JS)          │
│     4段レイアウト(表示レイアウト(4段構成)参照)   │
│     一段目: 現在値ゲージ行 / 二段目: 時系列グラフ  │
│     三段目: vLLM状態 + モデル一覧を常設表示、     │
│      「ログ・パラメータ」はポップアップ表示       │
│     四段目: RAG (sociax-rag) 状態 + 起動/停止      │
│     fetch を 2秒毎ポーリングしてビジュアル表示     │
│     (既存 vLLM/Qdrant に依存しない独立ポート)      │
│  ③ thermal-guard.service (root 専用・両機同一)  │
│     起動時に clock / power を自動判定           │
│     clock(GB10) : nvidia-smi -lgc / -rgc        │
│     power(R9700): sysfs power1_cap 300/250/210W │
│     → /run/thermal/state(0644) を ① が読む      │
└──────────────────────────────────────────────────────┘
```

### モデル管理バックエンド (2 系統)

同一コードベースで、モデルの起動・切替の仕組みだけ 2 系統を持つ
(`api/config.py` の `backend` キーで分岐。実装メモ 2026-10-21):

| backend | 対象 | プロファイル表の源 | 切替の実行体 |
|---|---|---|---|
| `compose` (既定) | dgx-spark (atom1) / atom2 | `docker-compose.yml` を動的パース | `switch_models.sh <profile>` |
| `modelctl` | WSL2 (Whitebear2026) | `model-ctl.sh` のモード一覧 (ブリッジ経由) | `model-ctl.sh` の `_mc_launch <mode>` |

- API の待ち受けポートはホストごとに `install_service.env` で決める
  (dgx-spark / atom2 = 8080、WSL2 = **8090**。Strata 系が 8080 を占有するため)
- `modelctl` は docker を要求しない (TabbyAPI / ExLlamaV3 / Strata / llama.cpp は
  ネイティブプロセス、bonsai / swiftqwen38 / mimo のみ docker。いずれも
  `model-ctl.sh` が排他制御するため Web 側は再実装しない)

### フォルダ構成

- `/home/cliclie/DGXSparkUtil/front/` — ブラウザから参照する表示側(静的ファイル: 単一 index.html + JS、
  **一段目の半円ゲージ(`doughnut`)と二段目の時系列グラフ(`line`)共通**の Chart.js 単一ファイル
  `chart.umd.js`。CDN 不使用・ローカル参照)
- `/home/cliclie/DGXSparkUtil/api/` — モニタリング結果を返却するAPI群(Python + FastAPI)。
  加えて `thermal_guard.sh` + `thermal-guard.service`(root 特権の GPU サーマルガード。
  clock=GB10 の GPU クロック / power=R9700 の GPU 電力上限を温度連動で 3 段制御) を含む
  - `config.py` — プラットフォーム自動検出と環境別値(パス・メモリ形態・backend・上限値)の集約
  - `main.py` — FastAPI エントリポイント(エンドポイント一覧は上記アーキテクチャ図)
  - `metrics.py` — ホストメトリクス(nvidia / amdgpu sysfs / /proc 系)
  - `vllm.py` — モデル状態・切替・パラメータ表示。`backend` で compose / modelctl を分岐
  - `rag.py` — RAG (embedding + Qdrant) の状態と起動/停止(compose 環境のみ)
  - `modelctl.py` / `wsl_modelctl_bridge.sh` — **modelctl バックエンド (WSL2)**。
    ブリッジが `model-ctl.sh` を source して非対話化し、モード一覧・切替・停止・
    表示用パラメータをタブ区切りで返す。`modelctl.py` がそれを compose 版と同一の
    プロファイル表形状に変換する (`model-ctl.sh` 本体は変更しない)
  - `install_service.sh` / `dgx-spark-api.service` — systemd 導入。unit は
    `__API_DIR__` / `__USER__` / `__PORT__` / `__KILL_MODE__` / `__LIMIT_NOFILE__` の
    プレースホルダを置換してレンダリングする
  - `install_service.env` — **git 管理外のホスト固有設定**(`PORT` / `KILL_MODE` /
    `LIMIT_NOFILE` / `DGXUTIL_THERMAL`)。WSL2 以外には置かない (無い = 従来動作)。
    `DGXUTIL_THERMAL` は WSL2 でのみ意味があり、`0` / 未指定 = サーマルガードを載せない (既定)、
    `1` = **監視専ら** (発動対象 `THERMAL_CONTAINERS=` を空にした unit を render) で載せる

### vLLM関連機能の設計

- UI 配置: 三段目(vLLM 状態モニタリング)に状態を常時表示。「パラメータ表示・編集」・「実行ログ」・「Cline設定値」は
  三段目のボタンから**ポップアップ(モーダル)表示**する(詳細は「表示レイアウト(4段構成)」参照)。
  **モデル切替だけはモーダルを持たず**、vLLM 行の下の常設一覧 + 「切り替え実行」ボタンで操作する
  (一覧常設によりモーダルが完全な重複となったため廃止。実装メモ 2026-10-11)

**状態モニタリング**

| 情報 | 取得源 |
|---|---|
| コンテナ稼働/停止・稼働時間・再起動回数 | `docker ps` / `docker inspect` |
| API健全性 | `GET http://<host>:<port>/health` |
| サーブ中モデル名 | `GET /v1/models` |
| リクエスト数・キュー・KVキャッシュ使用率・スループット等 | `GET /metrics` (vLLM 組み込み Prometheus 形式) |
| 同上(TabbyAPI) | `docker logs` (リクエスト毎統計の解析、実装メモ 2026-09-14 参照) |
| 同上(llama.cpp) | `GET /metrics` (`--metrics` 指定時、Prometheus 形式) + `GET /slots` (KVキャッシュ使用率) |
| 同上(Magnitude / magn) | `~/.magnitude/serving-usage.sqlite` (usage 表) + GPU 負荷から実行中を推定 (`/metrics` 無し、実装メモ 2026-10-06) |
| 同上(Strata / strata・atom2 compose) | `GET /metrics` (**JSON 形式・Bearer key 必須**。Prometheus ではない) + `/health` (key 不要・Cline 設定の max_context/images 用)。実装メモ 2026-10-28 |
| 同上(modelctl / WSL2) | TabbyAPI=起動ログ解析、Strata=`/metrics/prometheus`(`llamacpp:*` 同名系列)+`/slots`+`[strata] done:` ログ、llama.cpp=`/slots`+`print_timing` ログ、ExLlamaV3=`/health` のカウンタ差分 (実装メモ 2026-10-21) |
| 直近ログ | `docker logs --tail` (Magnitude は `journalctl --user -u magn-headless.service`、modelctl は `logs/ctl-*.log` 等のログファイル末尾) |
| ホストへの影響(統合メモリ・GPU負荷) | ホストメトリクス(調査結果参照) |

- llama.cpp は `--metrics` 指定時に `/metrics` (実行/待機・スループット) + `/slots` (KVキャッシュ使用率) を利用。
  `--metrics` 未有効時は `/slots` のみで基本状態を取得。E2E/TTFT は v0.5.0 では未公開(実装メモ 2026-10-04 参照)
- TabbyAPI は `/metrics` が無いが、docker logs のリクエスト毎統計から実行/待機・KVCache(推定)・
  E2E・TTFT・入力/出力スループットを取得する(実装メモ 2026-09-14 参照)
- **Magnitude (magn) はホストの systemd user serve**(コンテナを立てない)。稼働判定は serve の API 応答
  (`GET /inference/v1/models` + API key)**かつ** `magnitude models status <id>` の `Runtime` 列が `Ready`
  (モデルが VRAM にロード済み)。`/inference/v1/models` はインストール済みモデルを常時列挙するだけで
  ロード状態を反映しないため、API 応答単独では OS 起動直後に未ロードでも「稼働中」と誤表示する
  (実装メモ 2026-10-08)。`/metrics` は無く、ルート `/health` は serve のライフサイクル状態のみを返す。
  メトリクスは `~/.magnitude/serving-usage.sqlite` の usage 表と GPU 負荷から算出し、直近ログは
  `journalctl --user` を読む(実装メモ 2026-10-06)
- **Strata (strata・atom2 compose のみ) は `/metrics` が JSON 形式で Bearer key 必須**
  (Prometheus 系列ではないため既存の `vllm:`/`sglang:`/`llamacpp:` 検出チェーンには入れず、
  `get_status()` の strata 分岐で `_strata_server_metrics()` がマッピングする)。
  `/health` のみ key 不要で Cline 設定値の max_context/images をそこから読む。
  Strata は暫く idle でモデルを **auto-unload** するため (`live.state=unloaded`)、
  `requests_running` を 0 に正規化しスループット・KV も 0 を返す
  (リクエスト直後 5 秒はモジュールレベルキャッシュで計測値を維持。実装メモ 2026-10-28)。
  なお modelctl/WSL2 の `engine == "strata"` は**別物** (llama.cpp 系フォークの
  `/metrics/prometheus` + ログ解析、実装メモ 2026-10-21) で本分岐とは無関係
- **E2E・TTFT・入力/出力スループット(表示ラベルは `prefill` / `decode`)の前回値保持はフロント側で実施**: `api/vllm.py` は算出値(0/None を含む)をそのまま返す。
  `front/index.html` の `pickMetric()` が直近の非ゼロ値を保持し、API が 0/None を返した間は前回値を維持表示する(実装メモ 2026-10-06)。
  稼働モデル消失・モデル切替(profile 変化)で保持値をリセット。スループットの白/灰は「API が非ゼロの新しい計測値を返した」側を白とする相対判定
- **前回値保持はモデル稼働中のみ**: 稼働モデルなし(`active=null`)のときは保持値をリセットするだけでなく、
  `clearVllmMetrics()` がポート・稼働時間・実行/待機・KVCache・E2E・TTFT・prefill/decode を `"-"` に
  戻す(前のモデルの実測値を残さない。実装メモ 2026-10-08)。`#v-*` と `#vi-*`(現在値カード)の両側へ
  同時に反映される(実装メモ 2026-10-11)

**モデル切替**
- 三段目の常設一覧で profile を選択 → 「切り替え実行」ボタン → サーバ側で `switch_models.sh <profile>` を実行
- 切替時は現在稼働中のモデルを停止し、新モデルをロード(時間がかかる。スクリプトが所要時間を報告)
- 同時稼働は 1 モデル(既存スクリプトの挙動)
- パネルのジョブログ(`#panel-switch-log`)は実行中**最新フレーム(ヘッダー1行+直近4ログ行)のみ**を表示する。
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
- **長文推論中の `/health` 未応答は落下ではない**: ExLlamaV3 / TabbyAPI は推論中に uvicorn
  ワーカーが塞がる。`/api/vllm/status` は TCP 接続だけで `busy`(混雑中) を返し、フロントは
  「稼働中（推論中）」を表示する。status はエンジン種別・モデル名・ログ末尾をキャッシュして
  混雑時も 1.6 秒で返す(2026-10-28 節)

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
systemctl status thermal-guard        # GPU サーマルガードの状態確認
journalctl -u thermal-guard -f        # 発動ログ（温度連動の切替）
curl -s localhost:8080/api/thermal    # ガードの状態 JSON
```

- ユニットファイル: `/home/cliclie/DGXSparkUtil/api/dgx-spark-api.service`
  (テンプレート: `__API_DIR__` / `__USER__` / `__PORT__` プレースホルダを `install_service.sh` が
  sed で置換して `/etc/systemd/system/` にインストール)
- **ポートの既定は 8080 のまま**。他サービスと競合する環境は `PORT` で上書きする。
  `sudo` は環境変数を引き継がない (**WSL の sudo は `-E` 非対応**で `sudo: preserving the
  entire environment is not supported, '-E' is ignored`) ため、**リポジトリと同じ
  ディレクトリに置く `api/install_service.env` (git 管理外)** で指定する:
  ```bash
  # /home/cliclie/DGXSparkUtil/api/install_service.env  (WSL2 Whitebear2026 の実例)
  PORT=8090            # Strata 系が 8080 を使うため API は 8090
  KILL_MODE=process    # API 再起動で稼働中モデルサーバーを巻き込まない (下記)
  LIMIT_NOFILE=1048576 # WSL2 の CUDA に必要 (下記)
  DGXUTIL_THERMAL=0    # WSL のみ有効。0/未指定 = 載せない (既定)、1 = 監視専らで載せる
  ```
  → 以後は素の `sudo bash .../install_service.sh` で 8090 が入る。
  ファイルが存在しない環境 (atom1 / atom2) は `PORT=8080` / `KILL_MODE=control-group`
  / `LimitNOFILE` 行なし (systemd 既定 1024) の従来動作のまま
- **`KILL_MODE`**: unit の `KillMode`。既定 `control-group` のままだと、モデルサーバーを
  このサービスの cgroup 内で起動する `modelctl` (WSL) では
  `systemctl restart dgx-spark-api` が稼働中モデルまで SIGKILL する
  (`start_new_session` でセッションは切れるが cgroup は切れない)。WSL は `process` を使う。
  atom1 / atom2 のモデルは docker コンテナ (dockerd の cgroup) なので影響なし
  - 実測: `LimitNOFILE` 適用後のユーザーサービスで `systemctl --user restart` →
    API は 200、稼働中 TabbyAPI (`:5001/health`) も 200 のまま生存を確認
- **`LIMIT_NOFILE` (WSL2 で必須)**: systemd サービスの既定 `LimitNOFILE=1024` では、
  WSL2 の CUDA (`/dev/dxg` + D3D12) がモデルロード中にハンドルを枯渇させて
  `RuntimeError: CUDA driver error: unknown error` になる。
  ログインセッション (`ulimit -n` = 1048576) では同じ手順で成功するため、
  **切り分けは「同一セッションで `ulimit -n 1024` を下げて再現」**で行った
  (cgroup / 環境変数 / setsid は無関係であることを実測で否定)
- **WSL2 の `thermal-guard.service` はオプトイン**（既定はインストールしない）。
  WSL の GPU はホスト側ドライバ管理で `nvidia-smi -lgc` / `power1_cap` が効かないため、
  `/proc/version` が microsoft を含むときは `DGXUTIL_THERMAL=1` を明示したときだけ導入する。
  その際 unit に `Environment=THERMAL_CONTAINERS=`（空）と `Environment=THERMAL_MODE=clock` を
  注入して **発動させない**（`nvidia-smi --query` / sysfs / `docker ps` の読み取りと
  `/run/thermal/state` の更新のみ）。`thermal_guard.sh` 側は空文字を受け取るため
  `${THERMAL_CONTAINERS-既定}`（コロン無し）で展開する。手順は「WSL2 でサーマルガードを
  監視専らで導入する」（2026-10-30 節）参照。dgx-spark / atom2 は従来どおり（未指定で有効、
  `=0` で無効）
- 電源投入時に自動起動（`WantedBy=multi-user.target`）、クラッシュ時は自動再起動（`Restart=always`）
- venv が存在しない場合は `ExecStartPre` で自動作成する
- ログ: `/home/cliclie/DGXSparkUtil/api/server.log`（追記モード。JST 0 時に 0 バイトへ
  truncate → 下記「server.log の日次 truncate」）
- **`install_service.sh` は `thermal-guard.service`（root 特権）も同時にインストールする**。
  `api/thermal_guard.sh` に `chmod 755` して unit をレンダリングし、`enable` + 毎回 `restart`
  （`enable --now` は稼働中 unit を再起動しないため、Environment のしきい値変更が反映されない）。
  WSL2 で `DGXUTIL_THERMAL=1` を指定したときは、このレンダリング時に `Environment=THERMAL_CONTAINERS=`
  （空）を挿入して監視専らにする（`sed` の `^ExecStart=` 置換。`ExecStartPre=` にはマッチしない）
  - `dgx-spark-api.service` は `User=cliclie` のままで、GPU 制限を書けるのは
    thermal-guard の root unit だけ
  - AMD 機（R9700）では、旧 `llm-gpu-powercap.service`（210W 常時固定の oneshot）を
    自動で `disable --now` する（`power1_cap` の書き手を thermal-guard に集約）

### server.log の日次 truncate（JST 0 時）

`dgx-spark-logrotate.timer` が **JST の 0 時**に `api/rotate_log.sh` を実行し、`server.log` を
**0 バイトにする**（アーカイブは作らない。中身は uvicorn のアクセスログだけで行に
タイムスタンプが無く、保存価値がないと実運用で判断した。実測 187 MB は 1.2 MB に圧縮できたが、
その 1.2 MB にも残す値がなかった = 2026-10-26）。

- 方式は **truncate のみ**（リネームしない）。書き手は fd を開いたまま追記し続ける
  （`dgx-spark-api.service` の `StandardOutput=append:server.log`、`run.sh -d` の `>> server.log`）
  ため、リネームだと書込みがリネーム先へ流れ続けて `server.log` は以後増えない。
  truncate は inode/fd を維持するので **API を再起動しない**
- 空ファイル・ログ不在のときは何もしない。`flock` で多重実行防止
  （タイマーと手動実行が重なっても後発は何もしない）

手動運用:

```bash
systemctl list-timers dgx-spark-logrotate.timer        # 次回発火時刻 (JST 0 時) を確認
sudo systemctl start dgx-spark-logrotate.service       # 今すぐ空にする (API は停止しない)
bash /home/cliclie/DGXSparkUtil/api/rotate_log.sh      # 素のスクリプトでも可 (cliclie 権限で)
```

- 日付境界は JST 固定。`OnCalendar` はマシンのローカル TZ で評価され unit の `TZ` 環境変数は
  `ExecStart` にしか効かないため、`install_service.sh` が「JST 0 時 = そのマシンの何時何分」を
  換算して `OnCalendar` に焼き込む（JST 機は `*-*-* 00:00:00`、UTC 機は `*-*-* 15:00:00`。
  日本に夏時間はないので +0900 固定 = 通年同じ）
- `Persistent=true` なのでマシンの停止中に逃した分は起動後に実行される
  （＝停止中に書かれた分はまとめて捨てられる）
- **sudo 不要で導入する方法**: `bash api/install_logrotate_user.sh`。`~/.config/systemd/user/` に
  同内容の unit（`User=` 行を落とした版）を書き、`loginctl enable-linger`
  （DGX Spark 系ではパスワード不要で通った）でブート後も起動させる。
  `server.log` が `cliclie` 所有なので truncate に root 特権は要らない
  - 管理: `systemctl --user list-timers dgx-spark-logrotate.timer` /
    `systemctl --user start dgx-spark-logrotate.service` /
    `journalctl --user -u dgx-spark-logrotate.service -n 5`
  - `install_service.sh`（system unit 版）を入れるときは user timer を解除する
    （`systemctl --user disable --now dgx-spark-logrotate.timer` + unit 削除 + `daemon-reload`）。
    両方が残っても `rotate_log.sh` の `flock` で後発は何もしない

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
- 表示は 2 タブ(システム=現在値+時系列 / モデル=vLLM+RAG)に分割し、タブバーはヘッダ行内。モデル切替は
  モーダルを持たず、vLLM 行下の常設一覧 + 「切り替え実行」と、ヘッダ行のモデル選択プルダウン +
  切替ボタン(ロード済モデル選択時は「停止」)で操作する(実装メモ 2026-10-11 / 2026-10-26)
- 一段目は半円ゲージ 10 種 + vLLM 情報のテキストカード 1 枚(計 11 枚、10枚/行で折り返し)。外側細リングにゾーン帯(緑 <60% / 橙 60〜85% / 赤 >85%)、内側に値アーチ、中央に現在値を表示。IOPS は廃止しストレージ負荷を読込/書込 MB/s の 2 ゲージに分割(上限は DGX Spark / atom2 = 5 GB/s、WSL2 = 7 GB/s。実装メモ 2026-10-30)。ゲージは 2026-10-26 に縦横・文字を 80% 縮小(canvas 150×76/56 → 118×60/44、カード 176px → 140px、中央値 16px → 13px、タイトル 12px → 10px。最小の 11px 文字は据え置き)
- ゲージの 100% 基準: GPU 電力は 140 W(GB10 TDP、`nvidia-smi` の power.limit が [N/A] のため固定値)、GPU クロックは実測の `clocks.max.sm` = 3003 MHz、ストレージ I/O は 5 GB/s(WSL2 のみ 7 GB/s)。調整は `index.html` の `gaugeDefs()` の `max` と `CHART_NORM` のみ。ただし GPU クロックゲージは**サーマルガード発動中に限り目盛りをロック段(1665 MHz)へ寄せる**(`thermalClockLockMhz()`、2026-10-28 節)。二段目の時系列グラフは HW 上限基準のまま
- 二段目は全 9 系列を 0〜100% に換算して 1 枚の横長統合グラフに集約(CPU 温度=100°C / GPU 電力=140 W / ストレージ I/O=5 GB/s(WSL2 は 7 GB/s)で割って倍率 100)。横軸は「直近30分(既定) / 直近3時間 / すべて」で切替可能。見出しは「時系列 (全項目・2秒間隔)」1 行に集約し(左端に見出し・右端に範囲ボタン。副題「CPU / メモリ / GPU / ストレージ (全項目 100% 換算)」は廃止)、グラフ高さは 288px → 232px(80%、2026-10-26)
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
  ※ **本項は 2026-10-08 に置き換え**: 稼働判定と `sw.ready` は「API 応答 + `magnitude models status` の
  Runtime=Ready」に変更 (`_magn_active()` / `_magn_runtime_ready()`)。ルート `/health` は存在するが
  serve のライフサイクル状態のみを返し、モデル未ロードでも Ready になる。
  下記「Magnitude (magn) の OS 起動直後に「ロード済み」と誤表示する不具合修正 (2026-10-08)」参照
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
**実値は git 管理外の `~/LLM/compose/.env` の `MAGN_API_KEY` を唯一の正本**とし、この文書には記載しない
(2026-10-23 に README の実値記載を削除し、履歴からも除去した)。値を回す場合は `.env` と
`~/.magnitude/config.json` を同一値に揃えて `systemctl --user restart magn-headless.service` を実行する。
DGXSparkUtil 側の健全性判定と Cline 設定 (`GET /api/vllm/cline`) は `.env` を読む。
検証: 旧キー 401 / 新キー 200。

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

## Magnitude (magn) の OS 起動直後に「ロード済み」と誤表示する不具合修正 (2026-10-08)

`magn-headless.service` は `enabled` で OS 起動時に自動開始する。`api/vllm.py` の `_magn_active()` は
`GET /inference/v1/models` の応答だけで稼働判定していたが、このエンドポイントは **model store の
インストール済みモデルを常時列挙するだけ**で VRAM へのロード状態を反映しない。そのためモデルを
ロードしていない起動直後でも `active=magn` (health=true, model_name=qwen3.8-27b:gguf:q6) を返し、
フロントが「magn ロード済み」表示になっていた。

実測 (2026-10-08, 起動 3h55m・モデル未ロード):
- `GET /inference/v1/models` → `qwen3.8-27b:gguf:q6` を返す (応答あり)
- `magnitude models status qwen3.8-27b:gguf:q6` → `Runtime  Unloaded`
- VRAM 使用量 326MB / 32GB、`magnitude status` → `Active model  None`
- `GET /api/vllm/status` → `active=magn` ← 誤表示

`switch_models.sh` の `check_health()` は既に「API 応答 + magnitude CLI の Runtime 列」で二重判定しており、
API 側 (`api/vllm.py`) だけ取り残されていた。

- **修正**:
  - `api/vllm.py` に `_magn_runtime_ready(model_id)` を追加。`magnitude models status <id>` を実行し
    `Runtime` 列が `Ready` なら True、`Unloaded`/`Requested`/`Loading`/`Stopping`/`Failed` なら False、
    CLI 実行失敗・出力解析不能なら None を返す。CLI は unix socket RPC で ~0.22s かかるため 2 秒 TTL の
    結果キャッシュ (`_magn_runtime_cache`) を持つ (get_status 内の複数呼び出しも 1 回に集約)。
  - `_magn_active(port)` を「API 応答 **かつ** Runtime=Ready」に変更。判定不能時は従来動作 (API 応答のみ) に
    戻さない。`magnitude` CLI が存在しない環境 (Magnitude 未導入ホスト) では従来の API 応答判定のまま。
  - `_switching_info()` の magn 分岐も `_magn_active()` を使うようにし、切替完了表示の判定を
    `switch_models.sh` の完了判定と一致させた (serve 起動直後を「切替完了」と見なさない)。
  - `api/config.py` / `api/vllm.py` のコメントを実態に合わせて修正。
- **判明した副次的事実**: Magnitude にはルート `/health` が**存在する** (`{"service":"magnitude-acn",
  "state":{"_tag":"Ready"}}`)。ただしこれは serve のライフサイクル状態で、モデル未ロードでも Ready を返す
  ため稼働判定には使えない。`/metrics` は依然 404。ModelResidency の _tag は
  `Unloaded / Requested / Loading / Ready / Stopping / Resident / Failed`。
- **トレードオフ**: 稼働判定に CLI 実行が 1 回増える (~0.22s / 2s ポーリング)。CLI が unix socket
  (`~/.magnitude/state/application.sock`) を読むため api service は magn と同一ユーザー (cliclie) で
  稼働している必要がある (`dgx-spark-api.service` は `User=cliclie` + `XDG_RUNTIME_DIR=/run/user/1000`)。
  serve 起動中・モデル未ロードのときは「稼働モデルなし」表示になる (serve 常駐自体は稼働時間に出ない)。
- **置き換える従来メモ**: 2026-10-06 の「Magnitude (magn) 切替運用」の
  「稼働判定は API 応答のみ (`_magn_active()`)」「`/health` と `/metrics` は Magnitude に存在しない」は
  本メモに置き換え (稼働判定は API 応答 + Runtime=Ready。`/health` は存在するがロード状態は反映しない)。
- **検証 (2026-10-08)**: 実機 (モデル未ロード) で `_magn_runtime_ready()=False`、`_magn_active()=False`、
  2 回目呼び出しはキャッシュで 0.000s。`systemctl restart dgx-spark-api` 後、
  `GET /api/vllm/status` が `active=None` になることを確認 (修正前は `active=magn`)。
  陽性側 (ロード済みなら正しく稼働表示すること) は `Runtime       Ready` を返す `magnitude` の shim を
  PATH 経由で立てて検証し `_magn_runtime_ready()=True` / `_magn_active()=True` を確認 (28.7GB の実ロードは行っていない)。
  再起動後の server.log にエラーなし、`/api/vllm/status` は 200 を維持。

## 稼働モデルなしのときポート〜出力スループットをクリア (2026-10-08)

`active=None` (稼働モデルなし) 表示になった際、フロントの vLLM/モデル パネルは
「稼働モデル」「コンテナ状態」だけを更新し、**ポート・稼働時間・実行/待機・KVCache・E2E・TTFT・
入力/出力スループットを前のモデルの実測値のまま残していた**。前回値保持 (`heldMetrics`) は
「モデル稼働中に 0/null を埋める」ための仕組みで、稼働モデルがいない状態で古い値を表示し続ける
ものではないため、消失時に "-" へ戻す。

- `front/index.html`:
  - `V_METRIC_IDS` (`v-port`, `v-uptime`, `v-rw`, `v-kv`, `v-e2e`, `v-ttft`, `v-tps-in`, `v-tps-out`)
    と `clearVllmMetrics()` を追加。`setV(id, "-")` は className も `"v"` に戻すため、スループットの
    灰表示 (`stale`) も同時に解除される。
  - `renderVllm()` の `if (!a)` 分岐で、既存の `heldMetrics` / `heldProfile` / `tpsLastUpdate`
    リセットに続けて `clearVllmMetrics()` を呼ぶ (従来は保持値のメモリ上のリセットだけで、
    DOM の表示は更新されていなかった)。
  - 稼働中 (`active` あり) の前回値保持ロジック (`pickMetric`) はそのまま。モデル切替時
    (`a.profile !== heldProfile`) のリセットも従来どおり、次のポーリングで新モデルの値が描画される。
- **検証 (2026-10-08)**: JS エンジン (node/deno/bun) が無い環境のため、`index.html` から
  `renderVllm` を含む Tier-3 script ブロックをそのまま抜き出したテストページを作り、
  `firefox --headless --screenshot` で実行結果 (各要素の textContent と className) を読取り確認。
  - ACTIVE (値あり): port=10100 / uptime=00:10:00 / rw=1 / 0 / kv=12.3 % / e2e=4.5 s / ttft=0.42 s /
    in=300.0 tok/s / out=25.5 tok/s
  - HELD (稼働中・メトリクス 0/null): e2e=4.5 s / ttft=0.42 s / in=300.0 / out=25.5 が前回値を維持 (従来動作を維持)
  - NONE (`active:null`): **port / uptime / rw / kv / e2e / ttft / in / out すべて "-" (className="v")**、
    稼働モデル=「稼働中のモデルなし」、コンテナ状態=「停止中」

## UI タブ化・現在値の vLLM テキストカード・prefill/decode 表記・モデル一覧の常設表示 (2026-10-11)

要件: (1) 「現在値・時系列」と「vLLM/モデル・RAG」を添付(Home/Updates/Settings)風のタブ切り替えにする
(2) 現在値のゲージ行に vLLM のモデル名・ポート・コンテナ状態・入出力スループットを**文字だけ**表示する
(3) 入力スループットは `prefill`、出力スループットは `decode` と表記する
(4) タブ分け後の vLLM/モデル 行の下に、切替ポップアップの一覧を表示しそこから切替できるようにする

- **タブ化** (`front/index.html`):
  - header 直下に `.tabs`(システム / モデル)、`<main>` 内を `#tab-system`(現在値+時系列) と
    (※ 2026-10-26 に `.tabs` はヘッダ行内へ移動した。実装メモ 2026-10-26 参照)
    `#tab-model`(vLLM+RAG) の `.tab-panel` に分割。既定はシステム。
    `.tab-panel { display: none }` / `.tab-panel.active { display: block }` で切り替える
  - 非表示タブ内の canvas は `clientWidth=0` のため `Gauge.render()` がスキップされる。
    poll 時に `lastMetrics` へ直近 metrics を保持し、システムタブ復帰時に `redrawSystemTab()` が
    ゲージの `_last` を無効化して `renderGauges()` + `mainChart.resize()` + `refreshMainChart()` を実行する
- **現在値のテキストカード** (要件2):
  - `buildHostCards()` の innerHTML 末尾に `VINFO_CARD`(`.gauge.vinfo-card`) を連結。
    canvas を持たず `モデル名 / ポート / コンテナ状態 / prefill / decode` を文字表示する
  - ID は vLLM 行と衝突させないため `#vi-*`。`setV()` に `V_MIRROR` 対応表を追加し、`#v-*` へ書いた
    textContent と className を `#vi-*` へも同じ値で流し込む → `clearVllmMetrics()` の "-" クリア、
    held の灰(`stale`)、`up`/`down` の色分けも自動的に両側同期される(追加の分岐は不要)
  - モデル名は `display:block` + `line-height:1.3` + `max-height:2.6em` + `overflow:hidden` で 2 行上限。
    `-webkit-line-clamp` は Firefox で `display` が `flow-root` に解決されて幅制約が効かず、
    カード幅 176px を超えて描画がはみ出したため不採用(`title` にフル名を持たせる)
- **表記変更** (要件3): vLLM 行の `.k` を `入力スループット`→`prefill`、`出力スループット`→`decode`。
  API 側のキー名(`prompt_tokens_per_s` / `generation_tokens_per_s`)と held/白灰ロジックはそのまま
- **モデル一覧の常設表示** (要件4):
  - `#vllm-panel` の `.btns` の直下に `#panel-switch-warn` / `#panel-model-list` /
    `#panel-switch-go` / `#panel-switch-log` を追加
  - `renderModelList()` を「1 品目の生成(`makeModelItem()`)」と「描画先ループ」に分離し、
    `#model-list`(モーダル)と `#panel-model-list`(パネル)へ同じ `.model-item` を描画する。
    `selectedProfile` を共有するのでモーダル・パネルどちらからでも同じ対象を切替できる
    (現在稼働中=緑 `current`、`switching`/`stopping` の状態表示もそのまま流用)
    ※ この段階ではモーダルと併存。下記の廃止でパネル単一描画になった
  - `updateSwitchButtons()`: ジョブ実行中 / 未選択 / すでに稼働中の profile では「切り替え実行」を無効。
    ボタン文言は選択時に「切り替え実行: <profile>」
  - 切替実行は `startSwitch(btnId, logId)` に共通化(従来 `#switch-go` にインラインだった処理を関数化)
  - `JOB_LOG_IDS` を配列化し `jobLogEls(kind)` で解決。`pollJob()` の進捗をモーダルとパネル両方へ書き、
    `onJobDone()` は switch 完了時に `#panel-switch-log` を閉じる(結果は状態表示に反映される)
  - poll の `renderModelList()` は「モーダル表示中のみ」の条件を撤去し毎ポーリングに変更(パネル常設のため)
  - 排他警告は `style.display` を `""` ではなく `"block"` にする(`#panel-switch-warn` は ID セレクタで
    `display:none` のため、inline を空にすると CSS の none が残って表示されなかった)
- **モデル切替モーダルの廃止** (同要件の続報): 一覧・警告・進捗ログがパネル側に出揃い
  `#modal-switch` が完全な重複になったため撤去した。
  - HTML: `#btn-switch`(「モデル」ボタン)と `#modal-switch` 全体を削除。vLLM 行のボタン列は
    「ログ・パラメータ・Cline設定値・停止」の4つになった。残るモーダルは params/cline/log/stop/rag の5種
  - CSS: `#switch-log`(7行固定)を削除し、`#panel-switch-log` へ `line-height:14px; height:116px` を移設
    (パネル側も従来同様 7 行固定で、切替中のレイアウト揺れを防ぐ)
  - JS: `renderModelList()` の描画先・警告を `#panel-model-list` / `#panel-switch-warn` の単一参照に簡素化。
    `btn-switch` / `switch-close` ハンドラを削除。2箇所共用のために関数化していた `startSwitch(btnId, logId)` は
    呼び出し元が1つになったので `#panel-switch-go` のインラインハンドラへ戻した。
    `JOB_LOG_IDS.switch` は `["panel-switch-log"]`。`onJobDone()` の `#switch-go` 復元は撤去
    (実行ボタンの可否は `updateSwitchButtons()` が毎ポーリングで判定するため不要)
  - 検証: 同一の検証ページで `items=5 sel=OK selected=qwen38radiance go.disabled=false` を確認。
    `btn-switch` / `modal-switch` / `switch-go` / `switch-log` / `switch-warn` / `model-list` / `startSwitch`
    への残存参照がゼロであることも grep で確認

- **検証 (2026-10-11)**: JS エンジン(node/deno/bun)が無い環境のため、起動 IIFE を同期 XHR 版に置換した
  検証専用ページ `front/_v_new.html` を作り、`firefox --headless --screenshot` で実描画と DBG 行を読み取り確認
  (検証ファイルとスクショは確認後に削除済み)。
  - システムタブ: ゲージ 11 枚 + `vLLM / モデル` カード。
    `vi-model=[稼働中のモデルなし] vi-port=[-] vi-status=[停止中] vi-prefill=[-] vi-decode=[-]`、
    カード `w=176 h=166`(既存ゲージと同一幅)
  - 長いモデル名(`qwen3.8_flashnext_exl3_4p05bpw_extra_long_name_for_wrap_test`):
    `vm h=34 w=150 cardH=183` → カード幅内に 2 行で収まる
    ※ 2026-10-12 の変更でこのカードのタイトル行 `vLLM / モデル` と `モデル名` ラベルは削除、
    モデル名は値のみ 1 行省略、`コンテナ状態` は `状態` に改称 (下記 2026-10-12 を参照)
  - モデルタブ: vLLM 行の下に 5 品目(`items=5`)、クリックで `sel=OK selected=qwen38radiance`、
    `go.disabled=false`、ボタン文言「切り替え実行: qwen38radiance」。RAG セクションも同タブ内に表示
  - タブ切り替え自体が起動スクリプトの実行を要するため、モデルタブ表示に成功したことで
    例外無く完了していることも確認

## 現在値の vLLM テキストカード: タイトルとモデル名ラベルを削除・「状態」表記 (2026-10-12)

要件: 現在値(システムタブ)の vLLM/モデル パネルが他のゲージカードより高くなるため、
タイトルラベル `vLLM / モデル` と `モデル名` ラベルを削除する(モデル名**値**は表示する)。
`コンテナ状態` は `状態` に改称する。

※ 2026-10-11 メモの「vLLM / モデル」カード表記(タイトル行・モデル名 2 行 clamp・`cardH=183`)は
本メモで置き換え

- `VINFO_CARD`: `gtitle`(`vLLM / モデル`)と `モデル名` ラベルを削除。先頭行は
  `<span class="v name" id="vi-model">` のだけで、モデル名の値のみを表示する。
  行は `モデル名 / ポート / 状態 / prefill / decode` の 5 行
- CSS: 旧 `.vi-row.name-row` と 2 行 clamp(`line-height:1.3` + `max-height:2.6em`)は削除。
  新 `.vi-row .v.name` は `flex:1 1 auto` + `min-width:0` + `white-space:nowrap` +
  `overflow:hidden` + `text-overflow:ellipsis` の 1 行省略(176px のカード幅に収める)
- JS: `V_MIRROR` の `"v-model": "vi-model"` と、`setV()` の `name` クラス維持 +
  `title` 設定(1 行省略されたモデル名の救済)は残す
- `コンテナ状態` → `状態` は現在値カードのみ(モデルタブの vLLM 行 `#v-status` は従来表記)
- 検証(headless chrome で実描画): 長いモデル名
  (`qwen3.8_flashnext_exl3_4p05bpw_extra_long_name_for_wrap_test`)で
  `vinfoH=135` / 他カード 10 枚も全て 135(同高)、`nameClientW=150 nameScrollW=439`(1 行省略)、
  `title` にフル名、ラベルは `ポート|状態|prefill|decode`


## GB10 サーマルガード: 温度連動の GPU クロック制御 (2026-10-16)

> **2026-10-17 に WhitebearATOM2 (R9700) 版を追加した。** `api/thermal_guard.sh` と
> `api/thermal-guard.service` は両機で同一ファイルを使い、起動時に
> **clock(GB10 = GPU クロック) / power(R9700 = GPU 電力上限)** を自動判定する。
> 本節の clock 経路のロジックは変更していない。R9700 側は次節「atom2 サーマルガード」参照。
> `front/index.html` も両機同一で、**発動中に表示するパネルだけが異なる**
> (atom1 は監視 CPU/GPU 温度・制限 GPU クロック。下記「フロント表示」参照)。

### 背景

GB10 は CPU/GPU 同一ダイ・共有冷却で、`acpitz` が **104℃ に達すると本体が落ちる**
(ログなしの即時電源断)。NVIDIA-Nemotron-3.5-Super-VL-120B-A12B の長大 prefill で再現した。

**発熱源は GPU であって CPU ではない**(交差比較で確定):

| | Qwen 4p05 259K prefill | Nemotron 196K prefill |
|---|---|---|
| CPU %user+%system | **8.34%** (idle 90.68%) | 7.06% (idle 92.90%) |
| load1 ピーク | **3.87** | 2.07 |
| GPU 電力 peak | 71 W | **83 W** |
| acpitz peak | 88℃ | **94℃** |

CPU 負荷は Qwen の方が高いのに acpitz は 6℃ 低い。`acpitz` は SoC 内 7 ポイントの総合で
GPU 熱が直結する。GB10 には CPU 電力センサーが存在しない(`hwmon` に `power*_input` なし、
`/sys/class/powercap` も空)ため、GPU 電力が唯一の可視変数。

**GPU 側の制限可否**: `nvidia-smi -pl`(電力)と `-rac`(メモリクロック)は `not supported`。
`-lgc`(GPU クロック)は一般ユーザーで `does not have permission to change clocks` =
**権限エラーであって非対応ではない**。root で受理される:

```
sudo nvidia-smi -lgc 1500,1500   ->  実クロック 1475 MHz
```

**効果(実測)**:

| | 2398 MHz | 1475 MHz |
|---|---|---|
| GPU 電力 | peak 83 W | peak 26 W (avg 21.4) |
| GPU 温度 | peak 85℃ | peak 54℃ |
| **acpitz peak** | **94℃** (196K) → 259K で本体停止 | **72℃** (254K PASS) |
| prefill 速度 | 2285 T/s | 1110 T/s (49%) |

94℃ 到達時にも GPU の throttle reason は `0x0`(`sw_thermal_slowdown`/`hw_thermal_slowdown`
とも Not Active)。GPU は温度で減速しないので、自動保護は当てにできない。

### 設計: root 特権を `thermal-guard.service` だけに限局

API を root にすると `vllm.py` が使う `systemctl --user` / `journalctl --user`
(Magnitude 情報、271・290 行のコメント参照)が壊れ、かつ CORS `*` + 0.0.0.0 + 認証なしの
API に root を載せることになる。**cliclie への NOPASSWD 付与も望まない。**

そこで **root で動くのは `thermal_guard.sh` 1 本だけ**にした:

```
thermal-guard.service (root)  ── 2 秒ループ ──> nvidia-smi -lgc / -rgc
        └─> /run/thermal/state (0644)  ──> API /api/thermal (cliclie) が読むだけ
```

- `dgx-spark-api.service` は `User=cliclie` のまま**一切変えない**
- root unit が実行するコマンドは読み取り(sysfs / `nvidia-smi --query` / `docker ps`)と
  `-lgc` / `-rgc` のみ
- 制御ループが API と独立なので、**フロント更新で API を再起動しても制御が途切れない**
  (`RestartSec=5` の穴が開かない)
- `switch_models.sh` 側の GPU 制御は削除した(二重制御回避)。cpufreq 側は残す

### 3 段ヒステリシス

制御変数は `max(acpitz 最高値, GPU温度)`。

| 条件 | 動作 |
|---|---|
| `>= CRIT` (98℃) | `-lgc 1000,1000` — 104℃ 停止への最後の弁 |
| `>= HIGH` (90℃) | `-lgc 1665,1665` — `Environment=THERMAL_LOCK_HIGH_MHZ=1665`。`-lgc` はサポート刻みに切り下げるので実効値は **1651 MHz** |
| `<= LOW` (85℃) が 10 秒継続 | `-rgc` で解除 — `Environment=THERMAL_LOW_C=85` |

- **下げるのは即時、上げるのは 10 秒継続**。GB10 の熱時定数が約 100 秒あるので対称条件は発振する
- 熱応答実測: 70→80℃ を 28 秒、80→90℃ を 24 秒(=0.4℃/秒)。**2 秒制御で余裕がある**
- 1475 MHz では 68〜72℃ で**飽和**するので 90℃ に届かない(制御の安定性はこれに支えられている)
- **対象コンテナが稼働中のときだけ作動**（既定は `vllm-nemotron120b` と `tabbyapi-ornith122b`）。2026-10-16 は ExLlama 系が 88℃/71W で HIGH(90) に届かず圏外だったが、**2026-10-28 に Ornith 122B の 259K prefill で監視温度 97℃ を実測して対象に追加した**（下記「2026-10-28: 対象をコンテナ名リスト化」節）
- **終了時は必ず `-rgc`**(`trap ... EXIT TERM INT`)。低クロック固定で性能を失い続ける方が害が大きい
- 全しきい値は環境変数で上書き可(`THERMAL_HIGH_C` / `THERMAL_CRIT_C` / `THERMAL_LOW_C` /
  `THERMAL_LOW_SECS` / `THERMAL_LOCK_HIGH_MHZ` / `THERMAL_LOCK_CRIT_MHZ` /
  `THERMAL_INTERVAL_S` / `THERMAL_CONTAINERS`（対象リスト。既定 `vllm-nemotron120b tabbyapi-ornith122b`）/
  `THERMAL_CONTAINER`（単一指定の後方互換。指定時はリストを置き換える）)

### 追加ファイル

- `api/thermal_guard.sh` — 制御ループ(上記)
- `api/thermal-guard.service` — `RuntimeDirectory=thermal` で `/run/thermal` を自動作成・自動削除
- `api/config.py` — `THERMAL_STATE_FILE`(`DGXUTIL_THERMAL_STATE` で上書き可)
- `api/main.py` — `GET /api/thermal`(状態ファイルを読むだけ。unit 停止時は `{"enabled": false}`)
- `api/install_service.sh` — thermal-guard unit をレンダリングして `enable --now`

### 検証

**机上(しきい値を下げた擬似発熱)**: CRIT→1000MHz / HIGH→1500MHz / 通常→解除 を確認。
nemotron 非稼働(コンテナ名不一致)では 90℃ 相当でも `locked_mhz=0` を維持。
`GET /api/thermal` が全フィールドを JSON で返し、既存 `GET /api/metrics` も 200。
`systemd-analyze verify` 通過。

**実戦(2026-10-16 11:08、220K 目標を 2398MHz 起点で投入)**:

```
11:10:38  thermal-guard: 90C -> 1500MHz (acpitz=90C gpu=77C running=1)
11:10:48  thermal-guard: 84C -> 0MHz    (acpitz=84C gpu=66C running=1)
run1 req=220000 -> prompt=223739 comp=80 elapsed=147.4s prefill=1517.9 tok/s
```

- **発動は実測で機能した**。90℃ 検知で 1500MHz に下がり、acpitz は **90℃ で頭打ち**(94℃ に到達せず)
- **10 秒周期で `-lgc` を叩いても vLLM は壊れなかった**(Xid 再発 0 件、コンテナ生存、prefill PASS)。
  クロック変更が CUDA コンテキストを乱す懸念は実測で否定された
- **prefill 1517.9 T/s** は 1475MHz 固定の 1110 T/s より **37% 速い**。90℃ までフルクロックで
  走り、超えた分だけ落とす挙動は、固定ロックより速度面で有利

**実戦で発見した 2 つの不具合(修正済み)**:

1. **起動時に解除されない** — `current=0`(解除済み)で開始するが、実 GPU は手で `-lgc` した
   1475MHz にロックされている。`want=0 == current=0` で `apply` が走らず、`low_count=67`
   (134 秒)でも永久に解除されなかった。→ 起動時に 1 回 `-rgc` して既知の状態から始めるよう修正
2. **80〜90℃ のヒステリシス帯域が欠落** — `target_mhz()` が `HIGH(90)` 未満で 0 を返すだけで
   `LOW(80)` を見ていなかった。90℃ で下げる → 84℃ で解除 → 即 90℃ → 発動の **10 秒発振**に
   なった(上記ログがそれ)。→ `LOW < t <= HIGH` では `current` を維持する分岐を追加

修正後は 90℃→1500MHz、80℃ 以下で 10 秒→解除の周期運動になる(推定 90 秒周期)。
`-lgc` 連打は無害と確認済みなので、速度を優先するなら `THERMAL_LOW_C=85` のように
`LOW` を上げて解除を速める選択肢がある。

### フロント表示 (2026-10-17)

`front/index.html` は両機で同一ファイル。2 秒ポーリングで `/api/thermal` を引き、発動中に
着色するパネルは `thermalState()` が返す `watchId`(監視対象)と `limitId`(制限対象)が決め、
**atom1 と atom2 で対象が違う**(要求仕様):

| モード | 監視対象 `watchId` | 制限対象 `limitId` |
|---|---|---|
| atom1 = clock (GB10) | `acpitz_c >= gpu_c` → **CPU 温度**、それ以外 → **GPU 温度** | **GPU クロック** |
| atom2 = power (R9700) | **GPU 温度** | **GPU 電力** |

- **ヘッダ (P-State の右) のインジケータ**: `クロック制限 1500MHz (CPU 90℃)` /
  `クロック制限 1000MHz (GPU 99℃)`。温度は**犯人側だけ**を表示し `CPU`/`GPU` を明記する
- 段は**制限値そのもの**で決める(clock 側はクロック値そのもの)。
  `locked_mhz <= lock_crit_mhz`(1000MHz)→ 暗め赤 `--thr-red: #9e3b3b`、
  それ以外の発動 → 暗め黄 `--thr-yellow: #9a7b1f`
- **復帰**(`locked_mhz = 0` = `-rgc` 解除)と unit 停止時(`{"enabled": false}`)はクラスが
  外れて灰色(`--border: #30363d`)に戻る。`/api/thermal` 取得失敗時も同じく無表示
- 枠線の**太さは変えない**(他カードと高さを揃えたまま色だけで判別させる)

監視対象が 2 枚ある理由: GB10 は CPU/GPU 同一ダイ共有冷却で、`thermal_guard.sh` の clock
ループが見るのは `max(acpitz, GPU温度)`。状態 JSON の `temp_c` はその max なので、
`acpitz_c` と `gpu_c` を比較して**制限値に引っかかった側**を特定できる(同値は共有ダイ側の
acpitz = CPU 温度パネル)。atom1 の「GPU 電力」パネルは監視・制限対象ではないので着色しない。

**実機で発動 JSON を採取して確認(2026-10-17)** — Nemotron 196K prefill 中の実データ
`{"acpitz_c":75,"gpu_c":66,"locked_mhz":1665,"clock_mhz":1657,"lock_crit_mhz":1000,...}` を
`thermalState()` に通すと `watchId=g-cpu-temp` / `limitId=g-gpu-clock` / `crit=false`
(**暗め黄**)、ヘッダは `クロック制限 1665MHz (CPU 75℃)`。解除(`locked_mhz=0`)は全カード灰色。
実測で `acpitz_c > gpu_c` が 253 / 254 サンプルなので、**発動時に染まるのはほぼ常に CPU 温度
パネル**であり、atom2 版の「GPU 温度パネル固定」なら逆のパネルが染まることになる。

### インストール

```bash
sudo bash /home/cliclie/DGXSparkUtil/api/install_service.sh
systemctl status thermal-guard.service   # 起動確認
curl -s localhost:8080/api/thermal       # 状態 JSON
journalctl -u thermal-guard.service -f   # 発動ログ
```

**`install_service.sh` の再実行で unit 変更は自動反映されない**(2026-10-16 に実測):
`systemctl enable --now` は**稼働中 unit を再起動しない**。`Environment=` のしきい値変更は
`systemctl restart thermal-guard` をしないと旧環境変数のまま動き続ける。実際に
`THERMAL_LOW_C=85` を unit に入れた後も `/api/thermal` は `"low_c":80` を返し、稼働
プロセスの起動時刻は変更前のままだった。→ `install_service.sh` に `restart` を追加した。
反映の確認は `/api/thermal` の `low_c` / `lock_high_mhz` を**数値で見る**こと。

### クロック段と採用値(2026-10-16 実測)

`-lgc` の指定値は下位の段に丸められる。`--query-supported-clocks=graphics` は `[N/A]`
で段テーブルを公開しないため、実測に頼る:

| 指定 | 実クロック | 丸め幅 |
|---|---|---|
| `1500,1500` | **1475 MHz** | -25 |
| `1665,1665` | **1644 MHz** | -21 |
| `2400,2400` | 2398 MHz | -2 |

公称 base 1665 MHz / boost 2418 MHz に対して、実測は prefill 中 **2457/2463/2496/2515 MHz**
まで出る(**2418 MHz 説は実測と合わない**)。`clocks.max.gr` の報告は 3003 MHz。

**`1500` 指定は base 1665 MHz の「以下」**で、prefill を 2285→1110 T/s(49%)まで落として
いた。速度を 49% 殺す理由がなかった。→ `THERMAL_LOCK_HIGH_MHZ=1665`(実 1644 MHz)を採用。

**実測した(2026-10-16 11:39、220K、`-lgc` なし、thermal-guard のみ)**:

| 発動時ロック | prefill | acpitz peak | 発動/解除 |
|---|---|---|---|
| 1475 MHz(`-lgc 1500`) | 1517.9 T/s | 90℃ | 4 回 |
| **1651 MHz(`-lgc 1665`)** | **1493.5 T/s** | 90℃ | 3 回 |

**atom1 実機で再確認した(2026-10-17 08:50、196K 目標 = 実 199,210 tok、`prefill_load.py`)**:

```
08:51:33  thermal-guard: 90C -> 1665MHz (acpitz=90C gpu=78C running=1)
08:51:43  thermal-guard: 74C -> 0MHz    (acpitz=74C gpu=66C running=1)
08:51:52  thermal-guard: 90C -> 1665MHz (acpitz=90C gpu=81C running=1)
08:52:02  thermal-guard: 74C -> 0MHz    (acpitz=74C gpu=67C running=1)
08:52:15  thermal-guard: 90C -> 1665MHz (acpitz=90C gpu=83C running=1)
08:52:25  thermal-guard: 69C -> 0MHz    (acpitz=69C gpu=61C running=1)
run1 req=196000 -> prompt=199210 comp=80 elapsed=117.5s prefill=1695.8 tok/s
```

- 発動/解除は 3 回ずつ。**acpitz は 90℃ で頭打ち**(94℃ 未到達 = 104℃ 停止を回避)、
  GPU peak 83℃。発動中の実クロックは 1651〜1657 MHz
- **発動状態の占比は 2 秒 x 254 サンプル中 16 = 6%**。1651 MHz の冷却力が強く、
  発動から 10 秒で 74℃ まで下がって解除される(発振周期は短い)
- prefill **1695.8 T/s** は 1651 MHz 段の 2026-10-16 実測(1493.5 T/s)より **13% 速い**
- **`acpitz_c > gpu_c` が 253 / 254 サンプル** — Nemotron prefill は CPU 側が 9〜12℃ 高い。
  発動時に枠線が染まるのはほぼ常に「CPU 温度」パネルで、GPU 温度パネルは灰色のまま
  (フロントの atom1 向け分岐が効く実データ)

**速度の優位性はなかった(1.6% 遅い)。** 1651 MHz は 1475 より 11.5% 速いが発熱も速く、
90℃ 到達が早まって発動状態の占比が増えるため相殺される。**90℃ 到達までの時間が支配的**
なので、ロック値を上げる戦略は速度に効かない。

発動時の実測: クロック 1651 MHz、**消費電力 77W→31W**、acpitz 86℃ まで低下。
1651 MHz には acpitz を **64℃ まで下げる**冷却力があり `LOW=85` を下回って解除される
→ 発振は残るが周期は延びた(4 回→3 回)。

**速度を上げるには 90℃ 到達を遅らせる(=平均電力を下げる)しかない**が、2398 MHz 固定は
259K で 94℃ 到達により **48 秒で本体を停止させた**実績がある。`HIGH=90` はこの事故の
実測値から決めた値であり、上げる余地は乏しい。**現行設定(HIGH=90 / LOCK=1665 / LOW=85)
で概ね最適**。

### 未解決(別件)

1475 MHz 維持下で 1 回 `Xid 31`(MMU Fault / `FAULT_PDE ACCESS_TYPE_VIRT_READ`)を観測したが、
同条件の再投では再現せず 254K が通った。KV 容量は棄却済み(`GPU KV cache size: 380,487 tokens`)。
残る疑いは **`--max-num-batched-tokens 2048` < attention block size `4544`**
(Mamba2 混成で attention/mamba ページサイズを揃えた結果)。詳細は
`/home/cliclie/llm/README.md` の Nemotron 節。

**2026-10-17 に 196K 投で再発(既知扱い・現状手当不要)**: 1665 MHz 段を 3 回発動させた直後
(最後の解除 08:52:25 の 5 秒後)に `NVRM: Xid 31 … FAULT_PDE ACCESS_TYPE_VIRT_READ` →
`cudaErrorIllegalAddress` で EngineCore が shutdown し、コンテナが終了した。1 本目の prefill
199,210 tok は完了しているが decode が 0.7 T/s に損傷し、2 本目は HTTP 500。**熱とは無関係**
(acpitz は 90℃ で抑え込まれており 94℃ に届いていない)ので、**サーマルガード側の対応対象では
ない**。発動中の `-lgc` 連打は 6 遷移のみで、2026-10-16 の無害検証(10 秒周期で壊れない)と整合。

---

## GB10 サーマルガード: 対象をコンテナ名リスト化し Ornith 122B を追加（2026-10-28）

運用者が Ornith 1.5 122B（`tabbyapi-ornith122b`・ポート 8018）の needle テスト中に
フロントの監視温度が **97℃** に達したのを確認した。2026-10-16 の「ExLlama 系は圏外」判定が
覆ったため、**ガード対象を単一コンテナ名からリスト化**して追加した。

### 実測（2026-10-28・Ornith 122B / EXL3 4.049bpw / 259,040 tok prefill）

| 項目 | 値 |
|---|---|
| フロントの監視温度 `max(acpitz, GPU温度)` | **97℃**（運用者の目視。**CRIT 98℃ の 1℃ 下**） |
| `nvidia-smi temperature.gpu`（当方の 30 秒ポーリング） | **84〜85℃** |
| 差の意味 | **GPU 温度ではなく acpitz が 97℃ に達していた**。制御変数は `max(acpitz, GPU温度)` なので、GPU 温度だけを見ると圏外に見える（2026-10-16 の誤判定の原因） |
| prefill | 259,040 tok を 500.4 s = **518 T/s**（ネイティブ 262,144 の 98.8%） |
| アイドル復帰 | 約 3 分で acpitz 41℃ / GPU 39℃（全 acpitz ゾーン 39〜41℃） |

`acpitz` は SoC 内 7 ポイントの総合で、アイドル時は GPU 温度（39℃）と揃う。
**高負荷長文では acpitz が GPU 温度を 10℃ 以上上回る**。

### 変更内容（`api/thermal_guard.sh`）

| 箇所 | 変更前 | 変更後 |
|---|---|---|
| 対象指定 | `CONTAINER="${THERMAL_CONTAINER:-vllm-nemotron120b}"`（単一） | `CONTAINERS="${THERMAL_CONTAINERS:-vllm-nemotron120b tabbyapi-ornith122b}"`（空白区切りリスト） |
| 後方互換 | — | `THERMAL_CONTAINER` 指定時はリストを置き換える（unit 側は未設定＝新既定が効く） |
| 稼働判定 | `docker ps \| grep -qx "$CONTAINER"` | `docker ps` の名前を 1 回取得し、`CONTAINERS` のいずれかと `grep -qx` で完全一致（`break`） |
| 起動ログ | `container=<name>` | `containers=<list>` |

- 判定は**完全一致**なので部分一致で他モデルを巻き込まない（`tabbyapi-flashnext-4p05` は対象外のまま）
- **しきい値・段・ヒステリシスは一切変えていない**（HIGH 90 / CRIT 98 / LOW 85×10s / 1665・1000 MHz）
- `power` モード（atom2）は常時監視で対象リストを使わないため**無影響**

### 検証

`sh -n` / `dash -n` 通過。稼働判定は `docker` を差し替えて 5 ケース確認した:

| ケース | 対象指定 | 稼働コンテナ | `running` |
|---|---|---|---|
| A | 既定 | `tabbyapi-ornith122b` + 他 | **1**（追加が効いた） |
| B | 既定 | `tabbyapi-flashnext-4p05` + 他 | 0（対象外を維持） |
| C | 既定 | `vllm-nemotron120b` | 1（既存動作不変） |
| D | `THERMAL_CONTAINER=vllm-nemotron120b` | `tabbyapi-ornith122b` | 0（単一指定がリストを置き換える） |
| E | 既定（**実 docker**） | `tabbyapi-ornith122b` のみ | **1** |

反映は **2026-10-28 11:18:22 に `sudo systemctl restart thermal-guard` で実施済み**。起動ログは
`containers=vllm-nemotron120b tabbyapi-ornith122b`、`/run/thermal/state` の `model_running` は
ornith 稼働時に **1**（アイドル時は 41℃ / 2398 MHz / `locked_mhz=0`）。

### 実戦検証（2026-10-28 11:21・反映後に同一負荷を再投）

97℃ を出したのと同じ needle を再投した。ただし **filler が同一のプロンプトは ExLlamaV3 の
KV キャッシュに全ヒットして prefill が走らない**（259,040 tok が **5.1 s** で PASS）ため、
`needle_test_2p05.py` に `--seed`（200 文ごとに salt を混ぜる）を追加してキャッシュを外し、
260,442 tok で再投した。

| 項目 | 反映前（10:56・対象外） | 反映後（11:21・対象化） |
|---|---|---|
| prompt tokens | 259,040 | 260,442（salt 分 +1,402） |
| フロント監視温度のピーク | **97℃**（CRIT 98 の 1℃下） | **91℃** |
| 発動 | **なし**（`model_running=0` → `want=0`） | **18 回**（`90C -> 1665MHz`） |
| ロック時間 | 0 s | **216 s / 560 s = 39%**（実クロック **1651 MHz**） |
| 90℃ 以上に居た時間 | 97℃ まで上昇し続ける | **42 s**（21 サンプル / 2 秒解像度） |
| prefill 経過 | 503.6 s = 518 T/s | 558.8 s = **466 T/s（−10%）** |
| needle 結果 | PASS（739241） | **PASS（739248）** |
| 完了後 | — | `11:30:53 76C -> 0MHz` で解除（低クロック固定は残らない） |

- **発動は 90℃ 交差から 2 秒以内**。発振は残るが周期は約 30 秒（90℃ → ロック → 73〜81℃ → 解除 → 上昇）で、前節の実測傾向どおり
- **代价は prefill −10%**。decode は 90℃ に届かないので影響しない
- 教訓: **同一プロンプトの再投は prefill を通らない**。発熱・長文速度の実測は `--seed` でキャッシュを外して行う

### 教訓

1. **「ExLlama 系は圏外」は GPU 温度だけを見た判定だった**。ガードは `max(acpitz, GPU温度)` を
   見るので、**対象の合否は acpitz 側の実測で判断する**
2. **対象は既定値のリストなので unit の `Environment=` を足さなくても効く**。既存機（atom2 は
   `power` モードで常時監視）に影響なし

## atom2 サーマルガード: 温度連動の GPU 電力上限制御 (2026-10-17)

GB10 サーマルガード(前節)を WhitebearATOM2 (R9700) へ移植したもの。**同一の
`api/thermal_guard.sh` / `api/thermal-guard.service` を両機で共有**し、GPU の種類で
制御手段だけを切り替える。

| | atom1 (GB10) | atom2 (R9700) |
|---|---|---|
| モード | `clock` | `power` |
| 書き込み先 | `nvidia-smi -lgc / -rgc` (GPU クロック) | sysfs `power1_cap` (GPU 電力上限) |
| 監視温度 | `max(acpitz, GPU温度)` | **GPU 温度 (junction) のみ** |
| 対象 LLM | `THERMAL_CONTAINERS`（既定 `vllm-nemotron120b` + `tabbyapi-ornith122b`）の稼働中だけ | **常時** (ロード中のモデルを問わない) |
| 段 | 1000 / 1665 MHz / 解除 | **210 / 250 / 300 W** |

### 自動判定

`detect_mode()` が `nvidia-smi` が PATH にあれば `clock`、無ければ amdgpu の
`power1_cap` sysfs を探して `power`。`THERMAL_MODE=clock|power` で明示上書きできる
(`api/config.py` のプラットフォーム検出と同じ思想)。

### 3 段の電力上限 (要求仕様)

| GPU 温度 | 電力上限 | 環境変数 |
|---|---|---|
| 80℃ を超えたら | **250 W** | `THERMAL_POWER_HIGH_C=80` / `THERMAL_CAP_HIGH_W=250` |
| 90℃ を超えたら | **210 W** | `THERMAL_POWER_CRIT_C=90` / `THERMAL_CAP_CRIT_W=210` |
| それ以下 | **300 W (最大)** | `THERMAL_CAP_MAX_W=300` |

- 境界はミリ℃で比較するので「80℃ を超えたら」は 80.001℃ から発動し、80.000℃ は 300W
- `power1_cap_min` = 210W / `power1_cap_max` = 300W が R9700 の書ける範囲。要求された
  300/250/210W はちょうどこの範囲に収まる (下限を下回る指定は起動時にクランプ)
- 温度は `label=junction` を優先 (`metrics.py:_amdgpu_temp_c()` と同じ定義)。ダッシュボードと
  ガードが見る温度をズレさせないため

### 解放は「2 秒以上下がった」時だけ / 制限強化は即時

電力を**下げる**方向は即時。電力を**上げる**方向 (解放) は、その狙い値が
`THERMAL_POWER_RELEASE_SECS=2` **秒以上継続した時だけ**適用する。

- 判定はサンプル数ではなく**実時刻** (`pending_since` と `date +%s` の差分)。
  サンプル数でやると監視間隔の実測ぶんに引きずられる。実測で 1 秒ループが
  sysfs 読み込み込みで **約 1.4 秒/回** になり、2 サンプル待ちでは早すぎ (実効 1.2 秒)、
  3 サンプル待ちでは遅すぎ (実効 4.2 秒) た。時刻判定なら両方の問題が消える
- 段階を跨ぐ解放も「2 秒続いた狙い値」にしか行かないので、89℃⇄91℃ の発振で
  210↔250W を連打することはない
- 起動時に必ず `power1_cap_max` へ戻して既知の状態から始める。終了時 (`trap`) も
  必ず最大へ復帰 (前節の `-rgc` と同じ理由)

### 常時監視 (GB10 版との最大の違い)

`loop_power()` は `docker ps` による LLM 稼働判定を**行わない**。atom2 は VRAM 32GB で
モデルロード直後から電力が張り付くため、どのモデルが載っていても温度だけを見る。

### フロント表示 (2026-10-17)

`front/index.html` が 2 秒ポーリングで `/api/thermal` を引き、発動中なら

- **ヘッダ (P-State の右) にインジケータ**: `電力制限 250W (GPU 32℃)`
- **監視対象の「GPU 温度」パネルと制限対象の「GPU 電力」パネルの枠線**を着色
  - 250W 段 → 暗め黄 `--thr-yellow: #9a7b1f`
  - 210W 段 → 暗め赤 `--thr-red: #9e3b3b`

復帰 (上限 = 最大 300W) と unit 停止時 (`{"enabled": false}`) はクラスが外れて灰色
(`--border: #30363d`) に戻る。判定は `thermalState()` が担い、段は**制限値そのもの**
(`power_cap_w <= cap_crit_w`) で決める。GB10 (clock モード) は**監視対象が CPU 温度 / GPU 温度の
2 枚**で**制限対象は GPU クロック**と対象パネルが異なる (色段は `locked_mhz <= lock_crit_mhz`
が赤、それ以外の発動が黄、`locked_mhz = 0` = 解除は灰色)。詳細は前節
「GB10 サーマルガード → フロント表示」の表参照。

### 旧 `llm-gpu-powercap.service` を無効化

従来は oneshot の `llm-gpu-powercap.service` が `amd-smi set -o ppt0 210` で **210W に
常時固定**していた (実装メモ 2026-10-03)。残すと再起動時に 210W へ戻され、80℃ 以下で
300W にならない。`install_service.sh` が `power1_cap` の唯一の書き手を thermal-guard に
集約するため、AMD 機ではこの unit を `disable --now` する (unit ファイル自体は削除しない)。

### はまった点 (両機共通の unit を触る時の注意)

- **`/bin/sh` は dash**。`$((10#$1 * 1000000))` の進数プレフィックスは非対応で
  `arithmetic expression: expecting EOF`。W→µW の変換は素の算術展開にした
- **`set -euo pipefail` 下では `grep -q` が早期終了して SIGPIPE を返す**。
  `systemctl list-unit-files | grep -q '^llm-gpu-powercap\.service'` がパイプライン全体で
  非0 になり、無効化の分岐が静かに素通りした (一般シェルではマッチするのに)。
  判定は unit ファイルの存在 (`[ -e /etc/systemd/system/... ]`) に変えた
- 前節と同じく `systemctl enable --now` は稼働中 unit を再起動しないので、
  `install_service.sh` は thermal-guard を毎回 `restart` している

### 検証 (2026-10-17)

**疑似 hwmon でのループ検証** — `/tmp` にフェイク hwmon を作り、実ファイルからループ
末尾の `case` を除いた `loop_power()` をそのまま source して `cap_file()` だけ差し替えた。
実機の `power1_cap` には触っていない:

| 操作 | 期待 | 実測 |
|---|---|---|
| 旧 210W 固定 + 30℃ で起動 | 300W へ初期化 | **300W** |
| 30℃ → 85℃ | 250W 即時 | `85C -> 250W (was 300W)` |
| 85℃ → 95℃ | 210W 即時 | `95C -> 210W (was 250W)` |
| 95℃ → 30℃ を 1.2 秒 | 解放しない | 210W 維持 |
| 30℃ (1.2s) → 85℃ へ逆戻り | 発振しない | 210W 維持 |
| 30℃ を 3.2 秒 | 300W へ解放 | `30C -> 300W (was 210W, released after 2s)` |
| 300W → 95℃ | 210W 即時 | `95C -> 210W (was 300W)` |
| 95℃ → 82℃ を 3.2 秒 | 250W へ解放 | `82C -> 250W (was 210W, released after 2s)` |
| `kill -TERM` | 300W へ復帰 | **300W** |

**実機 (systemd)**:

```
thermal-guard start: mode=power high=80 crit=90 caps=300/250/210W release=2s interval=1s \
  cap=/sys/class/drm/card1/device/hwmon/hwmon5/power1_cap
```

- `llm-gpu-powercap.service` は `disabled` / `inactive`、`power1_cap` = **300000000**
- drop-in で `HIGH_C=25 / CRIT_C=30` に一時変更し、**全 LLM コンテナが `running: false`
  の状態**で `thermal-guard: 31C -> 210W (was 300W)` を確認 = 常時監視が効いている
- drop-in 削除 + restart で 300W に復帰
- `curl -s localhost:8080/api/thermal` (cliclie 権限) が
  `{"mode":"power","temp_c":32.0,"power_cap_w":300,"power_max_w":300,"power_min_w":210,
  "always_monitor":true,"high_c":80,"crit_c":90,"cap_high_w":250,"cap_crit_w":210,
  "cap_max_w":300,"release_secs":2,...}` を返す
- `sh -n` / `bash -n` / `python -m py_compile` 通過。clock モード (`loop_clock`) は
  前節のロジックそのまま (インデント変更のみ) で、atom1 側の挙動は不変


## atom1 フロントのサーマルガード表示: 監視対象を CPU/GPU 温度の 2 枚に (2026-10-17)

atom2 側で実装されたフロントのサーマルガード表示(`c9125f1`)を atom1(WhitebearATOM1 /
GB10)へ pull して反映した。ただし**監視対象とガード制限対象が両機で違う**ため、
着色・インジケータ対象のパネルを atom1 向けに変えた。

| | 監視対象(表示するパネル) | 制限対象 |
|---|---|---|
| atom2 (R9700 / power) | GPU 温度 | GPU 電力 |
| atom1 (GB10 / clock) | **CPU 温度 と GPU 温度**(制限値に引っかかった方だけ) | **GPU クロック** |

### 実装

`thermalState()` が監視対象 ID を `watchId` として返し(`limitId` は制限対象)、
`renderThermal()` は `g-gpu-temp` 固定ではなく **`st.watchId` と `st.limitId` の 2 枚だけ**
着色する。

```js
const a = Math.round(t.acpitz_c ?? 0);
const g = Math.round(t.gpu_c ?? 0);
const cpuSide = a >= g;
// watchId: cpuSide ? "g-cpu-temp" : "g-gpu-temp" / limitId: "g-gpu-clock"
```

- 犯人の決め手: `loop_clock()` は `t = max(acpitz, GPU温度)` を見るので、状態 JSON の
  `temp_c` はその max。`acpitz_c` と `gpu_c` を比較して max を生んだ側が制限値に
  引っかかったセンサー(同値は共有ダイ側の acpitz = CPU 温度パネル)
- ヘッダのインジケータも犯人側を明記: `クロック制限 1665MHz (CPU 92℃)` /
  `クロック制限 1000MHz (GPU 99℃)`。色段は従来と同一で `locked_mhz <= lock_crit_mhz`
  (1000MHz)= 暗め赤、それ以外の発動 = 暗め黄、`locked_mhz = 0`(解除)= 灰色
- atom1 の「GPU 電力」パネルは監視・制限対象ではないので着色しない(atom2 では制限対象)
- power 分岐にも `watchId: "g-gpu-temp"` を入れて**atom2 の表示は変更なし**

### 検証

- **inline script 7 ブロックを esprima(4.0.1)でパース** — 全ブロック OK(`??` は
  esprima 4.0.1 が ES2020 非対応なので `||` へ置換して parse。null 系で意味は同じ)
- **`thermalState()` / `renderThermal()` を dukpy(Duktape)で実行**し、`thermal_guard.sh` の
  実 JSON 形状で判定表を確認:

| 入力 | ヘッダ | 着色されるパネル |
|---|---|---|
| `locked_mhz=1665, acpitz=92, gpu=77` | `クロック制限 1665MHz (CPU 92℃)` 黄 | CPU 温度 + GPU クロック |
| `locked_mhz=1665, acpitz=88, gpu=91` | `… (GPU 91℃)` 黄 | GPU 温度 + GPU クロック |
| `locked_mhz=1000, acpitz=95, gpu=99` | `… (GPU 99℃)` 赤 | GPU 温度 + GPU クロック(crit) |
| `locked_mhz=1665, acpitz=gpu=90` | `… (CPU 90℃)` 黄 | CPU 温度 + GPU クロック |
| `locked_mhz=0`(解除)/ `{enabled:false}` | 空 | なし(全カード灰色) |
| `mode=power, 250W` | `電力制限 250W (GPU 85℃)` 黄 | GPU 温度 + GPU 電力(atom2 のまま) |

  どのケースも**着色は監視対象 1 枚 + 制限対象 1 枚の計 2 枚**で、他のカード
  (CPU 負荷 / GPU 電力 など)は灰色のまま
- **atom1 実機**: `thermal-guard` と `dgx-spark-api` は `active`。
  `curl -s localhost:8080/api/thermal` は
  `{"enabled":true,"temp_c":42,"acpitz_c":42,"gpu_c":41,"locked_mhz":0,"low_c":85,
  "lock_high_mhz":1665,"lock_crit_mhz":1000,...}`(`mode` キー無し = clock 経路)を返し、
  `curl -s localhost:8080/` の配信 HTML が作業ツリーと**完全一致**(front は静的配信なので
  `dgx-spark-api` の再起動は不要)。発動中は `acpitz_c` が `gpu_c` を上回ることが多く
  (Nemotron prefill は CPU 側が先に 90℃ に達する)CPU 温度パネルが着色される
- **atom1 実機で負荷を掛けて発動表示を確認(2026-10-17 08:50)** — `vllm-nemotron120b` に
  `prefill_load.py 196000 80 300 2` を流し、2 秒間隔で `GET /api/thermal` を 254 回採取した。
  発動 3 回 / 解除 3 回で、発動中の実 JSON
  `{"acpitz_c":75,"gpu_c":66,"locked_mhz":1665,"clock_mhz":1657,"lock_crit_mhz":1000,...}` を
  実 `thermalState()` に通した結果は `watchId=g-cpu-temp` + `limitId=g-gpu-clock` の
  `thr-high`(暗め黄)でヘッダは `クロック制限 1665MHz (CPU 75℃)`、解除 JSON は全カード灰色。
  **`acpitz_c > gpu_c` が 253 / 254** なので実運用の発動で染まるのはほぼ常に CPU 温度パネル。
  acpitz は 90℃ で頭打ち(= 104℃ 停止を回避)、prefill は 1695.8 T/s。詳細は
  「GB10 サーマルガード」節の実測と「フロント表示」参照
- 観測ログ: `/home/cliclie/llm/logs/thermal_watch_20261017.log`(2 秒間隔の
  `ts acpitz gpu locked clock_mhz model_running`、269 行)

### 反映手順(atom1)

```bash
cd ~/DGXSparkUtil && git pull --ff-only          # front は静的配信なので即反映
sudo bash api/install_service.sh                 # unit 変更 (power 用 Environment と
                                                 #  amdgpu 待ち ExecStartPre) を反映
systemctl restart thermal-guard                  # enable --now は稼働中 unit を再起動しない
curl -s localhost:8080/api/thermal | python3 -m json.tool
```

GB10 では `thermal_guard.sh` の clock 経路(`loop_clock`)は**ロジック変更なし**。unit の
追記分は power 用の環境変数と `ExecStartPre`(GB10 は `nvidia-smi` を見つけて即通過)だけで、
既存の `THERMAL_LOW_C=85` / `THERMAL_LOCK_HIGH_MHZ=1665` はそのまま有効。

## 一段目メーターの Chart.js 移植（2026-10-20・canvas 直描画の撤去）

### 経緯

- 一段目の半円ゲージは `Gauge.render()` が canvas 2D へ直接描画していた（`ctx.arc` で
  背景アーチ／閾値ゾーン帯／値アーチ）。二段目の時系列グラフは Chart.js のため、
  **一段目も描画ライブラリを Chart.js に統一**したいとの依頼
- 参考元: SociaX-Standalone `operations-ui`（`static/app.js` の `renderCpuGauge()` /
  `cpuGaugeDatasets()` / `cpuGaugeCutout()`）。同リポジトリ `SettingLogs.md` の
  「CPU負荷メーターの Chart.js 移植（2026-10-01・正本化）」と追随修正 1〜3 が確定仕様
- 対象は**全 10 枚**（単一 8 + dual 2）。`Gauge` クラスを丸ごと置換し canvas 直描画は完全撤去

### 構成

- `front/index.html` のみ変更（API 変更なし → `systemctl restart dgx-spark-api` 不要、
  ブラウザリロードのみで反映）
- `new Chart(canvas, {type:"doughnut", options:{responsive:false, circumference:180,
  rotation:-90, cutout, layout:{padding:{top:4, bottom:2}},
  plugins:{legend:{display:false}, tooltip:{enabled:false}},
  animation:{duration:1000, easing:"easeInOutQuad"}}})`
- `gaugeDatasets(def, v)` が dataset を生成する（`index 0` = 最外周）:
  `ds0` 閾値ゾーン帯 `data:[s0*100, (s1-s0)*100, (1-s1)*100]`・3 色ベタ・`borderWidth:0`・`weight:3`
  / `ds1` 単一=値アーチ（ゾーン色・`weight:11`）または dual=外側 A（`colorA` 固定・`weight:8`）
  / `ds2` dual のみ 内側 B（`colorB` 固定・`weight:6`）
- **DGXSparkUtil 固有の要点**: 値リング `data=[値%, 100-値%]` の**残り分を `"transparent"` ではなく
  `ARC_BG`** にした（operations-ui 準拠で transparent にすると canvas 版の背景アーチが消える）
- **余白制御**: 値リングの `borderWidth:1` + `borderColor:CARD_BG(#161b22)` がリング間 1px の
  擬似余白。`spacing` は端が引っ込むため不使用（operations-ui 追随修正 2 準拠）
- **文字は HTML オーバーレイ維持**: `.gval`（中央値 16px + 単位 + `subVal` 2 行目）/
  `.gval-dual-out`（dual の 2 行値をゲージ下）/ `.gsub` をそのまま流用。`set()` の HTML 生成は無変更
- **差分アニメ**: dataset オブジェクトは保持し `data`/`backgroundColor` だけ書き換えて `update()`
  （operations-ui の教訓: datasets を丸ごと差し替えると Chart.js が新規データとみなし毎回 0 から回る）
- `gaugeCutout(dual)` は「1 weight ≈ 1px」になる穴比率（単一 80% / dual 66%）

### 寸法対応（canvas 版 → Chart.js 実測）

`responsive:false` では canvas 属性サイズが論理サイズ（dpr は Chart.js の retinaScale が自動対応）。
`.gcanvas` の実幅は 176px − padding 12×2 − border 1×2 = **150px** で、canvas 属性も 150 にした
（152 にすると右に 2px はみ出す）。半円 doughnut は `getRatioAndOffset()` が `ratioY:0.5` /
`offsetY:0.5` を返すため**中心 y = canvas 高さ − padding.bottom** になり、
`padding:{top:4, bottom:2}` で canvas 版の `cy = h - 2` に一致する。

| ゲージ | 中心 x,y | 閾値ゾーン帯 | 外側アーチ | 内側アーチ | 頂部/下端 |
|---|---|---|---|---|---|
| 単一 150×76（canvas 版） | 75, 74 | 67〜70 | 値 54.8〜66 | — | 4 / 2 |
| 単一 150×76（Chart.js 実測） | 75, 74 | 67〜70 | 値 56〜67 | — | 4 / 2 |
| dual 150×56（canvas 版） | 75, 54 | 47〜50 | 38〜46 | 31〜37 | 4 / 2 |
| dual 150×56（Chart.js 実測） | 75, 54 | 47〜50 | 39〜47 | 33〜39 | 4 / 2 |

中央値 16px の最大径 53.3px（実装メモ 2026-10-05）は新内縁 56px に収まり文字干渉なし。

### 周辺機構

- `tuneGauge()`（`gpu_power_cap_w` / `gpu_clock_max_mhz` / `gpu_temp_crit_c` / `net_max_mbps` による
  実測上限補正）は `data` 再計算のみに反映。`cutout`/`weight` は上限に依存しないため再構築不要
- 項目未提供時のカード非表示（`cardEl.style.display="none"`）と `renderThermal()` の
  `thr-high`/`thr-crit` 枠線着色は**無変更**（`cardEl` クラス操作のみ）
- `redrawSystemTab()` の `_last` 無効化は維持。ゲージは `responsive:false` なので `resize()` 不要
- CSS は `.gauge .gcanvas canvas` の `width:100%/height:100%` を削除（Chart.js が inline style で
  論理 px を指定するため）。`position:absolute; left:0; top:0` は `.gval` の配置基準として維持

### 検証 1: 描画の実測（headless Chrome）

この環境に node はなく `google-chrome` (155.0.8059.84) があるため、稼働中のページへ eval を注入し
chart 内部を直接実測した。初回は `--headless=new --dump-dom` で iframe 経由に注入する形で行った
（検証用 HTML は検証後に削除）。ただし**この環境では iframe の `onload` が発火せず JSON が取り
出せない**ことが後続セッションで判明したため、それ以降の再検証はすべて **CDP
（`--remote-debugging-port` + websocket の `Runtime.evaluate`）** に切り替えている
（再現手順とハマりどころは「検証 3」にまとめた）。

※ 上記は DGX Spark 実機の記録。**Whitebear2026 (WSL2) には当初 Chrome が無く**、2026-10-26 に
公式 `.deb` を rootless で展開して導入した（`sudo` が対話認証必須のため apt は使えない）。
`~/.local/opt/google/chrome` へ展開 + `~/.local/bin/google-chrome` にリンク + `~/.bashrc` の PATH へ
`~/.local/bin` を追加。起動は `--no-sandbox` 必須（`chrome-sandbox` を setuid root にできないため）。
導入版は 156.0.8078.47 で、`--headless=new` + CDP により前項と同じ実測が行える

- **canvas 属性 / inline style**: 単一 `150×76`・dual `150×56` が全 10 枚に付与されることを確認
  （`responsive:true` はコンテナ計測が効かず 300px 固定で右へはみ出す — operations-ui の教訓どおり）
- **dataset**: 単一 2 本・dual 3 本、`weight [3,11]` / `[3,8,6]`、`cutout 80%` / `66%`、
  `borderWidth [0,1]` / `[0,1,1]`。`g-gpu-clock` は `zoneStops` が `data [83.9, 6, 10.1]` に反映
- **寸法**: 上表のとおり中心・リング半径が canvas 版と一致（`arc.outerRadius`/`innerRadius` 実測）
- **0 始まりアニメなし**: 値リングの `arc.circumference` を 16ms×400 回サンプリングし、目標 % が
  変化した全フレームで `circumference < 0.05` は**ゼロ**（前回値からの連続遷移）
- **タブ切替**: システム → モデル → システム復帰後、全ゲージの `circumference` が目標角度に収束
  （worst diff 0.031 は CPU 負荷が変動し続けている分で、他は ≤ 0.001）
- **ゾーン色連動**: 統合メモリ 88.1% → 赤 `#f85149`、CPU 負荷 15.6% → 緑 `#3fb950`
- **スクリーンショット**: 1 段目 10 枚のゾーン帯 3 色・背景アーチ・リング間 1px・中央値 2 行・
  dual のゲージ下 2 行値を確認。JS コンソールエラーなし
- **atom2 (discrete) 分岐**: `PLAT.memory_mode="discrete"` を模擬して** 11 枚**（RAM / VRAM 追加）を
  生成。`g-ram` / `g-vram` は単一ゲージと同一寸法（`cutout 80%`・`weight [3,11]`・中心 75,74・
  `bg` 付きカード）で、値のゾーン色連動（RAM 87.5% → 赤、VRAM 62.5% → 橙）と `subVal` 2 行目も
  従来どおり。上限補正は `g-gpu-temp` max 110 / `g-gpu-power` max 250 (R9700 PPT) /
  `g-gpu-clock` max 2350 + `zoneStops` 標準（`data [60,25,15]`）が `data` に反映されることを実測。
  GB10 の `/api/metrics` は `vram_*` を返さないため `g-vram` は従来の missing 機構で非表示
  （Chart.js 化とは無関係な既存挙動）

### 検証 2: 移植前 (c9125f1) との寸法回帰比較（2026-10-20）

「単一 137px / dual 147.6px の 10.6px 差が移植で入ったものか」を切り分けるため、
`git show c9125f1:front/index.html`（canvas 直描画版）を `http://localhost:8099` に配信し、
新版（8080）と同じ CDP スクリプトで `getBoundingClientRect()` を照合した。**判定 24 項目 ALL PASS**。

| 実測項目 | 移植前 c9125f1 | 移植後（現行） | 差分 |
|---|---|---|---|
| カード高さ 単一 9 枚 | 137 | 137 | 0.0 |
| カード高さ dual 2 枚（`g-disk-io` / `g-net`） | 147.6 | 147.6 | 0.0 |
| `.gtitle` | 17 | 17 | 0.0 |
| `.gcanvas`（単一 / dual） | 76 / 56 | 76 / 56 | 0.0 |
| `.gval-dual-out`（dual のみ） | 28.6 | 28.6 | 0.0 |
| `.gsub` | 16（`g-gpu-load` のみ 14） | 16（同） | 0.0 |

**単一と dual の 10.6px 差は移植前から同じ**（dual は `.gcanvas` が 56px に縮む代わりに
`.gval-dual-out` 28.6px がゲージ下へ出る）。CSS 上 `.gauge.dual .gcanvas{height:56px}` と
`.gval-dual-out` は今回の変更で無変更であり、**Chart.js 移植はカード外寸を一切変えていない**ことを
実測で確定した。あわせて新版側でも canvas 属性・options・リング半径と中心、タブ復帰後の角度収束、
atom1/atom2 の枠線着色 7 ケース、ヘッダ解決色 `rgb(154,123,31)`（= `--thr-yellow` `#9a7b1f`）を再確認した。

### 検証 3: CDP 方式の再現手順とハマりどころ

```bash
# 1) 移植前を 8099 で配信。index.html は chart.umd.js を相対参照するので /static/ 配置が必須
#    (それ以外だと 404 → Chart 未定義で落ちる)
mkdir -p /tmp/oldfront/static && cd ~/DGXSparkUtil
git show c9125f1:front/index.html \
  | sed 's|const API = "";|const API = "http://localhost:8080";|' > /tmp/oldfront/index.html
cp front/chart.umd.js front/meter-dgx.ico /tmp/oldfront/static/
(nohup python3 -m http.server 8099 --directory /tmp/oldfront >/tmp/hs.log 2>&1 &)

# 2) CDP 付き headless Chrome を起動 (--remote-allow-origins=* が必須。* はクォートする)
(nohup google-chrome --headless=new --disable-gpu --no-sandbox \
   --remote-debugging-port=9333 '--remote-allow-origins=*' \
   --user-data-dir=/tmp/cud >/tmp/cdp.log 2>&1 &)

# 3) 計測スクリプトで新版(8080) → 旧版(8099) → 新版を順に実測し PASS/FAIL を印字
python3 -m venv /tmp/jsenv && /tmp/jsenv/bin/pip install -q websocket-client
(nohup /tmp/jsenv/bin/python -u /tmp/verify_gauge.py > /tmp/gauge_report.txt 2>&1 &)
```

※ `verify_gauge.py` はリポジトリ外の使い捨てスクリプト（`/json/list` で page ターゲットを取り、
`Runtime.evaluate` で `gages[]` の `chart.data.datasets` / `getDatasetMeta().data`（`innerRadius`
`outerRadius` `x` `y` `circumference` `startAngle` `endAngle`）と `cardEl.getBoundingClientRect()`
を文字列化して返すだけ）。旧版側は `chart` を持たない（canvas 直描画）ので、寸法比較は
`document.querySelectorAll("#tab-system .gauge")` の `.gtitle` / `.gcanvas` / `.gval-dual-out` /
`.gsub` の高さを同じ式で読めばよい。

はまった点（次回セッション向け）:

- **`--remote-allow-origins=*` がないと WS handshake が 403**。Chrome 155 は既定で DevTools への
  Origin チェックを行う（`Rejected an incoming WebSocket connection from the http://127.0.0.1:9333
  origin`）。`/json/new` は GET 不可で **PUT** が必要
- **`Runtime.evaluate` の戻り値は `result.result.value` にネスト**している
- **navigate 直後は 8〜9 秒の実時間待機**が必要。仮想時間待機だとコンテキスト未確立で evaluate が
  空を返す
- **JS の `Array.join()` 後は真偽値が小文字 `"false"`**。Python 側 `str()` 想定の `"False"` と
  比較すると、設定が正しくても誤 FAIL する
- **`Math.round(r*10)/10` は 67.0 を `"67"` と文字列化**する。期待値を `"67.0-70.0"` にすると
  同じく誤 FAIL（実測は一致していた）
- 計測行の**列違い**で `float('150x76')` の ValueError を踏んだ（カード高さは canvas 属性の別列）。
  判定式は「値が正しいか」だけでなく「どの列を見ているか」も照合する
- **`pkill -f 'remote-debugging-port=9333'` は自爆する**（自分のコマンドラインにも同じ文字列が
  入り、シェルごと SIGTERM で終わる）。落とすなら `fuser -k 9333/tcp 8099/tcp`
- **stdout はファイルリダイレクト時ブロックバッファ**。進捗を確認しながら待つなら `python -u`

### 既知のトレードオフ

- `bg: rgba(色,.10)` を敷くカード（統合メモリ / RAM / VRAM / GPU 負荷）では、1px の擬似余白が
  `CARD_BG` 固定のためカード実色とわずかにズミる（operations-ui と同じトレードオフ）
- `borderWidth:1` は値セグメントの内外境界にも乗るため、アーチ端が 1px 欠けて見える
  （operations-ui で承諾済みの挙動）
- 現行 canvas 版は即時描画だったが、Chart.js 化で **1000ms / easeInOutQuad のアニメが追加**された
  （POLL_MS=2000 と両立。不要なら `animation:false`）

## WSL2 モデル切替バックエンド modelctl (2026-10-21)

Whitebear2026 (WSL2 Ubuntu 26.04 / RTX 5090 32GB) を第 3 の対象機材として導入した。
**docker compose を新設せず**、既存の汎用ランチャー `~/scripts/model-ctl.sh`
(RTX 5090 上の 14 モードを VRAM 排他で切替) をそのまま Web UI の切替バックエンドに使う。
atom1 (dgx-spark) / atom2 の `switch_models.sh` と同じ役割を `model-ctl.sh` が担う、という対応関係。

### プラットフォーム検出と backend 分岐

- `api/config.py` の検出順を `DGXUTIL_PLATFORM 明示 > WSL2 (/proc/version に microsoft) >
  nvidia-smi > amdgpu sysfs` にした。**WSL 判定は非 WSL では常に偽**なので
  dgx-spark / atom2 の検出結果は不変
- `PLATFORMS["wsl"]` を**追加** (既存 2 エントリのキー・値は変更していない)。
  `label=Whitebear2026-WSL` / `gpu_backend=nvidia` / `memory_mode=discrete`
  (RAM 82GB + VRAM 32GB → atom2 と同じ 2 ゲージを再利用) / `net_max_mbps=1000` /
  `exclusive_llm_rag=False` / **`backend=modelctl`** /
  `model_ctl=/home/cliclie/scripts/model-ctl.sh` / `rag_enabled=False` /
  `params_editable=False` / `gpu_temp_crit_c=95.0`
- 未設定キーは `_cfg()` の既定値に落ちるため、`backend=compose` / `rag_enabled=True` /
  `params_editable=True` = 既存 2 環境は従来どおりの解決値
- 上書きフック: `DGXUTIL_PLATFORM` / `DGXUTIL_COMPOSE_DIR` / `DGXUTIL_RAG_DIR` に加え
  `DGXUTIL_MODEL_CTL` (ランチャーのパス)

### ブリッジ (api/wsl_modelctl_bridge.sh) と modelctl.py

`model-ctl.sh` は **source して関数群を使う**設計なので、本体を 1 行も変更せずに済む。

- ブリッジが `source` 後に関数を**上書き定義して非対話化**する:
  `_mc_ask(){ return 0; }` (稼働中を停止して切替える確認 = 常に yes)、
  `_mc_mode_download(){ return 1; }` (重み未導入モードのその場 DL は走らせない。
  一覧の `ready=false` をフロントが受けて選択・切替を拒否する)
- サブコマンド `list` / `status` / `switch <mode>` / `stop <mode>` / `unload` / `params <mode>`。
  `list` は 1 モード 1 行のタブ区切り
  `mode / label / port / dir / ready / running / engine / log`
  (14 モードで 0.6 秒。`api/modelctl.py` が 1.5 秒 TTL でキャッシュ)
- `api/modelctl.py` が **vllm.py の compose 版と同じ形** `{profile: {service, container, port, ...}}`
  を返し、`api/vllm.py` は `_load_profiles()` の入口と `get_status` / `switch_model` /
  `stop_model` / `get_log` / `get_params` / `get_cline_config` の 6 箇所で
  `if _is_modelctl():` 分岐するだけ。**compose 経路のコードは無変更**
- 切替は既存の `_start_job` (`/api/vllm/job` でポーリング) に `_mc_launch` を渡す。
  `_mc_launch` が排他停止 → 起動 → `/health` 待機まで内包するため、
  14 モード固有の判定 (bonsai / mimo の docker inspect、Strata 5 モードの
  `/v1/models` id による代表集約など) を再実装していない
- 切替ジョブは `Popen(start_new_session=True)` で**セッション**を切り離す (uvicorn の
  kill で一緒にシグナルを受けないため)。ただし **cgroup は切れない**ので、
  `systemctl restart dgx-spark-api` からモデルを守るには unit の `KillMode=process` が要る
  (`start_new_session` だけでは不足だったのは実測で確認。compose 側は従来どおり
  セッション継承のまま = モデルが docker コンテナなので影響ない)

### 管理対象モード (WSL2 の model-ctl.sh / 14 モード)

RTX 5090 1 枚では `model-ctl.sh` が 14 モードを **VRAM 排他**で管理する
(同時起動不可。Web から切替すると稼働中を自動停止してから起動する)。
一覧は `bash api/wsl_modelctl_bridge.sh list` で取得できる（モデル名・導入状態は
実機の状態が変わるので記載しない）。

| モード | エンジン | API ポート | 備考 |
|---|---|---|---|
| `exl` | exllama | 8888 | ExLlamaV3 kit (serve_openai.py) |
| `tabby` | tabbyapi | 5000 | TabbyAPI |
| `swift40` / `swift35` | tabbyapi | 5001 / 5002 | 同一 dir、config 違い |
| `qwen9` | tabbyapi | 5003 | 9B |
| `bonsai` | llamacpp-docker | 8081 | 稼働判定は `docker inspect` |
| `swiftqwen38` | sglang-docker | 8040 | 稼働判定は `docker inspect` |
| `mimo` | sglang-docker | 8050 | 稼働判定は `docker inspect` |
| `strata15` / `strataS4` / `strataU4` / `strataq3` / `strataC1` | strata | **8080** | 5 モードが port/process 共有 → 代表 1 件に集約 |
| `llmjp` | llamacpp | 8060 | llama-server (`--metrics` 未指定) |

- **API の待ち受けは 8090**：Strata 系が 8080 を占有するため本リポジトリの既定 8080 は
  使えない (`install_service.env` の `PORT=8090`)
- 重み未導入のモードは `ready=false` で返り、フロントは**モデル名を灰色**にして
  選択・切替を不可とする（モデルタブ常設一覧とヘッダのモデル選択プルダウンが対象。
  プルダウン側の灰色は実装メモ 2026-10-27。`model-ctl.sh` 側はその場 DL を尋ねるが、
  ブリッジでは `_mc_mode_download` を上書きして走らせない）

### エンジン種別ごとのメトリクス源 (ログ解析は初期実装)

| モード | engine | 主経路 | ログ解析 |
|---|---|---|---|
| `tabby` `swift40` `swift35` `qwen9` | tabbyapi | ログのみ (`/metrics` 無し) | 既存 `_tabbyapi_parse_logs` を**ファイル入力でも再利用** |
| `strata15` `strataS4` `strataU4` `strataq3` `strataC1` | strata | `/metrics/prometheus` + `/slots` | `[strata] done:` / `thinking:` / `answering:` 行 |
| `llmjp` | llamacpp | `/slots` (`--metrics` 未指定) | `print_timing` 行 |
| `exl` | exllama | `/health` の累計カウンタ差分 | — |
| `bonsai` `swiftqwen38` `mimo` | llamacpp-docker / sglang-docker | 既存の HTTP + `docker logs` をそのまま利用 | 同左 |

- **Strata は `serve/server.py` が `/metrics/prometheus` を llama.cpp と同名系列
  (`llamacpp:prompt_tokens_seconds` 他) で公開している**ため、既存 `_llamacpp_metrics` が
  そのまま流用できる。加えてログの `expert cache NN.N% hit` を
  `expert_cache_hit_pct` (Strata 独自のキャッシュ命中率) として補完する
- **TabbyAPI の起動ログ `logs/ctl-*.log` は docker logs と同じ
  `HH:MM:SS.mmm LEVEL:` 接頭辞**だった (loguru 実ファイルは
  `YYYY-MM-DD HH:MM:SS.mmm | LEVEL |`)。両形式を受理する
  `_TABBY_ENTRY_RE_ANY` を追加し、`_tabbyapi_metrics(base, container, log_text=None,
  headers=None)` に引数を**末尾追加** (省略時は従来どおり `docker logs`)
- llama.cpp は `--metrics` 未有効のため `/slots` + ログの
  `prompt eval time / eval time / total time` 行から prefill/decode・TTFT・E2E を算出
- ExLlamaV3 kit は `/health` の `prompt_tokens_total` / `completion_tokens_total` を
  `vllm:*_total` に寄せて既存 `_tps_of` のカウンタ差分機構を再利用
- WSL の TabbyAPI は `/v1/models`・`/v1/model` に API キーが必要
  (`model-ctl.sh` の `_mc_mode_key` が正本)。ブリッジの `params` が返す
  `api_key` を curl ヘッダとして渡す (`headers=None` なら無認証 = 従来動作)

### フロント / 表示

- `/api/platform` に `backend` / `rag_enabled` / `params_editable` を追加
- `rag_enabled=false` → RAG セクションを非表示 + `/api/rag/status` の poll を省略
  (WSL に RAG compose は無い)
- `params_editable=false` → 「保存してコンテナを再作成」ボタンを非表示。
  `/api/vllm/params` は `model-ctl.sh` が読む値 (コンテキスト長・ポート・設定 JSON 等) を
  **`editable:false` で表示のみ**返す。`edit_params` は modelctl では 409 を返す
- `backend=modelctl` → 見出しを「モデル (model-ctl.sh)」、「コンテナ状態」→「状態」
- モード一覧の `ready=false`（重み未導入）は **モデル名を灰色 (`--muted`) にして**導入済みと区別し、
  「重み未導入」 suffix + 選択不可（切替ボタンも不可）にする。compose 環境は `ready` を
  返さないためクラスが付かず、表示は従来どおり
- **P-State の色分けに `P5` / `P7` を追加**（黄 = 中間状態）。実装メモ(2026-09-03) の
  分類は GB10 で観測される P0 / P2 / P3 / P8 / P12 / P15 のみで、GeForce (RTX 5090) で
  頻出する P5 が「それ以外」に落ちて**赤（異常に見える）**だった。
  GB10 / R9700 では P5・P7 が観測されないため既存環境の表示は実質不変
  - 併せて調査: ダッシュボードの P-State が `nvidia-smi` と「違う」ように見える件は、
    同時刻サンプル 12 回で全一致（`/api/metrics` は都度 `nvidia-smi` を叩く即値・ラグ 0.0 秒）。
    差が出る原因は (1) フロントの 2 秒ポーリング vs 打った瞬間の値、
    (2) `nvidia-smi` 実行自体が GPU を起こす (アイドル 20 秒後の初回サンプルが P5 に戻った)、
    (3) ブラウザが裏タブ時の `setInterval` 抑止 で、P8 / P5 は秒刻みで交互していた
- `api/metrics.py` の nvidia 経路は `memory_mode=discrete` のときだけ
  `memory.total/used`, `clocks.max.sm`, `power.limit` を追加取得して
  `vram_*` / `gpu_clock_max_mhz` / `gpu_power_cap_w` を出す
  (unified = GB10 はクエリ・キーとも従来どおり)。WSL の nvidia-smi は温度上限を
  公開しないため `gpu_temp_crit_c` はプラットフォーム既定値 95°C を使う

### 検証 (Whitebear2026 実機 2026-10-21)

- `/api/platform` = `wsl` / `backend=modelctl`。`/api/metrics` で
  RAM 82.48 GiB・**VRAM 31.84 GiB 中 2.10 GiB**・GPU 37°C / 49.7W / 上限 600W / 3090MHz を実測
- `swift40` (TabbyAPI) へ切替 → 19 秒で job 完了。実リクエスト後に
  `e2e 4.23 s / ttft 0.30 s / prefill 226.7 tok/s / decode 76.4 tok/s / kv_cache 0.14%`
  をログ解析で取得 (`/v1/model` の `cache_size 262144` を反映)
- `swift40` → `strataq3` へ切替 → **稼働中 TabbyAPI を自動排他停止**してから 82 秒で起動完了。
  `/metrics/prometheus` + `/slots` + ログで
  `requests_running 1 / kv_cache 0.026% / e2e 5.0 s / decode 77.5 tok/s / expert_cache_hit 83.4%`、
  `model_name=swift-1.5-iq3_s` を実測
- `/api/vllm/stop` (strataq3) → 15 秒で停止成功。`/api/vllm/log` は `logs/ctl-*.log` の
  末尾行を返す。`/api/vllm/cline` は LAN IP + API キー + ctx 262144 を返す
  (WSL 内は mDNS 解決不可のため `hostname.local` ではなく LAN IP。`model-ctl.sh` の案内と同じ)
- **model-ctl.sh 無変更**: `git -C ~/Whitebear2026-WSL2 status --porcelain scripts/` が空、
  `~/scripts/model-ctl.sh` の mtime も運用前そのまま (対話 CLI `llm` / `tabby` / `unloadllm` に影響なし)
- **systemd 導入後の再検証 (2026-10-21 05:33)**: `dgx-spark-api.service` (system.slice /
  `LimitNOFILE=1048576` / `KillMode=process`) 経由で `swift40` へ切替 → `job success=True`。
  リクエスト後に `e2e 1.56 s / ttft 0.21 s / prefill 300 tok/s / decode 162.8 tok/s /
  kv_cache 0.108%` をログ解析で取得。`MainPID` を kill して自動再起動 (Restart=always) しても
  **稼働中 TabbyAPI は生存** (api=200 / model health=200) を実測
- 稼働時間は起動ログの **ファイル名 `ctl-YYYYMMDD_HHMMSS.log`** (固定名ログは birth time) から
  算出する。mtime は追記で進むため採用しない (実測: 初版はリクエストのたびに 00:00:00 に戻った)

### atom1 / atom2 との互換性 (回帰実測)

改修前 (HEAD) と改修後のコードを同一環境で突き合わせた
(`DGXUTIL_PLATFORM=dgx-spark` / `atom2` + ダミー `DGXUTIL_COMPOSE_DIR`):

- 一致: `_load_profiles()` のプロファイル表、`SWITCH_SCRIPT` / `COMPOSE_FILE` のパス、
  `get_params()`、`_tabbyapi_parse_logs()`、`_llamacpp_metrics()`、`_sglang_metrics()`、
  `get_status()` のキーと `containers`
- 差分は `/api/platform` の**追加 3 キーのみ**
  (`backend="compose"` / `rag_enabled=true` / `params_editable=true` — いずれも従来動作)
- `install_service.sh` の `PORT` 既定値は 8080、thermal-guard は WSL 以外では従来どおり
  無条件に enable + restart。unit の `__PORT__` は既定レンダで 8080 になる

### 常駐化 (WSL)

```bash
sudo bash /home/cliclie/DGXSparkUtil/api/install_service.sh   # api/install_service.env を読む
```

- はまった点: **WSL の sudo は `-E` (環境保持) 非対応**で
  `PORT=8090 sudo -E bash install_service.sh` が
  `sudo: preserving the entire environment is not supported, '-E' is ignored` で落ちる。
  → ホスト固有値は `api/install_service.env` (git 管理外) に置いて解決した
- `systemd-analyze verify` でレンダリング後 unit の検証 OK
  (`KillMode=process` / `LimitNOFILE=1048576` / `--port 8090`)
- **はまった点 (systemd 化後にモデル切替が `CUDA driver error: unknown error` で失敗)**:
  原因は **systemd サービス既定の `LimitNOFILE=1024`**。WSL2 の CUDA (D3D12 / `/dev/dxg`) は
  モデルロード中に 1024 を超えるハンドルを開くため途中で失敗した。切り分けの実測順:
  1. system サービス経由の切替 → 失敗 (78% で `torch.empty` が CUDA error)
  2. **同じ環境変数を `/proc/<MainPID>/environ` から注入**してログインシェルから実行 → 成功
     (環境変数は無関係)
  3. `systemd-run --user --scope` (user.slice) から実行 → 成功 (cgroup も無関係)
  4. ログインセッションのまま `ulimit -n 1024` → **同じエラーが再現** → 確定
  5. `LimitNOFILE=1048576` を与えたサービスで切替 → 成功 (`job success=True`)
  - 教訓: 「system.slice が悪い」と早合流して `Slice=` / user サービス導入を一時実装したが、
    **`/proc/<pid>/limits` を先に見る**べきだった (余計な分岐は撤回した)

## api venv のセキュリティ/保守更新と依存ピン留め（2026-10-23）

稼働環境全体の CVE 調査（OSV / GitHub Security Advisories / Ubuntu CVE Tracker）で、当リポジトリの
`api/venv` と `~/LLM/tools/hf-venv` に既知 CVE が 2 件該当したため更新した。調査の全貌と
コンテナ基盤（docker-ce 29.9.2 / containerd.io 2.4.2）の移行記録は `~/LLM/README.md` の
2026-10-23 エントリ参照。

- **更新（api venv）**: `opentelemetry-api` 1.45.0 → **1.45.1**（GHSA-xcq7-p3pf-p88h — W3C
  `tracestate` ヘッダを先頭が無制限の `[ \t]*` 正規表現で split するため O(N²) に転び、
  未認証 1 リクエスト（約100KB）がワーカーを 6〜12 秒占有）。当サービスは propagator を明示設定
  していないが、starlette が `starlette/middleware/opentelemetry.py` から import するため
  削除ではなく更新を選んだ。併せて保守として fastapi 0.142.2 → **0.161.1**、starlette 1.7.0 →
  **1.9.0**、pydantic 2.13.5 → **2.14.1**（core 2.50.1）、websockets 17.1 → **17.3**、
  httptools 0.8.0 → **0.9.0**（いずれも OSV 該当なし）。
- **更新（`~/LLM/tools/hf-venv`）**: `certifi` 2026.7.22 → **2026.10.19**（GHSA-6q48-6c7w-9gw2 —
  emSign 漏洩鍵のルート CA 同梱。更新後 bundle は `G1 / ECC-C3 / C1` のみで、問題の **ECC-G3
  非包含**を実データで確認）。他 hf-xet 1.7.0 / tokenizers 0.23.3 / filelock 4.2.4 /
  truststore 0.10.5。
- **`requirements.txt` の下限引き上げ**: `install_service.sh` の `ExecStartPre` は venv 不在時に
  `pip install -r requirements.txt` を実行するため、`fastapi>=0.110` のままでは脆弱な旧版が
  再導入されうる。starlette / pydantic は推移的依存だが下限を実測版まで明示した。
- **`requirements.lock` を新規**（実測済みの完全ピン版・再現用）。`requirements.lock.pre-update` は
  更新前のスナップショット（ロールバック用）。
- **検証 (2026-10-23)**: `./venv/bin/python -c "import main"` → `routes=22` で import OK。
  `sudo systemctl restart dgx-spark-api` 後、`/api/platform`・`/api/metrics`・`/api/history`・
  `/api/vllm/status`・`/api/rag/status`・`/api/thermal`・`/`・`/static/chart.umd.js` がすべて 200。
  LAN 内クライアントからの定期ポーリング（`/api/thermal`・`/api/vllm/job`・`/api/rag/job`）も
  server.log 上で 200 継続を確認。
- **rebase 後の再検証 (2026-10-23)**: `origin/main` の WSL2/modelctl 系（`api/modelctl.py`、
  `api/vllm.py` の `backend` 分岐、`api/config.py` の `PLATFORMS["wsl"]`）を取り込んだ後も
  `import main` が `routes=22` で通り、`systemctl restart dgx-spark-api` 後に全 endpoint 200
  （`/api/rag/status` は初回のみ docker 照会で 12 秒台、以降 0.15 秒）。atom2 は `/api/platform` が
  `backend=compose`・`rag_enabled=true` を返し、WSL 判定は非 WSL では常に偽なので既存 2 機の挙動は不変。
  新コードに pip 追加依存はない（`modelctl.py` は標準ライブラリ + `config` のみ）。
- **据え置き**: uvicorn 0.54.0 / anyio 4.15.1 / idna 3.20 / PyYAML 6.0.3 / python-dotenv 1.2.4 /
  uvloop 0.23.0 / watchfiles 1.3.0 / h11 0.16.0 / click 8.5.0 は PyPI 最新かつ OSV 該当なし。
  `front/chart.umd.js`（Chart.js 4.4.9）は OSV 該当なし（最新 4.6.1、更新は任意）。
- **変更していない運用**: バインド（`0.0.0.0:8080`）、CORS `allow_origins=["*"]`、
  POST 系エンドポイントの認証なしは**従来どおり維持**（LAN 内運用を前提とした既存仕様。
  2026-10-23 の調査では露出低減案も検討したが、運用変更は見送った）。

## 履歴書き換え（API key 除去）後の他機同期手順（2026-10-23）

2026-10-23 に `git filter-repo` で README の Magnitude API key 実値を**履歴ごと**除去し、
`main`（`96a8fb5 → 6ec972e`）と `atom2対応`（`31397e1 → 10eec7e`）を force push した。
これより前に clone した機（**dgx-spark = atom1 / atom2 / Whitebear2026-WSL2**）は履歴が分岐するため、
`git pull` は `refusing to merge unrelated histories` で拒否されるか、通った場合は同じメッセージの
重複コミットをマージで抱え込む（filter-repo は全コミットのハッシュを変えるため、新旧の系譜に共通祖先がない）。
書き換え前の全 refs バックアップは **atom2 上の** `~/temp/DGXSparkUtil-pre-redact-20261023-0931.bundle`
（430 KB・他機には存在しない。必要なら atom2 から `scp` で取得）。

### 1. リポジトリの同期（各機で）

```bash
cd /home/cliclie/DGXSparkUtil
cp -a api/install_service.env /tmp/install_service.env.bak 2>/dev/null || true  # WSL2 のみ (PORT=8090 等)
git status --short            # tracked の未コミット変更を確認
git stash                     # 変更がある場合のみ。-u は使わない
git fetch origin
git reset --hard origin/main
git branch -f atom2対応 origin/atom2対応 2>/dev/null || true   # 同名ローカルブランチがある場合
```

- `api/install_service.env` は untracked なので `reset --hard` では消えないが、
  `git stash -u` や `git clean -fd` を併用すると失う。その場合は `/tmp` のバックアップか
  `git stash pop` で復元すること。無いと `install_service.sh` が `PORT=8080` /
  `KILL_MODE=control-group` / `LimitNOFILE` 行なしの既定で再レンダリングしてしまう
  （WSL2 では 8080 が Strata 系と衝突し、`LimitNOFILE` 欠落でモデル切替が CUDA driver error になる）

### 2. venv の依存更新（各機で必須・pull だけでは反映されない）

`api/venv/` は git 管理外で、`dgx-spark-api.service` の `ExecStartPre` は
**venv が存在しないときだけ** `pip install -r requirements.txt` を実行する。
既存 venv を持つ機は明示的に当てる必要がある（2026-10-23 に opentelemetry-api 1.45.1 の
CVE 修正と fastapi / starlette / pydantic / websockets / httptools の保守更新が入っている）。

```bash
cd /home/cliclie/DGXSparkUtil/api
./venv/bin/pip install -U -r requirements.txt      # 下限追従
# 実測ピン版に揃える場合: ./venv/bin/pip install -r requirements.lock
./venv/bin/python -c "import fastapi, starlette, pydantic, opentelemetry.trace; print(fastapi.__version__, starlette.__version__, pydantic.VERSION)"
sudo systemctl restart dgx-spark-api
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/api/platform   # WSL2 は 8090
```

### 3. 同期の検証

```bash
git --no-pager log --oneline -1                     # 6ec972e 以降を指すこと
git status -sb | head -1                            # ahead/behind が出ないこと
git --no-pager log -S'<旧 API key の実値>' --all --oneline   # 空であること（実値は本誌に記載しない）
```

- 旧コミットは reflog に一定期間残る。即座に消したい場合のみ
  `git reflog expire --expire=now --all && git gc --prune=now`
- 以降も `git pull` を使うなら `git config pull.ff only`（または `pull.rebase true`）を推奨。
  force push 直後の pull はマージコミットを生成する
- 他機で modelctl バックエンド（WSL2）を使う場合は、同期後に `~/scripts/model-ctl.sh` の実在を確認する。
  `api/config.py` は既定で `PLATFORMS["wsl"].model_ctl = /home/cliclie/scripts/model-ctl.sh` を使い、
  上書きは環境変数 `DGXUTIL_MODEL_CTL`。**`install_service.env` に書いても api プロセスには届かない**
  （`install_service.sh` が自プロセス内で export するだけで、`dgx-spark-api.service` のテンプレートに
  `Environment=` 置換がない）。既定パスに置けない場合は unit に直接
  `Environment=DGXUTIL_MODEL_CTL=/path/to/model-ctl.sh` を追記して `daemon-reload` + `restart` すること

### 4. 残る前提（重要）

force push は GitHub 側の**参照の付け替え**であり、旧コミット（`96a8fb5` / `f8ed431`）が
commit URL 直アクセス・PR / 通知キャッシュ・fork から即時に消える保証はない。
実値の鍵自体は有効なままなので、**鍵のローテーションが本質的な解決**
（前節「API key の指定」の手順。`.env` と `~/.magnitude/config.json` を同一値に揃えて
`magn-headless` と `dgx-spark-api` を両方再起動すること。`config.py` は import 時に `.env` を読む）。

### 5. 各機の同期状況

- **atom1 (WhitebearATOM1)**: 2026-10-28 に `2df7683` まで反映・push 済み（**本機が実装元**）。
  取り込み内容は 5 コミット — ornith122b の Cline 設定値対応（`a92c5d5`）、サーマルガード対象の
  コンテナ名リスト化（`89af358`）とその実戦検証記録（`33b4a3c`）、長文推論中のポーリング緩和と
  GPU クロック実値表示（`8d7ad6c`）、同期状況の記録（`2df7683`）。`sudo systemctl restart
  dgx-spark-api` を **12:00:21 に実施済み**（実測: prefill 中 `/api/vllm/status` 1.58〜1.61 秒で
  `busy=true`・モデル名と metrics を維持、アイドル 0.31 秒、`server.log` 直近 200 行にエラーなし）。
  フロントは静的配信なのでブラウザリロードで反映。**atom2 は `1323f68` で 5 コミット未同期** —
  `git merge --ff-only origin/main` で取り込み可。`api/thermal_guard.sh` の対象リストは clock モード
  専用（power モードのループは `CONTAINERS` を読まない）なので atom2 のガード動作に変更要、
  `api/vllm.py` 側は **API 再起動が必要**。
  2026-10-27 に `4700cc8` まで同期済み（本機。`git pull --ff-only origin main`
  で `861d946` → `4700cc8` の 8 コミットを fast-forward、作業ツリーはクリーン・コンフリクトなし）。
  取り込み内容は strata（Strata / atom2）の Cline 設定値対応（API key 表示と `/health` 動的取得、`ef845fa`）、
  strata の prefill/decode スループット対応（`/metrics` JSON・Bearer 必須からマッピング、`24c36bd`）、
  strata metrics の idle/unloaded 正規化と直後 5 秒キャッシュ（`04930ca`）（`api/config.py` / `api/vllm.py`）、
  ヘッダのモデル選択プルダウンで重み未導入を灰色表示（`front/index.html`、`6fbf245`）、
  各機の同期状況記録 README（`9fe469f` / `e0fb35c` / `728decf` / `4700cc8`）。
  フロントは静的配信のためブラウザリロードで反映（API 再起動不要）。実測: 配信中の `/`（ポート **8080**）は
  リポジトリの `front/index.html` とバイト一致し灰色表示コード（`muted` / 「未導入」）を含む、
  `/api/metrics` は 200、`dgx-spark-api` は active。
  strata 系の API 変更は strata プロファイル（atom2）でのみトリガーされるため本機（dgx-spark）の
  ランタイムには影響なし（`CLINE_MODEL_TABLE` の strata 項は atom2 用セクション）。
  2026-10-24 に同期済み（`c3fbe98` へ reset + `atom2対応` ブランチ同期 /
  venv を `pip install -U -r requirements.txt` で更新: fastapi 0.162.0 / starlette 1.9.0 / pydantic 2.14.1 /
  uvicorn 0.54.0 / opentelemetry-api 1.45.1 / API 再起動し `/`・`/api/platform`・`/api/metrics`・
  `/api/vllm/status` が 200・thermal-guard active を確認）。
  unit ファイルの再レンダリングは未実施（root 権限が必要。atom1 では新旧の機能差がゼロのため
  運用に無影響）。
  **2026-10-30: 本機で GPU クロックゲージのサブ行を「最大 <実測値> MHz」に変更して push した**
  （`front/index.html` と README のみ。従来の `実 <現在値> / 最大 <HW上限> MHz` を運用者依頼で
  `最大 <実測クロック> MHz` に簡略化。サーマルガード発動中の `実 X / ロック Y MHz` 表示、
  ゲージ目盛り（`tuneGauge()`）と統合時系列の 100% 基準（`CHART_NORM.gpuClock`）は変更なし。
  フロントは静的配信なのでブラウザリロードで反映、`dgx-spark-api` は active のまま再起動不要。
  実測: headless Chrome の構文解析で SyntaxError なし、`http://localhost:8080/` のスクリーンショットで
  GPU クロックカードのサブ行が「最大 1665 MHz」（1665 = `gpu_clock_mhz` の実測値）を確認。
  詳細は実装メモ 2026-10-30）
  - push 前に `git pull --rebase origin main` で Whitebear2026-WSL2 が 2026-10-30 に push した
    2 コミット（DISK ゲージと統合時系列の 100% 基準 7 GB/s 化 `860c938`、同期状況記録 `1ccdc64`）を
    取り込んだ。衝突なし — 本機の変更は `gaugeDefs()` の GPU クロック節、リモート変更は DISK 節
    （`diskMax`）と `CHART_NORM` 行で領域が非重複。実測: 統合後の `front/index.html` に
    `最大 ${real} MHz` と `diskMax` が共存していることを確認
- **atom2 (WhitebearATOM2)**: 2026-10-28 に `1323f68` まで同期済み（本機。HEAD の
  `1323f68` を含む。本機は strata 系 3 コミット（`ef845fa` / `24c36bd` / `04930ca`）の
  **実装元**で、2026-10-28 の Strata 0.2.7（engine 0.1.52）更新後も `/metrics` JSON・`/health` の
  フィールド互換を維持していることを実 HTTP で再確認（**コード変更不要**）。`dgx-spark-api` は
  `.env` の `STRATA_API_KEY` 更新（2026-10-27）と strata 系コミットのたびに `systemctl restart` 済み
  （`config.py` は import 時に `.env` を読むため）。実測: `GET /api/vllm/cline` は
  `api_key=実キー48文字` / `model_id=qwen3.8-flash-next-iq3_xxs` / `context 131072` / `images true`、
  `GET /api/vllm/status` は strata 稼働カードで decode/prefill（0.2.7 実測 60.12 / 158.75 T/s の
  live 瞬時値）を返し、アイドル時は 0 表示。詳細は「Strata (strata・atom2) の Cline 設定値と
  prefill/decode 表示（2026-10-26 / 10-28）」セクション参照。
  2026-10-27 に `77a5dd2` まで同期済み（`git fetch --all --prune` +
  `git merge --ff-only origin/main` で `04930ca` → `77a5dd2` の 4 コミットを fast-forward、作業ツリーは
  クリーン・コンフリクトなし）。取り込み内容はヘッダのモデル選択プルダウンで重み未導入を灰色表示
  （Whitebear2026 実装。`front/index.html` の `#hdr-model-select option.not-ready { color: var(--muted); }`
  と `hdrModelOptions()` が返す 3 番目の `notReady` フラグ、`6fbf245`）と、各機の同期状況を記録する
  README 追記（`728decf` / `4700cc8` / `77a5dd2`）のみで **`api/` 配下の変更はゼロ**。よって API 再起動は
  不要で、フロントは静的配信のためブラウザリロードで反映。実測: 配信中の `/`（ポート **8080**）は
  リポジトリの `front/index.html` とバイト一致し灰色表示コード（`not-ready` が両ファイルで 6 箇所）を
  含む、`dgx-spark-api` は active のまま `/api/metrics`・`/api/platform`・`/api/vllm/status` が 200。
  なお本機は strata 系 3 コミット（`ef845fa` Cline 設定値対応 / `24c36bd` prefill/decode スループット対応 /
  `04930ca` idle/unloaded 正規化と直後 5 秒キャッシュ）の実装元で 2026-10-26 にコミット済み、稼働中の
  `dgx-spark-api`（MainPID の cwd は `/home/cliclie/DGXSparkUtil/api`、`NRestarts=0`）は 2026-10-27
  **06:43:53 起動**で今回の pull（08:00:35）より後のため strata 系のコードはランタイムに反映済み
  （Whitebear2026 で必要だった 07:49 の再起動は本機では不要）。`atom2対応` ブランチも
  `origin/atom2対応` と同期済み（差分なし）。
  2026-10-26 に `861d946` まで同期済み（`git pull --ff-only origin main`
  で `a8ea7e5` → `861d946` の 2 コミットを fast-forward。vLLM 情報カードの状態表示を「稼働中」/「API応答なし」へ
  短縮するフロント変更（`front/index.html` 1 行）と、Whitebear2026 のリモート更新反映を記録する README 追記を
  取り込み。フロントは静的配信のためブラウザリロードで反映（API 再起動不要）。
  同じく 2026-10-26 に `abbc63d` まで同期済み（`git pull --ff-only origin main`
  で `993073b` → `abbc63d` の 8 コミットを fast-forward。WSL2 側が push した 2026-10-26 の一連のフロント改良
  （ヘッダ切替のタブ維持、プルダウンの稼働中モデル自動追随、dual メーターの単一レイアウト化、
  二段目グラフの凡例短縮、タイトル行から「 Monitor」と日時計を削除）と、server.log を JST 0 時に
  truncate する機構（`api/rotate_log.sh` / `api/install_logrotate_user.sh` / `dgx-spark-logrotate.{timer,service}`）を
  取り込み。フロントは静的配信のためブラウザリロードで反映（API 再起動不要）。`dgx-spark-logrotate.timer`
  の本機への適用は未実施（次の保守時に `bash api/install_logrotate_user.sh` を予定）。
  2026-10-24 にも同期済み（`origin/main` へ fast-forward で `e24aeec` まで更新。
  変更は `front/index.html`（静的配信）と README のみのため API 再起動不要。`dgx-spark-api` active のまま
  配信中の `/` に修正コードが含まれることを HTTP で確認。`/api/metrics` は
  `gpu_clock_max_mhz=2350.0` / `gpu_power_cap_w=300.0` を返し、グラフ基準は従来どおり 2350 MHz / 300 W
  （thermal_guard の電力制限発動中は基準も 250/210 W に連動して変わる挙動に）。
- **Whitebear2026 (WSL2 / RTX 5090)**: 2026-10-26 の一連のフロント改良（ヘッダ切替のタブ維持、
  プルダウンの稼働中モデル自動追随、dual メーターの単一レイアウト化、行高さ 108px 統一）を
  `origin/main` の HEAD として push した。**atom2 は 2026-10-26 に同期済み**（上記 atom2 項参照）、**atom1 は未同期**（各機で `git pull --ff-only origin main`。
  変更は `front/index.html` と README のみで API 再起動は不要、ブラウザのリロードで反映）。
  **本機 (Whitebear2026) でも 2026-10-26 にリモートの更新を反映済み**: `git pull --ff-only origin main` で
  `f44dc5a` → `a8ea7e5` の 5 コミットを fast-forward（作業ツリーはクリーン、コンフリクトなし）。
  取り込み内容は server.log を JST 0 時に truncate する機構（`api/rotate_log.sh` /
  `api/install_logrotate_user.sh` / `dgx-spark-logrotate.{timer,service}` と、system unit 版
  `install_service.sh` に追加された「JST 0 時 = そのマシンの何時何分」の換算 + 導入時の初回 truncate）、
  `server.log.*.zip` / `.rotate_log.*` を無視する `.gitignore`、タイトル行から「 Monitor」と日時計
  （`#clock` と 1 秒間隔の `setInterval`）を削除した `front/index.html`、atom2 の同期記録の README。
  フロントは静的配信のためブラウザリロードで反映（`dgx-spark-api` は active のまま再起動不要）。
  実測: 本機の API はポート **8090**（`dgx-spark-api.service`）で、配信中の `/` に `id="clock"` は
  存在せず `<h1 id="hdr-title">DGX Spark</h1>`（`document.title` は従来どおり「<ホスト名> Monitor」）、
  `/api/metrics` は 200。`dgx-spark-logrotate.timer` は**本機では未適用**（`systemctl list-timers` /
  `systemctl --user list-timers` とも 0 件、`api/server.log` は 13.9 MB のまま。適用するなら
  sudo 不要の `bash api/install_logrotate_user.sh`（user timer）か、`sudo bash api/install_service.sh`
  の再実行で system timer）。
  **さらに 2026-10-26 にリモートの更新を反映済み**: `git fetch --all --prune` +
  `git pull --ff-only origin main` で `c77202f` → `9fe469f` の 2 コミットを fast-forward
  （作業ツリーはクリーン、コンフリクトなし）。取り込み内容は、vLLM 情報カードの状態表示を
  「稼働中 (API応答)」/「稼働中 (API応答なし)」から「稼働中」/「API応答なし」へ短縮する
  `front/index.html` の 1 行変更（`861d946` / `renderVllm()` 内の `setV("v-status", …)`）と、
  atom2 が `861d946` まで同期したことを記録する README 追記（`9fe469f`）。
  フロントは静的配信のためブラウザリロードで反映（`dgx-spark-api` は active のまま再起動不要）。
  実測: 配信中の `/`（ポート **8090**）はリポジトリの `front/index.html` とバイト一致し
  `a.health ? "稼働中" : "API応答なし"` を含む（`/api/metrics` は 200）。
  なお 1552 行目の modelctl 側 `稼働中 (API応答)` は今回の変更対象外で従来どおり。
  **2026-10-27: 本機でヘッダのモデル選択プルダウンの重み未導入灰色表示を実装して push した**
  （`front/index.html` と README のみ。フロントは静的配信のためブラウザリロードで反映、
  `dgx-spark-api` は active のまま再起動不要。headless Chrome + CDP で展開時の描画（未導入 13 件が
  `--muted`、導入済みと RAG が `--text`）と、閉じた表示色・切替ボタン状態・ヘッダ高さの回帰なしを
  確認。詳細は実装メモ 2026-10-27）。**atom1 / atom2 も 2026-10-27 に同期済み**（上記各機項参照。
  atom1 は `4700cc8`、atom2 は `77a5dd2` まで）
  **2026-10-27: 同じく本機でリモートの 3 コミットも反映した**（`git pull --rebase origin main` で
  `e0fb35c` → `04930ca` の 3 件を、自前の灰色表示コミット（`6fbf245`）の下に積んだ。内容は
  `ef845fa` strata の Cline 設定値対応（API key 表示と `/health` 動的取得）、`24c36bd` strata の
  prefill/decode スループット対応（`/metrics` JSON・Bearer 必須）、`04930ca` strata metrics の
  idle/unloaded 正規化と直後 5 秒キャッシュ。更新対象は `api/config.py` / `api/vllm.py`）
  - 注意: 稼働中の `dgx-spark-api`（systemd system unit、起動 2026-10-27 06:39）は pull（07:46）
    前のコードで動いていたため strata 系 3 件がランタイムに未反映だった → **2026-10-27 07:49 に
    `sudo systemctl restart dgx-spark-api` を実行して反映済み**（`ActiveState=active` /
    `Result=success` / `NRestarts=0`、MainPID の cwd は `/home/cliclie/DGXSparkUtil/api` で
    作業ツリーを import。`04930ca` の新キャッシュ `_strata_last_metrics`（`api/vllm.py:163`）が
    稼働コードに存在）。実測: `/`・`/api/platform`・`/api/metrics`・`/api/vllm/status`・
    `/api/thermal`・`/api/rag/status` すべて 200、`journalctl -p warning` 0 件、
    `active=null`（LLM は未稼働のまま。containers 15 件、`ready=true` は swift40 / strataq3）。
    `/api/vllm/cline?profile=strataq3` は未稼働のため 409「稼働中のモデルがありません」で
    従来どおりの業務エラー。headless Chrome + CDP でも JS エラー 0 件、プルダウンの灰色表示
    （未導入 `--muted` / 導入済み・RAG `--text`）とボタン 66×24px・header 40px を再確認
  **2026-10-30: 本機で DISK ゲージと統合時系列グラフの 100% 基準を 7 GB/s に引き上げて push した**
  （`front/index.html` と README のみ。`PLAT.platform === "wsl"` の分岐で `7000`、DGX Spark / atom2 は
  `5000` のままなので他機は無影響。NVMe 直通の実運用で読込 7 GB/s 弱を観測し、5 GB/s 基準では
  ゲージとグラフが振り切れていたため。フロントは静的配信なのでブラウザリロードで反映、
  `dgx-spark-api` は active のまま再起動不要。実測: `/api/platform` が `platform: "wsl"` を返し、
  headless Chrome の `--dump-dom` で DISK ゲージのサブ行が「上限 7 GB/s」になることを確認、
  他ゲージ（NW 上限 10 Gbps など）に変化なし。詳細は実装メモ 2026-10-30）
  - push 前に `git fetch` + `git rebase origin/main` で atom1 が 2026-10-28 に push した 9 コミット
    （ornith122b の Cline 設定値対応 `a92c5d5`、サーマルガード対象のコンテナ名リスト化 `89af358` と
    実戦検証記録 `33b4a3c`、長文推論中のポーリング緩和と GPU クロック実値表示 `8d7ad6c`、
    実装メモ `2df7683` / 同期状況反映 `30a9cfb`、各機の同期記録 `77a5dd2` / `1323f68` / `8205571`）を
    取り込んだ。衝突は README の機能表と「ゲージの 100% 基準」行のみ → atom1 側の GPU クロック
    記述（`thermalClockLockMhz()`）を保ったまま disk 上限の注記を統合して解決
  - 注意（**解消済み**）: 稼働中の `dgx-spark-api` は 2026-10-30 06:13 起動時点では pull 前の
    `4700cc8` コードだった（`/api/vllm/status` の `active` に `busy` / `api_listening` が無く
    `health` / `metrics` / `model_name` のみ = `8d7ad6c` 以前）→ **運用者が 2026-10-30 06:58:28 に
    `sudo systemctl restart dgx-spark-api` を実施**。実測: `ActiveState=active` / `SubState=running` /
    `Result=success` / `NRestarts=0`、MainPID の cwd は `/home/cliclie/DGXSparkUtil/api`
    （起動 06:58:28 > `api/vllm.py` の mtime 06:35:29 = rebase 後のコードを import）。
    `/api/vllm/status` の `active` に `busy:false` / `api_listening:true` が現れ（= `8d7ad6c` 反映）、
    strata 系の metrics キー（`generation_tokens_per_s` / `prompt_tokens_per_s` / `e2e_latency_s` /
    `ttft_s` / `kv_cache_usage_pct` / `requests_running` / `requests_waiting` /
    `expert_cache_hit_pct`）も返る（= `24c36bd` / `04930ca` 反映）。status 応答は初回 2.88 s →
    2 回目以降 0.017 s（キャッシュ有効）。`/`・`/api/platform`・`/api/metrics`・`/api/vllm/status`・
    `/api/vllm/cline`・`/api/thermal`・`/api/rag/status`・`/api/history` すべて 200、
    `journalctl -u dgx-spark-api --since 06:58 -p warning` 0 件。稼働モデル strataq3 は
    `health=true` / uptime 00:42:23 で継続（再起動の影響なし）。headless Chrome でも DISK
    「上限 7 GB/s」/ GPU クロック「最大 1117 MHz」/ vLLM 状態「稼働中」を確認
  **2026-10-30: 同じく本機で atom1 の GPU クロックサブ行変更（2 コミット）を反映した**
  （`git pull --ff-only origin main` で `1ccdc64` → `996cc1d` の 2 件を fast-forward、コンフリクトなし。
  内容は `3e1debc` の `gaugeDefs()` GPU クロック節（サブ行を `実 X / 最大 Y MHz` → `最大 <実測値> MHz`
  に簡略化、`front/index.html` 1 行）と `996cc1d` の README 更新（機能表・実装メモ・atom1 側の同期記録。
  本機の DISK 7 GB/s 化 2 コミットを atom1 側が rebase で取り込んだ旨を含む）。変更領域が GPU クロック節
  で本機の DISK 節（`diskMax`）とは非重複のため統合に問題なし。フロントは静的配信なのでブラウザ
  リロードで反映（`dgx-spark-api` は active のまま再起動不要。上記の `api/vllm.py` 未反映の注意は
  今回も有効）。実測: headless Chrome の `--dump-dom` で DISK「上限 7 GB/s」と GPU クロック
  「最大 1065 MHz」（= `gpu_clock_mhz` の実測値）と NW「上限 10 Gbps」が同一ページに共存することを確認。
  **atom2 は今回の 2 コミット分が未同期の可能性あり**（atom2 側で `git fetch --all --prune` +
  `git status -sb` を確認し `git merge --ff-only origin/main` で取り込み）
  **2026-10-30: 同じく本機で GPU クロック ゲージのサブ行を「最大 <HW 上限> MHz」に戻した**
  （`front/index.html` のみ・静的配信なのでブラウザリロードで反映、API 再起動不要。atom1 が同日に
  変更した「`最大 <実測値> MHz`」を運用者依頼で `gpu_clock_max_mhz`（= `nvidia-smi -q -d CLOCK` の
  `Max Clocks → SM`、本機 3090 MHz）へ戻す。**表示は全機共通**なので atom1 / atom2 も
  `git pull --ff-only origin main` 後は HW 上限表示になる（半円中央値は実測のまま、発動中は
  `実 X / ロック Y MHz` で実値が見える）。実測: `/api/metrics` の `gpu_clock_max_mhz=3090.0` は
  `Max Clocks SM 3090 MHz` と一致、headless Chrome のサブ行は `最大 3090 MHz` / 中央値は
  `1245 MHz`（実測）。詳細は 2026-10-30 節「GPU クロック ゲージのサブ行を…」）
  - 併せて本機は **サーマルガードを監視専らで導入済み**（`api/install_service.env` を
    `DGXUTIL_THERMAL=1` にして 07:14 に `sudo bash api/install_service.sh` を実行。
    `/api/thermal` は `{"enabled":true,...,"locked_mhz":0,"model_running":0}` を返し、
    フロントのサーマル表示は灰色のまま = 他機と状態が揃った。`${THERMAL_CONTAINERS-既定}` への
    修正（`7fd9da8`）は **2026-10-30 07:25:22 に `sudo systemctl restart thermal-guard` を実施**。
    起動ログが `... interval=2s containers=`（**末尾が空**）になり、発動対象が無いことを確認。
    `/api/thermal` は `{"enabled":true,...,"locked_mhz":0,"model_running":0}` を返し、
    `journalctl` の発動ログ（`thermal-guard: <温度>C -> <MHz>MHz`）は 0 件のまま =
    `nvidia-smi -lgc` を一度も叩いていない
    - 注意: restart 直後の 2 秒未満は `RuntimeDirectory=thermal` が削除された後で初回ループも
      まだなので `/api/thermal` は `{"enabled":false}` を返す（運用者が 07:25:22 に観測した値は
      これ。数秒後に `enabled:true` へ復帰することを実測で確認）

## 統合時系列グラフの GPU クロック/電力が 100% を超える不具合の修正（2026-10-24）

WSL2 (RTX 5090) で二段目の統合時系列グラフの GPU クロック系列が 100% を超えるという報告の調査・修正。

**原因**: `buildMainChart()` の `CHART_NORM` が `discrete ? 2350 : 3003`（MHz）・`discrete ? 300 : 140`（W）の
**構築時固定値**で、R9700（atom2）前提の値だった。RTX 5090 も `memory_mode=discrete` のため
3090 MHz が 3090/2350 = 131.5%、電力も 600 W 上限に対し最大 200% となった。
半円ゲージ側は `tuneGauge()` が `gpu_clock_max_mhz` / `gpu_power_cap_w` で上限を実測補正するため
影響が出ていなかった。GPU 実クロックが 3090 MHz（`nvidia-smi` の `clocks.max.sm` = ハード上限）を
超えることはなく、表示側の換算バグ。

**修正**（`front/index.html` のみ・再起動不要）: `refreshMainChart()` 冒頭で `lastMetrics` の
`gpu_clock_max_mhz` / `gpu_power_cap_w` があれば `CHART_NORM.gpuClock` / `gpuPower` を更新した。
換算は毎回の `toPct()` で行われるため過去点も新基準で再スケールされる。
unified（GB10）は両キーを返さないため従来どおりの固定基準のまま。
atom2 はゲージと同じく実測上限（2350 MHz / 現在の `power1_cap`）に追随する挙動になる
（thermal_guard の電力制限発動中はグラフ基準も 250/210 W に連動して変わる）。

**検証**（Whitebear2026 実機 2026-10-24）: `nvidia-smi -q -d CLOCK` の Max Clocks = 3090 MHz を確認。
一時起動した API（ポート 8091）の `/api/metrics` が `gpu_clock_max_mhz=3090.0` / `gpu_power_cap_w=600.0`
を返すことを確認。換算ロジック再現で 3090 MHz → ちょうど 100%、600 W → ちょうど 100% を確認
（修正前はそれぞれ 131.5% / 200%）。

## レイアウト圧縮: ヘッダ行タブ統合・ヘッダのモデル選択・ゲージ/時系列の 80% 縮小（2026-10-26）

要件: (1) システム/モデルのタブをヘッダ行へ移動 (2) モデルタブの横にモデル選択プルダウンと切替ボタン
（RAG もリスト上は一番下。ロード済のモデルを選択中は停止ボタン） (3) 一段目の半円グラフを縦横・文字
80% 縮小（一番小さい文字はそのまま） (4) 二段目の線グラフを高さ 80% 縮小し、初期起動時の範囲を直近30分
の既定値に変更。

**ヘッダ行のタブ統合**（`front/index.html`）:

- `<div class="tabs">` を `<header>` 内へ移す。header は `align-items: stretch` + `padding: 0 20px` +
  `min-height: 40px` にし、`.tabs` は `align-self: stretch`、`.tabs button` は `height: 100%` にして
  アクティブ下線が header の border-bottom に接するようにした（実測 gap 1px = border 分）
- 従来 header 50px + tabs 43px = 93px だったヘッダ領域が **40px** になり、ページ全体は
  `document.body.scrollHeight` **837px → 664px**（1600×1000 viewport 実測）
- タブ切替は `showTab(key)` に集約（ヘッダ行のタブボタンのみ呼ぶ。ヘッダの切替ボタンはタブを遷移させない）

**ヘッダ行のモデル選択プルダウンと切替ボタン**:

- `#hdr-model-select` は `hdrModelOptions()` が LLM 一覧 + **RAG を最下部**（`__rag__`）で生成する。
  `PLAT.rag_enabled === false`（WSL）では RAG を出さない。`#hdr-switch-btn` と共に `.hdr-model`
  （`margin-left: 14px`）でモデルタブの直横に置く
- 選択値は既存の `selectedProfile` を共有するため、パネル常設一覧のクリック選択と双方向に同期する。
  毎ポーリングの `renderModelList()` が `renderHdrModel()` を呼び、選択肢ラベルに ` (稼働中)` /
  ` (重み未導入)` / RAG の ` (稼働中)` を付与する
- `renderHdrSwitchBtn()` の状態: 未選択=「切替」disabled / 稼働中モデル=「停止」(`.stop` 赤) →
  `openStopModal()` / 停止中モデル=「切替」→ `runSwitch()` / 重み未導入=「切替」disabled /
  RAG 稼働中=「停止」→ `openRagModal("stop")` / RAG 停止中=「起動」→ `openRagModal("start")` /
  ジョブ実行中=disabled
- `panel-switch-go` の onclick は `runSwitch()` に、`btn-stop` の onclick は `openStopModal()` に抽出して
  ヘッダのボタンと共用した。`runSwitch()` は即座に両ボタンを disabled にして連打を塞ぐ
- RAG を選んだときパネルの「切り替え実行」は対象外なので `isRag` で disabled にしラベルも固定
- RAG の稼働状態は新設の `ragStatus`（`poll()` が `/api/rag/status` を代入）で見る

**一段目ゲージ（半円グラフ）の 80% 縮小**:

- CSS: `.gauge` `flex: 0 0 176px` → **140px**（10枚/行）、`padding: 10px 12px 8px` → **8px 10px 6px**、
  `.gcanvas` **76px → 60px**、`.gauge.dual .gcanvas` **56px → 44px**、`.gtitle` **12px → 10px**、
  `.gval` **16px → 13px**、`.gtitle .gdot` 12px → 10px、`.gsub` margin-top 4px → 3px
- **11px の最小文字**（`.gval .unit` / `.gval-dual-out` / `.gval-sub` / `.gsub`）は指示どおり据え置き
- JS: `GW / GH_SINGLE / GH_DUAL` **150/76/56 → 118/60/44**（カード 140px − padding 10×2 − border 1×2
  = 118px 幅、高さ `.gcanvas` と同じ）。`GAUGE_ROUT` **{70,50} → {54,38}**（`layout.padding` top4+bottom2 を
  引いた高さ制約上の実効外径。canvas 幅 118 の半幅 59 は常に高さ制約より大きい）。
  リング厚み `W_ZONE / W_VAL_SINGLE / W_VAL_OUTER / W_VAL_INNER` **3/11/8/6 → 2/9/6/5**。
  `gaugeCutout()` は `GAUGE_ROUT` と weight 合計から穴比率を出すので半径変更に自動追従する
- 実測: 単一 canvas 118×60 / dual 118×44、gtitle 10px・gval 13px・gsub/unit/sub 11px、canvas の
  カード内はみ出しなし。カード幅 176 → 140px（79.5%）。カード高さは 137 → 128px（93%）で、これは
  11px 文字の行（dual の 2 行値 28.6px と `.gsub` min-height 14px、幅縮小で増えたサブ行折り返し）を
  そのまま残していたためで、ゲージ描画自体の 80% 縮小とは別枠
  （**dual の 44px とサブ行折り返しは、同日後半の「ヘッダ操作とゲージ表示の改訂」で解消した**）

**二段目の 80% 縮小と既定範囲の変更**:

- `.chart-card .cwrap` **288px → 232px**
- `rangeKey` の初期値 **"all" → "30m"**、HTML 側 `#range-btns` の `class="active"` を 30m へ移動。
  サーバー側 `/api/history`（5秒間隔・30分）のバックフィルと同一範囲で初期表示される
- 見出しを 1 行に集約: セクション先頭の `<h2>時系列 (全項目・2秒間隔)</h2>` を `.charts-head` の中へ移し、
  副題 `<h3>CPU / メモリ / GPU / ストレージ (全項目 100% 換算)</h3>` を削除した（左端に見出し、右端に範囲ボタン）。
  `.charts-head` は `justify-content: space-between` なので h2 を最初の子にすると自動的に左へ寄る。
  `section > h2` でなくなるため、青い左バー + 14px グレーの見た目は `.charts-head h2` へ移設
  （他の 3 セクションの `section > h2` は従来どおり）。見出し行は h2 30px + h3 行 27px + gap → **27px の 1 行**になり、
  `.chart-card` の top は 406 → 376px、`body.scrollHeight` は 664 → 634px

**検証**（DGX Spark 実機 2026-10-26、headless Chrome + CDP。検証スクリプトはセッション用の一時ファイル
`/tmp/cdp_verify.py`（新版の計測）と `/tmp/cdp_old.py`（HEAD 版を `git archive` して高さ比較）で、
リポジトリには含めない）:

- ヘッダ: headerH 40px / `.tabs` は header 内（`tabsInHeader=true`・body 直下に tabs なし）/
  下線と header 下端の gap 1px / `#hdr-model-select`・`#hdr-switch-btn` は header の枠内
  （`selInsideHeaderBox=true`）
- プルダウン: 18 モード + RAG が最後（`lastIsRag=true`）、ラベルに ` (稼働中)` / ` (重み未導入)` が付く
- ボタン状態: 未選択=切替 disabled / 稼働中=停止(cls=stop) / 停止中=切替 / 重み未導入=切替 disabled /
  RAG 稼働中=停止 / RAG 停止中=起動 / ジョブ実行中=disabled。パネル側「切り替え実行」は RAG 選択時に
  disabled + ラベル「切り替え実行」
- ゲージ: canvas 118×60（単一）/ 118×44（dual。dual は同日後半の改訂で 60px に統一）、カード幅
  140px、文字 10 / 13 / 11px、値テキストとサブ行は canvas 内（単一 canvas 60px に対し値 21〜40px）
- 線グラフ: cwrap 232px、`rangeActive=30m`・`rangeKey=30m`、横軸はバック履歴の全点（30分未満）。
  見出し行は 27px の 1 行で `h3Exists=false`・`sameRow=true`、見出しは左端・範囲ボタンは右端に接（gap 0）、
  `section > h2` は残り 3 セクション（現在値 / vLLM・モデル / RAG）
- `Runtime.exceptionThrown` / `consoleAPICalled` のエラー 0 件、横スクロールバーなし（`overflowX=false`）
- 実機のモデル切替・停止・RAG 起動/停止 API は**発火させていない**（稼働中の qwen38bf16 への影響を避ける。
  ボタン状態は `currentStatus` / `ragStatus` を注入して描画ロジックのみ検証）

## ヘッダ操作とゲージ表示の改訂: タブ維持・自動追随・dual メーター統一（2026-10-26）

前項（レイアウト圧縮）を受けた運用面の改善。すべて `front/index.html` のみで API 再起動は不要。
検証はいずれも Whitebear2026 の headless Chrome + CDP（`Runtime.evaluate` と `Page.captureScreenshot`）。

**ヘッダの切替ボタン: 表示中のタブを維持する**:

- 実装時は進捗ログが出るモデルタブへ `showTab("model")` してから `runSwitch()` を呼んでいたが、
  閲覧中のタブが切り替わるとの指摘で廃止した。ヘッダからの切替は `runSwitch()` を直接呼ぶ。
  タブ切替は `showTab(key)`（ヘッダ行のタブボタンのみ）に集約したまま
- 進行中は `renderHdrSwitchBtn()` がボタンラベルを `HDR_JOB_LABELS`（switch=切替中 / stop=停止中 /
  recreate=再作成中、未知の kind は 実行中）へ差し替えて状態を示す。`runSwitch()` は押した瞬間に
  「切替中」へ変え（poll 待ちの 2 秒の空白をなくす）、以降は毎ポーリングの `renderHdrSwitchBtn()` が
  維持する。進捗ログは従来どおりモデルタブの `#panel-switch-log` に出る（見たければモデルタブを開く）

**ヘッダ切替ボタンの幅は「最大 4 文字 + 余白」**:

- `#hdr-switch-btn` は `min-width: calc(4em + 18px)` + `white-space: nowrap` + `text-align: center`。
  `4em` = 最長ラベル 4 文字（日本語 1 字 = 1em、`font-size: 12px` → 48px）、`+18px` =
  `padding: 4px 8px`（`#hdr-model-select` と揃えた）+ `border 1px × 2`。**em 指定なので環境のフォントに
  追従する**（headless のフォールバックは 1 字 7.2px、実機の日本語フォントは 1 字 12px）
- 変遷: `min-width: 84px`（実装直後。通常ラベル「切替」の実測 32.4px に対し過剰）→
  `padding 4px 8px` + `60px` → ラベルから `…` を外して `calc(4em + 18px)`（headless 実測 66px）
- **`nowrap` がないと 58〜60px で 3 文字目以降が 2 行へ折り返して `h=38px` になった**（高さ 14px 増）。
  実測は全ラベル（切替/停止/起動/切替中/停止中/再作成中/実行中）で `w=66px h=24px`、右端 745px 固定、
  header は 40px のまま

**プルダウンの稼働中モデル自動追随と 10 秒猶予**:

- `selectedProfile` は従来ページ開時 `null`（プレースホルダ「モデルを選択」）で、ターミナル側で
  切替わってもプルダウンは動かなかった。新設 `syncHdrModelSelection()` を毎ポーリングの
  `renderModelList()` **先頭**で呼び、`Date.now() >= hdrManualUntil` のときだけ `selectedProfile` を
  `currentStatus.active.profile`（無ければ `null`）へ揃える。先頭に置いたのは、一覧の `.selected` と
  `sel.value` を同一ポーリングで揃えるため（`renderHdrModel()` は `renderModelList()` の末尾から呼ばれる）
- ユーザー操作側（`#hdr-model-select` の onchange / `.model-item` の onclick / `runSwitch()`）は
  `markHdrManual()` で `hdrManualUntil = Date.now() + HDR_MANUAL_MS`（10 秒）を更新して追随を止める。
  過ぎると（実効は次のポーリング、最大 2 秒後）稼働中モデルへ戻る
- 選択値はモデルタブ常設一覧と共有しているため、一覧側の選択も同じ条件で戻る
- RAG を選んだ場合も猶予が切れると稼働中 LLM へ戻る（本機は `rag_enabled=false` で RAG 選択肢が無く、
  RAG 環境での挙動は未実測）

**ゲージのサブ行折り返しを解消して行高さを 108px に統一**:

- `g-gpu-power.sub` は従来 `上限 600 W (GPU power limit)` のように末尾へ種別
  （`(GB10 TDP)` / `(GPU power limit)` / `(R9700 PPT)`）を付いていたが、カード幅 140px では 2 行へ
  折り返って行高さを押上げるため `上限 600 W` のみにした（`.gsub` 26 → 14px）
- `g-disk-used.sub` の `232 GiB / 1007 GiB total`（24ch）も折り返していたため単位を末尾 1 回に統合して
  `232 / 1007 GiB`（総量 1024 GiB 超は `1.20 / 1.82 TiB`）へ。専用関数 `fmtGiB()` は参照が
  なくなったため削除した
- 3 行目を押上げていた実際の要因は「ストレージ使用率」ではなく **dual メーター**だった
  （→ 次項で単一レイアウトへ統合）。`.gauge.vinfo-card` は natural 89px で、`.grid` の stretch に
  引き伸ばされているだけだった
- 実測: 行高さ 120 / 120 / 122.6 → **108 / 108 / 108**、`#host-cards` 370.6 → **344px**、
  `body.scrollHeight` 755 → **728px**

**dual メーター（DISK / NW）を単一レイアウトへ統合**:

- `GH_DUAL`（44）を廃止して `GH_SINGLE` 60px に統一（`GW / GH_SINGLE` = 118/60、
  `GAUGE_ROUT` は `{single:54, dual:38}` → **54 共通**）。`gaugeCutout(dual)` は weight 合計だけで
  穴比率を出す（単一 80% / dual **76%** → 穴の内径 82px）。CSS の `.gauge.dual .gcanvas` と
  `.gval-dual-out` 系（`.gauge .gval-dual-out` / `.gauge.dual .gval-dual-out` / 合同セレクタ）は削除
- `Gauge` コンストラクタは dual でも値要素を `.gcanvas` 内へ `appendChild` し、クラスは単一と同じ
  **`.gval`** に統一（`gval-dual` は不要になった）。`set()` の dual 分岐は 1 行目 = A（外側リング）、
  2 行目 = B（内側リング）を `<div class="gval-line">`（2 行目は `gval-line gval-sub`）で並べる
- **タイトルへ単位を移動**: `ストレージ負荷` → **`DISK(B/s)`**、`ネットワーク負荷` → **`NW(bps)`**。
  値は `読込 0.0 / 書込 0.0MB/s` のような幅を取る表記をやめ、**`R 0.0M` / `W 0.0M`**、
  **`UP 0.0M` / `DL 0.0M`**（ラベル 1〜2 文字 + 接頭語のみ）にした
- 新設 `pfxParts(v, base)` が値を **`{n: 数値, s: 接頭語}`** に分割して返す（`v >= base` → `G`、
  それ以外 → `M`。**1 未満も M のまま小数 1 桁**なのでアイドル時は `R 0.0M`、0.42 MiB/s は `R 0.4M`）。
  `base` は API の換算と同一基数で disk=1024（`/1024/1024`）、net=1000（`/1e6`）。dual の `def.unit`
  と `def.fmt` は不要になったため削除
- **NW は A/B を入換**えて上り（UP）を外側リング・1 行目にした（従来は下りが A）。ネットワーク系列は
  時系列グラフに無いため凡例色との対応は崩れない（DISK の緑=読込・黄=書込は `#56d364` / `#e3b341`
  のままグラフ系列と一致）
- **メーター内の書式も単一メーターに統一**: 数値は `.gnum`（`.gval` の 13px 白を継承）、接頭語は既存の
  `.unit`（11px `--muted`）、ラベル `.glabel` は 11px で **対応リングの色を inline style 指定**
  （`R`=`colorA` 緑 `#56d364` / `W`=`colorB` 黄 `#e3b341`、`UP`=青 `#58a6ff` / `DL`=紫 `#bc8cff`）。
  2 行目は RAM/VRAM の `subVal` と同じ `.gval-sub` に載せるため
  `.gauge .gval .gval-sub .gnum { font-size: 13px }` で 11px への上書きを戻す
- 実測: dual canvas **118×60**、外側 R 52/46・内側 46/41（穴 82px）で 2 行テキストと交差なし、
  カード高さは **3 行とも 108px**。1 行目の中心 = canvas 内 **30px**・2 行目 = **47px** で、
  RAM の値（29px）と `subVal`（46px）に一致。`.glabel` の computed color は `def.colorA` / `def.colorB` と
  **4/4 一致**、`.gnum` は `#e6edf3` 13px、`.unit` は `#8b949e` 11px。表示例 `R 0.0M` / `W 0.3M`、
  `UP 0.0M` / `DL 0.0M`（1 行のテキスト幅 23px）。最大値ストレス（`R 4.9G` / `W 4.9G`、
  `UP 10.0G` / `DL 10.0G`）でも 1 行を維持

**二段目グラフの凡例ラベル（2026-10-26 に 2 段階で短縮）**:

- `buildMainChart()` の `lineDS()` 第 1 引数のみを変更。まず全系列から末尾の ` %` を削除し、
  ストレージ系を `DISK使用率` / `DISK Read` / `DISK Write` へ変えたのち、**項目名 + 空白 + 単位**の
  短縮表記へ変更した（`○○負荷`・`○○使用率` → `○○ %`／`○○温度` → `○○ ℃`／`○○クロック` → `○○ MHz`／
  `○○電力` → `○○ W`）。現行 11 系列は `CPU % / CPU ℃ / RAM % / VRAM % / GPU % / GPU ℃ / GPU W /
  GPU MHz / DISK % / DISK Read / DISK Write`（GB10 は RAM/VRAM が `メモリ %` 1 系列になり 10 系列）。
  `DISK Read` / `DISK Write` は単位を持たないため据え置き
- **ゲージ側の `unit: "%"`・`°C` と 1 段目タイトル「ストレージ使用率」は無変更**（要望は凡例が対象）。
  凡例の ℃ は U+2103、ゲージ側の `°C` は U+00B0 + C のまま
- 実測: `chart.legend.legendItems[].text` が上記 11 ラベル、凡例は h=31px の 1 行。Y 軸は従来どおり
  0〜100% 換算、`#host-cards` は 108 / 108 / 108px、`body.scrollHeight` 728px で不変

**検証サマリ（Whitebear2026 / headless Chrome + CDP）**:

- ヘッダ切替: システムタブで「切替」を押しても `active tab=system` / `showTab()` 呼び出し 0 件 /
  `runSwitch()` は実行される（モデルタブでも `model` のまま）。`/api/vllm/switch` だけモックして
  走らせた本物の `runSwitch()` で猶予が 10 秒へリセットされ、ラベルが「切替中」・両ボタン disabled を確認
- プルダウン: 読込時に稼働中 `swift40` が選択表示 / `strataq3` 選択後 6 秒経過でも維持（残り 4 秒）/
  猶予切れで `swift40` へ復帰 / `currentStatus.active` を差し替えると追随 / `active=null` では
  「モデルを選択」+ 切替ボタン disabled。一覧クリックの猶予は `ready=false`（重み未導入）品だと
  `notReady` で早期 return するため **`ready=true` の品で確認**する（`.model-item:not(.not-ready)`）
- 前項の機能（タブ統合・80% 縮小）への回帰: 全ラベルでボタン 66px、行高さ 108px、gauges 11 枚、
  `Runtime.exceptionThrown` / `consoleAPICalled` のエラー 0 件
- 検証スクリプトはセッション用の一時ファイル（`/tmp/verify_tab.py` / `/tmp/vh*.py` / `/tmp/dual*.py` /
  `/tmp/btn*.py`）でリポジトリには含めない。実機のモデル切替・停止 API は発火させていない

- **タイトル行から「 Monitor」を削除**（同日の追加指示）: `h1#hdr-title` の初期値を
  「DGX Spark Monitor」→「DGX Spark」、`applyPlatform()` の
  `h1.textContent = name + " Monitor"` → `h1.textContent = name`（`name` は dgx-spark が
  `PLAT.label`、atom2 / wsl は `PLAT.hostname`）。**ブラウザタブの `<title>` と
  `document.title` の「… Monitor」は対象外として残した**
  - 実測（atom1 = WhitebearATOM1、headless Chrome + CDP `/tmp/cdp_title_check.py`）:
    `PLAT.platform="dgx-spark"` / `label="DGX Spark"` の環境で `h1="DGX Spark"`（`Monitor` 無し）、
    `document.title="DGX Spark Monitor"`（意図どおり未変更）
  - `--dump-dom --virtual-time-budget` は fetch 待ちの JS 初期化が完了せず静的値しか返さなかった。
    CDP で `Runtime.evaluate` を 5 秒後に実行して実描画を確認する
- **タイトル行の日時計（`yyyy/mm/dd hh:mm:ss`）を削除**（同日の追加指示）: `header` の
  `<span id="clock">`、CSS `header #clock`、1 秒間隔の `setInterval`（`toLocaleString("ja-JP",
  {hour12:false})` を流し込む block）を削除。要素を消すと `getElementById("clock")` が
  1 秒後に `null` 参照で落ちるので **setInterval も同時に消すのが必須**。
  後続コメント「P-State(タイトルの現在日時の右)」→「P-State(タイトル右)」に修正
  - 実測（atom1、headless Chrome + CDP `/tmp/cdp_clock_check.py`）: `#clock` は `null`、
    ヘッダ text は `DGX Spark | P0 | システム | モデル | …`（日時なし）、
    時系列 360 → 362 点に増加（ポーリング生存）、gauges 11 枚、
    `Runtime.exceptionThrown` / `consoleAPICalled(error|warning)` / `Log.entryAdded(error|warning)` **0 件**

## server.log の日次アーカイブ（JST 0 時・zip）（2026-10-26）

> **同日に truncate へ切り替えた**: ログの中身を精査した結果、アーカイブは廃止し
> **JST 0 時に 0 バイトへ truncate するだけ**にした。現行仕様は
> 「server.log の日次 truncate（JST 0 時）」と「server.log はアーカイブせず JST 0 時に
> truncate（2026-10-26）」を参照。以下は zip 版の実装記録（命名方式の検証は残る）。

**要望**: `api/server.log` は当日分だけを保ち、日をまたいだら前日分を
`api/server.log.YYYYMMDD.zip` に改名＋zip 圧縮する。日付またぎの判定は JST。

**追加ファイル**

- `api/rotate_log.sh` — コピー → truncate → zip。`API_DIR` は `BASH_SOURCE` から導出する
  （`/tmp` へコピーして完結する単体テストができる）。`LOG_TZ`（既定 `Asia/Tokyo`）で上書き可
- `api/dgx-spark-logrotate.service` — `Type=oneshot` / `User=__USER__`。100MB 級のコピー＋zip が
  prefill 中の vLLM と競合しないよう `Nice=10` + `IOSchedulingClass=idle`
- `api/dgx-spark-logrotate.timer` — `OnCalendar=__JST_MIDNIGHT_LOCAL__` + `Persistent=true`
- `install_service.sh` — 上記 unit をレンダリングして `enable --now` し、導入時に一度
  `systemctl start dgx-spark-logrotate.service` して溜まったログを当日分だけに整える
- `.gitignore` — `api/server.log.*.zip` / `api/.rotate_log.lock` / `api/.rotate_log.*`
- `api/install_logrotate_user.sh` — **sudo 不要版**。`~/.config/systemd/user/` に unit を書き、
  `loginctl enable-linger` を有効化してタイマーをブート後も生かす（`server.log` は
  `cliclie` 所有なのでコピー / truncate に root 特権は要らない）

**「改名」ではなく copytruncate にした理由**: 書き手が `StandardOutput=append:server.log`
（systemd unit）と `>> server.log`（`run.sh -d`）で fd を保持し続けるため、rename すると
書込みがアーカイブ側へ流れ続けて `server.log` は以後増えない。truncate は inode/fd を保つので
追記側を再起動せずに済む。logrotate の `copytruncate` と同じトレードオフで、
コピー開始〜truncate 間（数十 ms）の書込みを失う。

**JST 判定の 2 箇所**

1. アーカイブ名: `TZ=Asia/Tokyo` を export したうえで
   `date -d "@$(stat -c %Y server.log)" +%Y%m%d`（最終書込みの JST 日付）。
   ログ行にタイムスタンプがない（uvicorn 既定の `INFO:     IP:port - "GET …" 200 OK`）ため、
   行単位の時刻分割はできずファイル単位の分割 + 最終日命名になる
2. 発火時刻: `OnCalendar` はマシンのローカル TZ で評価される（unit の `TZ` 環境変数は
   `ExecStart` にしか効かない）。`install_service.sh` が `TZ=Asia/Tokyo date -d 'today 00:00:00' +%s`
   を取り、その epoch をローカル TZ で `+%H:%M:%S` して `*-*-* HH:MM:SS` を生成する

**検証サマリ（atom1 = WhitebearATOM1 実機・`TZ=Asia/Tokyo`。サンドボックス `/tmp/rot_test*` と実サービスの両方）**

- 前日 mtime のログ → `server.log.20261025.zip`（zip 内エントリ名は `server.log`）、
  `server.log` は 0 バイト、展開内容は原文と一致
- 昼間の手動実行（当日 mtime）→ `server.log.20261026.zip`。同日分の再実行 → `…2.zip` へ連番
- JST 境界: mtime `2026-10-25 23:59:59 +0900` → `20261025`、`2026-10-26 00:00:01 +0900` → `20261026`
- 空ログ・ログなしは zip を作らない。作業 tmpdir の残骸 0（`.rotate_log.lock` は flock の対象なので残る）
- **fd 生存**: `exec 3>>server.log` で 0.05 秒間隔に追記し続ける書き手の途中でローテート →
  zip に `writer 1..8`、`server.log` に `writer 9..37`（同じ fd で追記が継続、欠番なし）
- `flock`: 同時起動した 2 本目は rc=0 で何もしない、zip は 1 本
- **実サービスに対する初回ローテート**（`api/server.log` 196,292,515 B = 187 MB）→
  `server.log.20261026.zip` **1,264,094 B（1.2 MB、99.4% 圧縮）**。zip 内は先頭
  `INFO:     Started server process [295023]` から 2,904,909 行。`server.log` は 0 バイトになり、
  以後も稼働中の uvicorn（pid 2191）が同じ fd で追記を継続（+404 B/2 秒で単調増加、
  `/proc/2191/fdinfo/1` の flags に `O_APPEND`、`NRestarts=0` = API 無停止、`GET /` は 200）
- **user timer 実導入**（`install_logrotate_user.sh`）: `systemctl --user list-timers` の次回発火が
  `Tue 2026-10-27 00:00:00 JST`。`systemctl --user start` の oneshot は `status=0/SUCCESS` で
  journal に `rotated 27388 bytes -> server.log.20261026.2.zip (440 bytes)`
  （連番命名が実運用で機能）。`systemd-analyze --user verify` は無警告
- `bash -n` 通過。`systemd-analyze verify`（レンダリング後 unit）は無警告
  （`rotate_log.sh` に実行権が必要 → repo 側は 755、`install_service.sh` も chmod 755 する）
- レンダリング実測: プレースホルダ残 0、`OnCalendar=*-*-* 00:00:00`（JST 機）。
  同じ換算式は TZ=UTC 機で `15:00:00` を返すことを確認
- **本機 (atom1) は user timer で稼働中**: `install_logrotate_user.sh` を実適用済み
  （`loginctl enable-linger cliclie` はパスワードなしで通った）。`sudo bash api/install_service.sh`
  の system unit 版はパスワード必須のため未実行（次の保守時に）。他機 (atom2 / DGX Spark / WSL) は
  `git pull` 後に `bash api/install_logrotate_user.sh`（sudo 不要）で有効化できる

## server.log はアーカイブせず JST 0 時に truncate（2026-10-26）

**要望**: `server.log.YYYYMMDD.zip` の `YYYYMMDD` を「その zip に含まれる行の JST
0:00〜23:59」の意味にしたい（前項の mtime 命名は 10/23〜26 が混在していた）。

**調査で「既存アーカイブの日付分割は不可能」と判明**（対象 = 187 MB・2,904,909 行・
実カバレッジ 2026-10-23 03:37〜10-26 09:50）

- ログ行に日付が無い。uvicorn 既定のアクセスログ書式で
  `INFO:     192.168.0.26:64020 - "GET /api/metrics HTTP/1.1" 200 OK` の 1 行のみ
- journald にアクセスログ行は入っていない。`StandardOutput=append:` は journal へ
  コピーしないため `journalctl -u dgx-spark-api` は 56 行だけ（`GET /api/metrics` は 0 件）
- journal に残る unit 起動は 10/23〜10/26 で **9 回**（23 日 2 / 24 日 3 / 25 日 2 / 26 日 2）。
  一方ログ内の `Started server process [pid]` は **136 個**で、差分の 127 個は
  `run.sh -d` (nohup) 起動 = 時刻が復元できない。→ 判明する時刻境界は 290 万行中 9 点

**結論**: 中身に保存価値が無いので **zip を廃止し、JST 0 時に 0 バイトへ truncate するだけ**にした。

**変更内容**

- `api/rotate_log.sh`: `cp` / `zip` / 連番命名を削除。`flock` → 空・不在チェック →
  `: > server.log` → journal に 1 行。`LOG_TZ`（既定 `Asia/Tokyo`）は出力時刻の
  JST 表示にのみ使う（リネームしない設計理由は前項と同じ: 書き手の fd は inode に紐づく）
- `api/dgx-spark-logrotate.service`: `Description` を truncate に。zip で必要だった
  `Nice=10` / `IOSchedulingClass=idle` は削除（truncate は IO を食わない）
- `api/dgx-spark-logrotate.timer`: 変更なし（`OnCalendar` = JST 0 時、`Persistent=true`）。
  停止中に逃すと「溜まった分をまとめて捨てる」挙動になる
- `install_service.sh` / `install_logrotate_user.sh`: コメントと進捗メッセージのみ
- `.gitignore` の `api/server.log.*.zip` は**据え置き**（zip 版が残した
  `server.log.20261026.zip` / `.2.zip` が untracked に出ないようにする）。
  中身に価値は無いので不要なら `rm api/server.log.*.zip` で消してよい

**検証（atom1 = WhitebearATOM1 実機・`TZ=Asia/Tokyo`）**

- サンドボックス `/tmp/rot_test_run.sh` **PASS=17 / FAIL=0**: 0 バイト化／zip を作らない／
  出力は 1 行で `truncated server.log (34 bytes)`・時刻は `+09:00`／空ログ・ログ不在は
  無動作・無出力／`flock` 保持中は無動作でサイズも不変、解除後は実行／
  追記し続ける fd は truncate 後も 29 行が新しい `server.log` へ継続／inode は truncate で不変
- 実サービス: `systemctl --user start dgx-spark-logrotate.service` → `status=0/SUCCESS`、
  journal に `2026-10-26T10:09:32+09:00 truncated server.log (207371 bytes)`。
  truncate 後も 6 秒で 2,424 → 3,688 B に成長（同じ fd で追記継続）、`GET /` = 200、
  `NRestarts=0`（API 無停止）
- `systemd-analyze verify`（system 版 / user 版）は無警告。`bash -n` は 3 スクリプトとも通過
  - 教訓 1: `editor` でスクリプトを**作り直すと実行権が落ちる**。
    `systemd-analyze verify` が `Command ... is not executable` で検出できた（`chmod 755` で解消）
  - 教訓 2: truncate は速すぎるため「同じスクリプトを 2 本並列」では `flock` を
    検証できない（1 本目が完了してロックが解ける）。`( exec 9>lock; flock 9; sleep 2 ) &` で
    **明示的にロックを保持**して検証する

## vLLM 情報カードの状態表示の短縮（2026-10-26）

**要望**: 一段目の半円グラフの末尾の vLLM 情報カードで「稼働中 (API応答)」「稼働中 (API応答なし)」を
それぞれ「稼働中」「API応答なし」へ短縮する。

**変更内容**

- `front/index.html` `renderVllm()`: `setV("v-status", ...)` の
  「稼働中 (API応答)」→「稼働中」、「稼働中 (API応答なし)」→「API応答なし」。
  色クラス（`up` / `warn`）は不変
- `setV()` は `V_MIRROR` で `#v-status`（モデルタブの vLLM 行）と `#vi-status`（情報カード）の
  両方にミラーするため、**両方の表示が短縮表記になる**
- 四段目 RAG セクション（Embedding / Qdrant）の「稼働中 (API応答)」「起動中… (API応答なし)」は
  別セクションのため据え置き

## ヘッダのモデル選択プルダウンで重み未導入を灰色表示（2026-10-27）

**要望**: ヘッダのモデル切り替えプルダウンで、展開時の一覧にて未ロード（重みが無い）項目の文字色を
灰色にして、ロード済みの重みと色で識別できるようにしたい。

**検討**:

- モデルタブ常設の一覧（`.model-item`）は既に `ready === false` を `.not-ready` クラスで
  `--muted`（灰色）にしている。ヘッダのプルダウンも同じ判定・同じ色で揃えるのが最小の変更
- ネイティブ `<select>` の `<option>` は `color` が効く（Chrome / Edge / Firefox の
  Windows・Linux）。`disabled` にすると灰色になるが**選択自体ができなくなる**ため不採用
  （現状、未導入でも選択はでき、切替ボタンが disabled になる挙動を維持したい）
- 完全カスタム（div ベースのドロップダウン）にすれば macOS の OS ネイティブピッカーでも色を
  制御できるが、自動追随（`syncHdrModelSelection()`）や 2 秒ポーリングごとの再描画の移植が必要で
  過剰。ネイティブ option の CSS で実装し、macOS ではラベル末尾の「 (重み未導入)」表記が
  補助になるままとした

**変更内容**（`front/index.html` のみ。API 再起動不要）:

- `hdrModelOptions()`: 各要素を `[value, label, notReady]` の 3 元組にし、`c.ready === false` を
  `notReady` として返す。RAG は「未起動 = 停止中」で重み未導入ではないため常に `false`
- `renderHdrModel()`: `notReady` の option だけ `o.className = "not-ready"` を付与
- CSS: `#hdr-model-select option.not-ready { color: var(--muted); }` を追加

**検証**（Whitebear2026-WSL = modelctl / `rag_enabled=false`、headless Chrome 156 + CDP
`/tmp/cdp_gray_check.py`。実機の切替・停止 API は発火させていない）:

- 選択肢 16 件（プレースホルダ + LLM 15。RAG は WSL のため無し）。`cls=not-ready` の 13 件は
  `rgb(139, 148, 158)`（=`--muted`）、導入済みの `swift40` / `strataq3` とプレースホルダは
  `rgb(230, 237, 243)`（=`--text`）で、クラスと色の不一致 0 件
- `Page.captureScreenshot` で展開時のドロップダウンを撮影し、未導入行が灰色・導入済み行が白で
  描画されることを確認（ホバー中の先頭行は OS の選択背景で白になる）
- 副作用なし: 未導入を選択しても**閉じた状態の `select` 本体は `--text` のまま**（option の色が
  表示部へ継承しない）／選択値は従来どおり `selectedProfile` へ入り、`#hdr-switch-btn` は
  `disabled` のまま（従来挙動を維持）
- 回帰なし: パネル常設一覧の `.model-item.not-ready .name` は `--muted` のまま。`PLAT.rag_enabled = true` と
  `currentStatus.active = {profile: 'swift40'}` を注入した状態でも RAG 選択肢と稼働中モデルは `--text`
  （選択肢 17 件）。`#hdr-switch-btn` は 66×24px、header は 40px、`Runtime.exceptionThrown` と
  error コンソール 0 件
- 検証スクリプトはセッション用の一時ファイル（`/tmp/cdp_gray_check.py` / `/tmp/cdp_gray_regress.py` /
  `/tmp/cdp_gray_shot.py`）でリポジトリには含めない

## Strata (strata・atom2) の Cline 設定値と prefill/decode 表示（2026-10-26 / 10-28）

**経緯**: 2026-10-26 に atom2 へ Strata エンジン（Qwen3.8-Flash-Next IQ3_XXS、
compose プロファイル `strata`、ホストポート 8009）を追加した。profiles 対応表は
`_load_profiles()` の compose 動的パースで自動追従するが、Cline 設定値の API key と
メトリクスは Strata 固有の認証・形式に対応する必要があった（コミット ef845fa /
24c36bd / 04930ca）。2026-10-28 の 0.2.7（engine 0.1.52）更新後も JSON 構造は
フィールド互換を維持しておりコード変更不要だった。

**Strata の API 仕様（本機実測）**:

| エンドポイント | 認証 | 形式 |
|---|---|---|
| `GET /health` | 不要 | JSON（`max_context` / `images` / `loaded` / `model`） |
| `GET /v1/models`・`/v1/*` | **Bearer 必須** | OpenAI / Anthropic 互換 |
| `GET /metrics`・`/slots` | **Bearer 必須** | **JSON**（Prometheus ではない。`engine` / `live` / `requests` の 3 ブロック） |

key は `~/LLM/compose/.env` の `STRATA_API_KEY`（`config.py` が既存の
`_read_compose_env()` で読む。サービス起動時のみ読込のため .env 変更時は
`systemctl restart dgx-spark-api` が必要）。

**実装**（`api/vllm.py` のみ。フロント無変更 = 既存の行別コピー/一括コピーが有効化される）:

- `get_cline_config()`: strata 分岐で `config.STRATA_API_KEY` を返す（従来は
  「認証なし(任意の値でOK)」表示）。コンテキスト・画像対応は key 不要の `/health` から
  動的取得（`_strata_health_info()`）。compose の command ブロックが無い
  （環境変数設定型）ため `--max-model-len` 等の動的取得は使えない。
  `CLINE_MODEL_TABLE` にフォールバック値（131072 / images: true）
- `get_status()`: モデル名取得（`/v1/models`）を strata のみ Bearer 付きに。
  メトリクスは Prometheus チェーンに入れない early-return 分岐で
  `_strata_server_metrics()` がマッピング:
  - decode = `live.tok_s`（生成中の瞬時値）→ 直近**完了**リクエストの `decode_tok_s`
  - prefill = `live.prefill_tok_s_mean` → `prompt_tokens ÷ (prompt_ms/1000)`
  - E2E = `duration_s` / TTFT = `first_engine_token_s` / KV = `(prompt+output)÷context×100`
  - `finish=cancel`（クライアント切断）のリクエストは値が null のため
    `stop`/`length`（完了）から代表を選ぶ
  - idle / auto-unload（`live.state=unloaded`）では running=0 に正規化し
    スループット・KV は 0（llama.cpp の idle と同じ体験）。直後 5 秒は
    `_strata_last_metrics` キャッシュで計測値を維持
- Strata は暫く idle でモデルを auto-unload し、次回リクエストで自動再ロードする
  （magn の on-demand と同様の運用。`/health` の `loaded` で確認できる）

**検証**（実 HTTP・strata 0.2.3 / 0.2.7 の両方で確認）:

- `GET /api/vllm/cline` → `api_key=実キー48文字` / `model_id=qwen3.8-flash-next-iq3_xxs` /
  `context 131072` / `images true`
- `GET /api/vllm/status` → `model_name` 取得 OK。生成中は live 瞬時値
  （decode 60.7 / prefill 297.7 T/s 等が追従変動）、アイドル時は 0 表示
- 0.2.7（engine 0.1.52）で `/metrics` JSON の `live` / `requests` フィールド互換を
  全て確認（`all(k in d['live'] ...)=True`、`req0 keys ok=True`）
- 「重み未導入」灰色表示（2026-10-27・modelctl 専用）は `ready === false` の strict
  判定で、compose 側は `ready` 未定義（undefined）のため strata が誤って
  切替不可・灰色になる回帰なしを確認

## ダッシュボードのポーリング緩和と GPU クロックの実値表示 (2026-10-28)

### 症状 (Ornith 122B の長文 prefill 中)

`~/llm` の Ornith 122B (TabbyAPI / ExLlamaV3) で 260K prefill を流すと、モデルカードが
「API応答なし」になり、ゲージもサーマル表示も 20 秒近く固まった。prefill 中の実測:

| 項目 | 修正前 |
|---|---|
| `GET /api/vllm/status` | **20.46 秒** |
| `active.health` | false |
| `active.model_name` / `active.metrics` | null / null |

原因は **モデルの HTTP が塞がれたときのプロブの直列積み上げ**。1 リクエストで次を順に
叩いており、各 curl は max-time まで待つ (塞がれていると全部満額):

| 呼び出し | max-time |
|---|---|
| `/health` (health 判定) | 3 s |
| `/v1/models` (health フォールバック) | 3 s |
| `/v1/models` (モデル名) | 3 s |
| `/metrics` (エンジン種別 + メトリクス) | 5 s |
| `/.well-known/serviceinfo` (`_is_tabbyapi`) | 3 s |
| `/v1/models` (`_is_llamacpp`) | 3 s |
| `/v1/model` (`_tabbyapi_metrics` の cache_size) | 3 s |

ExLlamaV3 は推論中に uvicorn ワーカーが塞がるので、**プロセスが生きていても HTTP は
返らない**。「応答なし = 落ちた」ではない。

### API 側の緩和 (`api/vllm.py`)

1. **変わらないものをキャッシュ** (TTL 付き、失敗時は前値を返して表示を止めない)
   - エンジン種別 `_engine_kind`: 判定**成功**時 120 s。判定**失敗 (混雑) は 10 s だけ**
     — 失敗を 120 s すると TabbyAPI が永久にメトリクス無し表示になる (検証で発覚)。
     種別が既知なら `/metrics` が空でも serviceinfo / `/v1/models` の追加プロブ (計 6 s)
     を払わず stale で返す
   - モデル名 `_model_name`: 成功 30 s / 失敗 10 s
   - `docker logs` 末尾 `_logs_tail`: 5 s (2 秒ポーリングの重複を省く)
   - TabbyAPI `/v1/model` の `cache_size` / `max_batch_size` は静的 → `_tabby_model_txt` 120 s
   - **`ts` は判定完了時刻で書く**。開始時刻を入れると 8 秒かかった判定の TTL が実質
     2 秒になり毎コール再判定していた (cProfile で発覚、11 s/回)
2. **`busy` / `api_listening`** を追加: `/health` が 1.2 秒で返らないとき TCP 接続だけを
   試みる (`_tcp_open`)。listener が受け付ければ `busy=true` = 生きていて混雑中
3. プロブの max-time 短縮: `/health` 3→1.2 s、`/v1/models` 3→1.5 s、`/metrics` 5→2 s、
   `/v1/model` 3→1.5 s
4. `/get_load` 等の `None` を `{}` に正規化 (front のキー参照を揃える)

**効果 (prefill 継続中に `get_status()` を 5 回連続実行)**:

| コール | 修正前 | 修正後 |
|---|---|---|
| 1 回目 (キャッシュ寒結) | 20.5 s | 11.1 s |
| 2 回目以降 | 20.5 s | **1.5 / 3.1 / 1.6 / 1.5 s** |
| health / busy | false / 項目なし | false / **true** |

### フロント側の緩和 (`front/index.html`)

- `fetchJson(url, timeoutMs)` に **AbortController 期限** (既定 6 s)。期限がないと
  status が詰まったとき ゲージ・RAG・サーマルまで一緒に止まっていた
- `poll()` は 4 エンドポイントを **`Promise.allSettled` で並列**に引き、それぞれ期限を
  付ける (metrics 5 s / status 9 s / rag 5 s / thermal 3 s)。失敗したカードは前回の値を残す
- 状態表示: `health` → 「稼働中」/ `busy` → **「稼働中（推論中）」(hot)** /
  どちらも false → 「API応答なし」(warn)

### GPU クロックゲージは実値とロック段を表示

サブ行は従来 `最大 3003 MHz` だけ。3003 MHz は `clocks.max.sm` の HW 上限で、実 boost は
2400〜2515 MHz、サーマルガード発動中は 1651 MHz。**目盛りが 3003 固定なので 1651 MHz に
落とされても針が 55% 付近に張り付き、発動の判断がつきなかった** (運用者指摘)。

- サブ行を **`実 <現在値> / 最大 <clocks.max.sm> MHz`** に変更
- **発動中は目盛りをロック段に寄せる**: `tuneGauge()` が `thermalClockLockMhz()`
  (`/api/thermal` の `locked_mhz`。power モード = atom2 は `mode:"power"` で対象外) を
  返し `max = locked_mhz` → 発動中は `実 1651 / ロック 1665 MHz` で針はほぼ振り切る。
  解除時は `clocks.max.sm` (3003) へ戻る
- 二段目の時系列グラフの 100% 基準 (`CHART_NORM.gpuClock`) は HW 上限のまま。発動中は
  線が 55% 付近へ下がるので発動の目視確認に使える

検証: 発動中の `/api/thermal` は `locked_mhz:1665 / clock_mhz:1651` (clock モード、
`mode` キーなし) → `thermalClockLockMhz()` は 1665 を返す。atom2 の power モード状態は
`mode:"power"` かつ `locked_mhz` なし → null。フロント構文は括弧バランスが HEAD 版と
同じ 0 になることを確認 (node 未導入のため構文パーサなし)。`py_compile` 通過。

反映には `sudo systemctl restart dgx-spark-api` が必要 (front は静的ファイルなので
ブラウザ再読み込みだけでよい)。

---

## ストレージ I/O（DISK）ゲージの上限を WSL 環境のみ 7 GB/s に（2026-10-30）

**背景**: Whitebear2026-WSL（NVMe 直通）で運用中に読込が 7 GB/s 弱に達し、100% 基準が
5 GB/s のため DISK ゲージと統合時系列グラフが振り切れた。CrystalDiskMark のシーケンシャル
（R 17.7 / W 14.2 GB/s）までは実運用では出ないが、5 GB/s 基準では足りない。

**変更内容**（`front/index.html` のみ。API 再起動不要・DGX Spark / atom2 は 5 GB/s のまま）:

- `gaugeDefs()`: `const diskMax = PLAT.platform === "wsl" ? 7000 : 5000;` を追加し、
  `g-disk-io` の `max: 5000` → `max: diskMax`、サブテキスト `"上限 5 GB/s"` →
  `` `上限 ${diskMax / 1000} GB/s` ``（WSL は「上限 7 GB/s」）
- `buildMainChart()`: 統合時系列グラフの 100% 換算基準 `CHART_NORM.diskIO` を
  `5000` → `PLAT.platform === "wsl" ? 7000 : 5000` に（ゲージと同一基準）

**検証**（Whitebear2026-WSL、headless Chrome `--dump-dom http://localhost:8090/`）:

- `/api/platform` が `platform: "wsl"` を返し、DOM 上の DISK ゲージ サブ行が **「上限 7 GB/s」**
- `g-disk-io` は従来どおり描画され、他ゲージ（NW 上限 10 Gbps など）への影響なし

---

## GPU クロックゲージのサブ行を「最大 <実測値> MHz」に（2026-10-30）

**背景**: 2026-10-28 に `実 <現在値> / 最大 <clocks.max.sm> MHz` としたサブ行（2026-10-28 節）を、
運用者依頼で「最大 aaaa MHz」1 行に簡略化。**aaaa は HW 上限ではなく実測値**を入れる。

**変更内容**（`front/index.html` のみ。API 再起動不要）:

- `gaugeDefs()` の `g-gpu-clock` の `sub()`: 通常時の返却を
  `` `実 ${real} / 最大 ${mx} MHz` `` → `` `最大 ${real} MHz` `` に変更（`real` = `gpu_clock_mhz` の実測値）。
  不要になった `mx`（`gpu_clock_max_mhz`、無ければ discrete 2350 / GB10 3003 のフォールバック）を削除
- **据え置き**: サーマルガード発動中の `実 ${real} / ロック ${lock} MHz` 表示（`thermalClockLockMhz()`）、
  ゲージ目盛りの実測上限補正（`tuneGauge()` の `gpu_clock_max_mhz`）、統合時系列の 100% 基準
  （`CHART_NORM.gpuClock`）はそのまま。`gpu_clock_max_mhz` はゲージ・グラフの基準として引き続き使用

**検証**（本機 atom1 / DGX Spark、2026-10-30）:

- headless Chrome で全 script をパース → SyntaxError なし
- 稼働中の `http://localhost:8080/` を headless Chrome でスクリーンショット → GPU クロックカードの
  サブ行が **「最大 1665 MHz」**（1665 = `gpu_clock_mhz` の実測値。発動は非表示の通常フォーマット）。
  他ゲージ（NW 上限 10 Gbps など）に変化なし
- フロントは静的配信のためブラウザリロードで反映（`dgx-spark-api` 起動中 8080、再起動不要）
---

## WSL2 でサーマルガードを監視専らで導入する（2026-10-30）

**目的**: Whitebear2026-WSL2（RTX 5090 / 水冷）は温度が安定しており制限発動が不要なので、
従来 `thermal-guard` を載せていなかった（`/api/thermal` は `{"enabled":false}`）。atom1 / atom2 と
状態を揃え、**監視だけ**（温度・クロックの実測を `/run/thermal/state` → `/api/thermal` に流す）
載せられるようにする。

**変更**（`api/install_service.sh` のみ。atom1 / atom2 は挙動不変）:

- 従来は `/proc/version` が microsoft を含むと `TG_ENABLED=0` に**無条件で**落としていた
  （`DGXUTIL_THERMAL=1` を書いても上書きできず、WSL では導入手段がなかった）
- → WSL2 はオプトイン化: `DGXUTIL_THERMAL=1` のときだけ `TG_ENABLED=1` とし、同時に
  `TG_MONITOR_ONLY=1` を立てる（未指定 / `=0` は従来どおりスキップ）
- unit レンダリング時に `TG_SED` 配列へ `-e "s|^ExecStart=.*|&\nEnvironment=THERMAL_CONTAINERS=\nEnvironment=THERMAL_MODE=clock|"`
  を追加し、`[Service]` 内（`ExecStart` の直後）に 2 つの `Environment=` を挿入する。
  `^ExecStart=` は `ExecStartPre=` にマッチしないので先頭の待ち受け行はそのまま
- 冒頭コメントと `==>` 進捗表示にも監視専らの意味を明記（`thermal-guard=1 (monitor-only: THERMAL_CONTAINERS=)`）

**なぜこれで発動しないか**（`api/thermal_guard.sh` の clock ループ）:

- 発動条件は `running=1`（`docker ps --format '{{.Names}}'` の出力に `THERMAL_CONTAINERS` の
  名前が完全一致で並ぶ）**かつ** `max(acpitz, GPU温度) >= HIGH(90)℃`
- `THERMAL_CONTAINERS` が空なら `for c in $CONTAINERS` が回らず `running=0` 固定 →
  `want=0` となり `nvidia-smi -lgc` は一度も叩かれない（読み取りと状態ファイル更新のみ）
- **展開は `${VAR-default}`（コロン無し）が必須**。初版は `${VAR:-default}` のままだったため
  `Environment=THERMAL_CONTAINERS=` の**空文字が既定リストへフォールバック**し、導入時の起動ログが
  `containers=vllm-nemotron120b tabbyapi-ornith122b` になった（2026-10-30 07:14 実測）。当機は
  `docker ps` 0 件で `model_running=0` のため発動自体はしていないが、構造保証が壊れていた。
  → `api/thermal_guard.sh` を `${THERMAL_CONTAINERS-vllm-nemotron120b tabbyapi-ornith122b}` に修正。
  修正実測: `THERMAL_CONTAINERS= timeout 3 sh api/thermal_guard.sh` → `containers=`（空）、
  未設定 → `containers=vllm-nemotron120b tabbyapi-ornith122b`（**atom1 / atom2 は従来どおり**）
- 本機実測: acpitz zone は存在せず（`/sys/class/thermal/thermal_zone*` が空 = `a=0`）、
  `nvidia-smi --query-gpu=temperature.gpu` = **34℃**、`docker ps` は 0 件（modelctl 運用で
  対象コンテナ名自体が存在しない）
- フロントは `thermalState()` が `if (!t.locked_mhz) return null`、`thermalClockLockMhz()` も
  `locked_mhz > 0` のみ有効なので、`enabled:true` でも `locked_mhz=0` の間はヘッダのサーマル
  表示もカード枠線も従来どおり灰色（GPU クロックゲージの目盛りは HW 上限 3090 MHz のまま）

**副作用**: clock モードは起動時（`thermal_guard.sh` 174 行）と終了時（`on_exit`、161 行）に
`nvidia-smi -rgc`（クロック制限の**解除**）を 1 回ずつ叩く。WSL/GeForce では unsupported で
失敗する想定（stderr は破棄）だが、Windows 側で手動クロック制限を掛けている場合は解除されうる。
**導入手順**（WSL2 の各機で。`sudo` が必要）:

```bash
# 1) git 管理外のホスト固有設定を監視専らに切り替える
sed -i 's/^DGXUTIL_THERMAL=.*/DGXUTIL_THERMAL=1/' /home/cliclie/DGXSparkUtil/api/install_service.env
grep -n DGXUTIL_THERMAL /home/cliclie/DGXSparkUtil/api/install_service.env

# 2) unit を再 render + 起動。dgx-spark-api も再起動されるが、WSL は KILL_MODE=process なので
#    modelctl で起動した稼働中モデルサーバーは巻き込まれない
sudo bash /home/cliclie/DGXSparkUtil/api/install_service.sh

# 3) 確認 (sudo 不要)
systemctl --no-pager status thermal-guard | head -12
cat /run/thermal/state
curl -s localhost:8090/api/thermal
journalctl -u thermal-guard --no-pager | tail -5
```

期待する状態: `ActiveState=active`、`/run/thermal/state` が
`{"ts":...,"enabled":true,"temp_c":34,"acpitz_c":0,"gpu_c":34,"clock_mhz":...,"locked_mhz":0,"model_running":false,...}`、
`/api/thermal` が `{"enabled":false}` → `enabled:true` に変わる、
`journalctl` の先頭が `thermal-guard start: mode=clock ... containers=`（末尾が空 = 発動対象なし）。
※ `restart` 直後の 2 秒未満は `RuntimeDirectory=thermal` が削除された後で初回ループもまだ
ため、`/api/thermal` は `{"enabled":false}` を返す（誤検出に注意。数秒で `enabled:true` に復帰）。

**検証**（Whitebear2026-WSL、2026-10-30。コード変更時点の実測）:

- `bash -n api/install_service.sh` 通過
- 分岐の模擬実行（`IS_WSL` × `DGXUTIL_THERMAL` の 6 通り）:
  `IS_WSL=0` は未指定→`enabled=1 monitor=0` / `=0`→`0/0` / `=1`→`1/0`（**atom1 / atom2 は従来どおり**）、
  `IS_WSL=1` は未指定→`0/0` / `=0`→`0/0` / **`=1`→`1/1`**（監視専ら）
- 生成 unit の検証: `Environment=THERMAL_CONTAINERS=` / `Environment=THERMAL_MODE=clock` が
  `[Service]`（12 行 `ExecStart` の直後、48 行 `[Install]` より前）に入り、
  `systemd-analyze verify` は警告 0（exit 0）。`sh -n api/thermal_guard.sh` も通過

**本機の導入結果**（2026-10-30 07:14、`sudo bash api/install_service.sh`）:

- 進捗表示は `thermal-guard=1 (monitor-only: THERMAL_CONTAINERS=)`。`thermal-guard.service` は
  `active (running)`（MainPID = `/bin/sh .../thermal_guard.sh` + `sleep 2` = 2 秒周期）
- `/run/thermal/state` = `{"enabled":true,"temp_c":36,"acpitz_c":0,"gpu_c":36,"clock_mhz":615,`
  `"locked_mhz":0,"model_running":0,"low_c":85,"lock_high_mhz":1665,...}`、`/api/thermal` も同一
  （従来は `{"enabled":false}`）。`locked_mhz=0` なのでフロントのサーマル表示は灰色のまま
- `dgx-spark-api` も同時に 07:14:06 へ再起動され `HTTP check: 200`。`KILL_MODE=process` のため
  稼働中の strataq3 は生存。`server.log` は初回 truncate で 23M → 12K
  （退避: `/tmp/server.log.20261030-071358.bak`）
- 初回の起動ログが `containers=vllm-nemotron120b tabbyapi-ornith122b` だった（上記の落とし穴）
  → 修正後の再適用は `sudo systemctl restart thermal-guard` のみ（unit は変更不要）。
  修正スクリプトでの模擬起動では `containers=`（空）をすでに確認済み

**無効化（戻す）**:

```bash
sudo systemctl disable --now thermal-guard
sudo rm -f /etc/systemd/system/thermal-guard.service
sudo systemctl daemon-reload
sed -i 's/^DGXUTIL_THERMAL=.*/DGXUTIL_THERMAL=0/' /home/cliclie/DGXSparkUtil/api/install_service.env
curl -s localhost:8090/api/thermal    # {"enabled":false} に戻る
```
---

## GPU クロック ゲージのサブ行を「最大 <HW 上限> MHz」にする（2026-10-30 / 本機依頼）

**背景**: 同日に atom1 が「`実 <現在値> / 最大 <HW上限> MHz`」を「`最大 <実測値> MHz`」へ簡略化
した（2026-10-30 節）。Whitebear2026-WSL2 で運用中にサブ行が `最大 882 MHz` と表示されたが、
これは `nvidia-smi -q -d CLOCK` の **Clocks（実測 SM）** の値で、「最大」の語と合わない。
運用者依頼で **Max Clocks（= `clocks.max.sm`）** を表示するよう戻した。

**変更内容**（`front/index.html` のみ。API 再起動不要。**全機共通の表示**）:

- `gaugeDefs()` の `g-gpu-clock` の `sub()`: 通常時の返却を
  `` `最大 ${real} MHz` ``（`real` = `gpu_clock_mhz`）→ `` `最大 ${mx} MHz` ``
  （`mx` = `gpu_clock_max_mhz`、無ければ `discrete ? 2350 : 3003` のフォールバック）に戻す
- **据え置き**:
  - 半円内の中央値は実測クロックのまま（`get: (m) => m.gpu_clock_mhz`）
  - サーマルガード発動中の `実 <現在値> / ロック <Y> MHz`（`thermalClockLockMhz()`）。
    発動中は実値が読めるので、2026-10-28 節の「発動判断がつきにくい」問題の解消は維持
  - ゲージ目盛りの実測上限補正（`tuneGauge()` の `gpu_clock_max_mhz`）と
    統合時系列グラフの 100% 基準（`CHART_NORM.gpuClock`）

**検証**（Whitebear2026-WSL2、2026-10-30）:

- `/api/metrics` の `gpu_clock_max_mhz=3090.0` は `nvidia-smi -q -d CLOCK` の
  `Max Clocks → SM : 3090 MHz` と一致（`gpu_clock_mhz` は `Clocks → SM` の実測。この時 1440.0）
- headless Chrome の `--dump-dom`（ポート 8090）: GPU クロックカードのサブ行が **`最大 3090 MHz`**、
  半円中央値は `<div class="gval">1245<span class="unit">MHz</span>` と実測値のまま
- 回帰なし: DISK ゲージは `上限 7 GB/s`（WSL 分岐）を維持、NW は `上限 10 Gbps`

**留意**: 実測値の常時表示は無くなる（実値は半円中央値とツールチップで見る）。atom1 の
2026-10-30 実装メモ（「最大 = 実測値」）は履歴として残るが、このコミット以降の表示は HW 上限。
