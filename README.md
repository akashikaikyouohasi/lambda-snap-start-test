# lambda-snap-start-test

AWS Lambda の **SnapStart**（Python 3.12）を、**New Relic Lambda Extension** による APM／分散トレースと併用して試すための最小構成です。

- Runtime: Python 3.12（SnapStart 対応ランタイム）
- IaC: AWS CDK v2（Python）
- Observability: New Relic Lambda Layer + Extension（APM / 分散トレース / ログ転送）
- 動作確認: Function URL に GET → ハンドラ内で外部 URL（既定 `https://httpbin.org/get`）を `urllib.request` で呼び出し → 戻り値とレイテンシを返す

`urllib.request` は `http.client` 経由で New Relic Python エージェントに自動計装されるため、外部呼び出しが分散トレースの span として記録されます。

## ディレクトリ構成

```
.
├── app.py                                 # CDK エントリポイント
├── cdk.json
├── infrastructure/
│   └── lambda_snap_start_stack.py        # Lambda + Alias + Function URL + NR Layer
├── src/
│   └── handler.py                         # 外部 URL を叩くだけのハンドラ
└── requirements.txt                       # CDK の Python 依存
```

## 前提

- AWS CLI が設定済み（プロファイル / リージョン）
- Node.js + AWS CDK v2 (`npm i -g aws-cdk`)
- Python 3.12
- New Relic ライセンスキー & アカウント ID

> Docker は不要です。Lambda は追加 pip パッケージなしで動かしているため、`Code.from_asset` の素のアップロードだけで完結します。

## セットアップ

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## デプロイ

ライセンスキー／アカウント ID を CDK context で渡します。

```sh
cdk bootstrap   # 初回のみ
cdk deploy \
  -c new_relic_license_key=<NEW_RELIC_LICENSE_KEY> \
  -c new_relic_account_id=<NEW_RELIC_ACCOUNT_ID>
```

リージョンは `CDK_DEFAULT_REGION` または `AWS_REGION` で指定できます（既定: `ap-northeast-1`）。

### オプションの context

| key | 既定 | 用途 |
| --- | --- | --- |
| `new_relic_layer_version` | `20` | `NewRelicPython312` レイヤーのバージョン。最新は <https://layers.newrelic-external.com/> で確認 |
| `external_url` | `https://httpbin.org/get` | ハンドラから叩く外部 URL |

例：

```sh
cdk deploy \
  -c new_relic_license_key=... \
  -c new_relic_account_id=... \
  -c new_relic_layer_version=20 \
  -c external_url=https://api.example.com/healthz
```

## 動作確認

`cdk deploy` 後の Outputs に出る `FunctionUrl` を叩きます。

```sh
curl -s "$(aws cloudformation describe-stacks \
  --stack-name LambdaSnapStartStack \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
  --output text)" | jq
```

Function URL は **alias (`live`)** に紐付いているので、すべての呼び出しが SnapStart 対象の published version を経由します。

## SnapStart の確認ポイント

1. AWS コンソール > Lambda > 該当関数 > **Configuration > General** で *SnapStart: PublishedVersions* になっていること
2. **Versions** タブに alias `live` が指している version が並んでいること
3. CloudWatch Logs の `REPORT` 行で

   - 通常コールドスタート: `Init Duration: ...`
   - SnapStart 復元後: `Restore Duration: ...`

   が出力される。SnapStart が効いた呼び出しでは `Init Duration` ではなく `Restore Duration` が見える

4. New Relic 側
   - APM > Services > `lambda-snap-start-test`
   - Distributed tracing で 1 リクエストの中に外部 HTTP 呼び出しの span が含まれていることを確認

> 一度だけ叩くと warm のままなので、コールドを再現したいときは関数のメモリや環境変数を変更して publish させ直すか、しばらく時間を置いてから叩いてください。

## 後片付け

```sh
cdk destroy
```

## 既知の注意点

- **SnapStart は `$LATEST` には効かない**。必ず alias 経由で呼ぶこと（このスタックの Function URL は alias に紐付け済み）。
- **uniqueness 問題**: SnapStart はスナップショットを使い回すため、初期化時に発行した一意 ID／開いたコネクションは複数実行で共有されます。本サンプルではハンドラ内で都度コネクションを張る `urllib.request` を使っており影響なし。
- **New Relic レイヤーのバージョン**は時間とともに上がります。古いまま動かないときは `-c new_relic_layer_version=...` で更新してください。
