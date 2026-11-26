# herdr-dashboard

herdr の状態をブラウザから閲覧・操作する Web アプリ。Python 標準ライブラリのみで動作します。

```sh
uv run server.py             # http://localhost:8765
PORT=9000 uv run server.py   # ポート変更
uv run test_server.py        # テスト
```

## できること

- pane の一覧と agent の状態(blocked を上に表示、agent のみに絞り込み可能)
- 過去の履歴: Claude の会話は「履歴」タブで最初まで遡れます。shell は端末タブに直近 1000 行を表示し、「全履歴を取得」で herdr が保持する全行を読めます(取得中は herdr 側の表示がスクロールします)
- PC では ⊞ で最大 4 pane の分割表示
- 端末画面の表示と操作(入力欄、キーボタン、端末にフォーカスして直接入力)。Claude の質問や許可確認にもここから回答できます
- Claude Code の subagent / shell / monitor の実行状態
- Claude Code が公開した Artifact へのリンク

## 別端末からの接続

既定で `0.0.0.0` で待ち受けます。起動時に表示される `他端末:` の URL (トークン付き) を開いてください。
同じ URL は画面の一覧下部にも表示されます。本端末の `localhost` はトークン不要です。

```sh
HOST=127.0.0.1 uv run server.py   # 本端末のみで待ち受ける
```

トークンは `~/.config/herdr-dashboard/token` に保存されます。削除して再起動すると再発行されます。

`tailscale serve` などのプロキシを本端末で使う場合は、許可するホスト名を指定します。
プロキシ経由は localhost 扱いになりトークンが不要になるため、到達できる人は誰でも任意のコマンドを実行できます。

```sh
HERDR_DASH_HOSTS=myhost.example.ts.net uv run server.py
```

## 注意

- 通信は暗号化されない HTTP です。信頼できるネットワーク内でのみ使ってください。
- トークン付き URL を知っている人は、このマシンで任意のコマンドを実行できます。
- 同じマシンの他のローカルユーザーは、トークンなしでアクセスできます。
- タスクと Artifact の情報は `~/.claude/projects` の transcript から取得しています(非公開の形式のため、Claude Code の更新で壊れる可能性があります)。
