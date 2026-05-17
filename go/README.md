# Go Lambda + New Relic (SAM 編)

Python 編 (`../infrastructure/`, `../src/`) と同じ「Lambda が外部 URL を 1 回 GET → New Relic で観測」という構成を、Go ランタイム + AWS SAM で再現したもの。SnapStart は使わない (AWS が Go runtime 非対応、Go は元々 cold start が速い)。

## 構成

- Runtime: `provided.al2023` (Go バイナリを `bootstrap` として配置)
- Architecture: x86_64 (Python 側と揃え)
- IaC: AWS SAM
- 計装: `github.com/newrelic/go-agent/v3` + `nrlambda` 統合
- テレメトリ転送: NewRelicLambdaExtension レイヤー (拡張バイナリのみ、言語別レイヤーは Go 用には無い)
- エンドポイント: Function URL (Auth: NONE)

## ファイル

```
go/
├── main.go               # ハンドラ (httpbin を GET、NR 自動計装)
├── go.mod / go.sum       # go mod tidy で deps 解決
├── Makefile              # sam build から呼ばれる (BuildMethod: makefile)
├── template.yaml         # SAM テンプレート
├── samconfig.toml.example
└── .gitignore
```

## 前提

- Go 1.22+ (`brew install go`)
  - `nrlambda` が go 1.25 を要求するので、go.mod に `toolchain go1.25.10` を入れてある
  - 1.22 系で `go build` を打つと自動で 1.25 のツールチェーンが DL される (`GOTOOLCHAIN=auto` のデフォルト動作)
- AWS SAM CLI (`brew install aws-sam-cli`)
- AWS CLI 認証情報 (既存のもので OK)
- New Relic ライセンスキー (Python 側と同じものを再利用可)
- Docker は不要 (SAM の Go ビルドは Makefile + ローカル Go)

## デプロイ

```sh
cd go

# 1) 依存解決 (初回および main.go の import 変更時)
go mod tidy

# 2) パラメータ設定 (初回のみ)
cp samconfig.toml.example samconfig.toml
# samconfig.toml の __REPLACE__ をライセンスキー / アカウントIDで埋める。
# NewRelicExtensionLayerVersion の最新版は以下のいずれかで確認:
#   - https://layers.newrelic-external.com/
#   - https://github.com/newrelic/newrelic-lambda-extension/releases (extension version → 対応 layer version)
#   - 特定 version の存在確認 (cross-account なので list ではなく get):
#       aws lambda get-layer-version \
#         --region ap-northeast-1 \
#         --layer-name arn:aws:lambda:ap-northeast-1:451483290750:layer:NewRelicLambdaExtension \
#         --version-number 73

# 3) ビルド + デプロイ
sam build
sam deploy
```

`sam deploy` の Outputs に Function URL が表示される。

## 動作確認

```sh
URL=$(aws cloudformation describe-stacks \
  --stack-name lambda-snap-start-test-go \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionUrl`].OutputValue' \
  --output text)

curl -sS "$URL" | jq

# 6 並列で叩いて cold start を複数発生させる
seq 1 6 | xargs -n1 -P6 -I{} curl -sS -o /dev/null "$URL"
```

New Relic UI:
- **APM & Services > `lambda-snap-start-test-go`** に出現
- **Distributed tracing**: Lambda 関数 → `External/httpbin.org/...` の親子 span ツリー
- **All Capabilities > AWS Lambda > `lambda-snap-start-test-go`**: Invocations / Duration / Errors

NRQL で Python 側と並べて比較:

```sql
FROM AwsLambdaInvocation
SELECT count(*), average(duration), uniqueCount(traceId)
WHERE entityName IN ('lambda-snap-start-test', 'lambda-snap-start-test-go')
FACET entityName
SINCE 30 minutes ago
```

## 後片付け

```sh
sam delete --stack-name lambda-snap-start-test-go
```

## 既知の注意点

- **Function URL の auth = NONE**: URL を知っていれば誰でも叩ける。検証用途と割り切り。残し続けるなら IAM auth に切り替え推奨
- **NR Extension Layer version は時々上がる**: `template.yaml` の default 値は静的なので、定期的に最新化推奨 (上記 `aws lambda list-layer-versions` コマンド参照)
- **NR エンティティ作成にラグ**: 初回テレメトリ受信後、APM Services 一覧に出るまで数分かかる。先に AWS アカウントを NR にリンクしておくこと (Python 側の検証で実施済みなら不要)
