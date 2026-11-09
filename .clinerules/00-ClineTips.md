# Cline Tips

Clineで作業を行うときの所作

> **★最重要**: `run_commands` が `✖ Invalid input` を返したら、**チャットを作り直す前に**
> 「★★ run_commands が `✖ Invalid input` になったら」節（argv 形式 `{"command":..,"args":[..]}` への切替）
> を試すこと。2026-11-07 にセッション再開なしで復旧済み。旧来記載の「セッション再開が唯一の確実な復旧」は誤り。

## コマンドは30秒でエラーになる

-- 長いコマンドを実行するとき、sleepで遅延させるがsleepの時間は30秒未満にすること。

-- **タイムアウトすると実行中コマンドのプロセスツリーが強制終了される**。`nohup ... &` だけでは
   防げず、同じコマンド文字列内で待機（`sleep 28; curl ...` など）して30秒を超えると、
   デタッチしたつもりだった起動中の長時間プロセスまで巻き込んで殺される
   （ログが途中行で途切れ、失敗メッセージすら残らないので原因が分かりにくい）。
   起動に数分かかるもの（Strata のモデルロード等）は
   `setsid nohup <cmd> > /tmp/x.log 2>&1 < /dev/null &` でセッションを分離して**起動コマンドは即返し**、
   完了待ちの sleep は入れず、状態だけ別コマンドで20秒未満ごとに poll する。

-- 30秒以内に確実に返すには `timeout 25 <cmd>` で明示的に切る手もある（stop.sh 等の短時間確認に有効）。

## 6回同じコマンドを実行するとエラーになる

-- 同じコマンドを6回実行する前にpwd等の影響がないコマンドを空実行するか、clineが成功と判定するコマンドを実行すること。

## run_commands は「書き方」で回避できる

-- 引数のゲート（zod）が厳格で、`commands` は**文字列の配列**しか通らない。要素をオブジェクトにすると
   キー名を問わず `{"error":"✖ Invalid input"}` で呼び出し全体が死ぬ
   （例 `{"commands":[{"cmd":"git status"}]}`、`{"commands":[{"exec":"ls"}]}`）。
   上流に `cmd` 1 キーだけ通す PR cline/cline#13438 があるが、未マージ・人間レビュー 0・
   main 側が先に同じ `schemas.ts` を変更済み（#15089）で rebase 必要。当てにしないこと。
-- 送り方のルール:
   - NG: `{"commands":[{"cmd":"..."}]}` / `{"commands":[{"command":"..."}]}` / 文字列とオブジェクトの混在
   - OK: `{"commands":["git status","git --no-pager log"]}`（1 個でも必ず配列にする）
   - OK: argv を分けたい場合は**入力全体を** `{"command":"node","args":["-v"]}` の形にする
     （配列の要素としてではなく、トップレベルで渡すのが前提）
-- キー名を発明しない。`commands` / `files` / `queries` / `requests` など定義名どおりを使う
   （欠くと `✖ Invalid input: expected array, received undefined → at requests` のように
   zod がフィールドパス付きで弾く）。
-- 同じコマンドが成功したり失敗したり混在する場合は、書き方の問題ではなく次項のセッション破損が原因。
   **ただし次項のとおり、argv 形式への切り替えで復旧できることが多い**（セッション再開は最後）。

## ★★ run_commands が `✖ Invalid input` になったら: セッション再開前に argv 形式へ切り替える

**2026-11-07 の実測で、セッションを作り直さずに復旧した。** 従来の「セッション再開が唯一の確実な復旧」は
**誤り**なので、以下の優先順位で対処すること。

### 症状（2026-11-07 実例）
- `commands` 配列形式の呼び出しが、`echo ok` のような自明コマンドを含め **全滅**して `✖ Invalid input` を返す
- 同一セッション内で `echo ok` は通り、次の呼び出しは失敗する、という**間欠**パターンもある
- `editor` / `read_files` / `search_codebase` / `fetch_web_content` は終始正常に動く
  （＝ツール基盤の破損ではなく、`run_commands` の引数ゲートだけが壊れる）

### 復旧手順（上から順に試す。実測で 1 → 2 → 3 の順で成功した）
1. **`commands` 配列をやめ、トップレベル argv 形式に切り替える**（これが本命）
   - 効く例: `{"command":"echo","args":["probe"]}` → 成功
   - 壊れた `commands` 形式と同じ内容でも、`command` / `args` の分離なら通った
   - 実例: `{"command":"bash","args":["-lc","cd /home/x && git status --short"]}` → 成功
2. **args を「純粋な分割」にする**（1 要素に長いコマンド文字列を詰めない）
   - 強い: `{"command":"timeout","args":["24","bash","/tmp/run_bench.sh"]}` → 成功
   - 弱い: `{"command":"bash","args":["-lc","python3 bench.py --base http://... --key XXX --parallel 4"]}`
     → **同じ形でも失敗した**。`--base` / `--key` のような**長いフラグ連なりを 1 要素に詰めると弾かれやすい**
3. **コマンド本体をスクリプトファイルへ逃がす**（`editor` は生きているのでそれで書く）
   - `editor` で `/tmp/run_x.sh` を作り、`run_commands` からは `["24","bash","/tmp/run_x.sh"]` だけ呼ぶ
   - 引数が多い処理（bench・curl POST・git 複合）はこれで安定した
4. **それでも全滅なら** 新しいチャットでセッションを作り直す（従来どおりの最終手段）

### 書き方の補足（前回から変更なし）
- `commands` は**文字列の配列**のみ。要素をオブジェクトにするとキー名問わず呼び出し全体が死ぬ
- 同一コマンドの連発も避けると無難（`--` を含む長い単一文字列が最も弾かれやすい）
- 裏起動は `setsid nohup <cmd> > /tmp/x.log 2>&1 < /dev/null & echo PID=$!` を
  `{"command":"bash","args":["-lc","..."]}` の中に入れて即返す（30 秒ルールは維持）

### 状態変更（コミット・プッシュ・デプロイ）の途中で壊れたら
- 上記 1-3 で実行を継続できるのが最短。**復旧を諦めて利用者に代行を頼むのは 4 の後**
- いずれにせよ推測で「完了した」と報告しない。実行できた範囲と残りを切り分けて提示する

### 予防（変更なし）
- `Start-Process` 等で起こした dev サーバー / preview / puppeteer(Chrome) を残したまま長時間作業しない。
  検証したら必ず kill してプロセスを積まないこと
- 上流バグ cline/cline#10506 は依然 not_planned でクローズ済み＝修正パッチは期待できない。
  **回避策（argv 形式）を標準手段として覚える**のが現実的な解

# LLM Thinking & Output Constraints
- **Do not output loose markdown formatting inside `<thinking>` tags.** Never use triple backticks (```) or unclosed symbols inside the thinking block, as it may break the parser or cause a token timeout.
- **Ensure to close the `</thinking>` tag promptly.** Keep the inner thought process concise and actionable. Do not wander or repeat the same logical loops infinitely.
- **Do not write full code snippets inside `<thinking>`.** Only outline the logic. Actual code modifications must be deferred to the text outside or inside specific tool calls.
- **Avoid over-thinking on simple tasks.** If the next step is obvious, immediately close the `</thinking>` block and execute the appropriate tool or respond to the user.
- **Strictly enforce structured outputs.** If you need to reason in Japanese, do so cleanly, but ensure that all XML-like tags (e.g., `<thinking>`, `</thinking>`) remain perfectly intact in raw English syntax without any modifications or trailing text.
