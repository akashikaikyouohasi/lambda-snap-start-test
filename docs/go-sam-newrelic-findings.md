# Go + AWS SAM + New Relic Lambda: 検証ノート

## TL;DR

Python 編 ([../infrastructure/](../infrastructure/), [../src/](../src/)) と同じ
「Lambda が httpbin を 1 回 GET → New Relic で観測」のシナリオを Go + AWS SAM
に書き換えて並べた結果、以下が判明：

1. **New Relic Lambda Extension のバージョン差で、`AwsLambdaInvocation` に
   出る属性が変わる**。とりわけ **`cwInitDuration` (= cold start 時間)** は
   extension v2.5.x 以降でしか出ない
2. **Go ネイティブの cold start は ~290 ms**。Python が SnapStart で頑張って
   叩き出す Restore Duration (~600 ms) より速い
3. NR distributed trace ペイロードの `"ap":"Unknown"` は serverless モード
   特有の cosmetic 仕様 (Python 編で慌てたが、バグではない)
4. SAM の運用ハマりどころ数点 (`CAPABILITY_AUTO_EXPAND`、Transform の日付、
   cross-account layer の version 確認手段、Docker 不要の Go ビルド)

## 環境

| 項目 | 値 |
| --- | --- |
| Lambda runtime | `provided.al2023` (Go バイナリを bootstrap として配置) |
| Architecture | x86_64 |
| IaC | AWS SAM CLI |
| 計装 | `github.com/newrelic/go-agent/v3` + `nrlambda` 統合 |
| Lambda Layer | `NewRelicLambdaExtension:73` (extension v2.5.2、x86_64) |
| 起動経路 | Function URL (Auth: NONE) → 公開バージョン |
| 比較対象 (Python 編) | `NewRelicPython312:20` (extension v2.3.14 がバンドル) |

## 発見 1: Extension バージョンで `AwsLambdaInvocation` の属性が変わる

### 何が違うか

同じ NR Lambda Extension という仕組みでも、layer バージョンによって
`AwsLambdaInvocation` イベントに乗ってくる属性数が違う。

```sql
FROM AwsLambdaInvocation
SELECT keyset()
WHERE entityName IN ('lambda-snap-start-test', 'lambda-snap-start-test-go')
SINCE 1 hour ago
```

| 属性 (numeric) | Python 編 (extension v2.3.14) | Go 編 (extension v2.5.2) |
| --- | --- | --- |
| `duration` | ✅ | ✅ |
| `externalDuration` | ✅ | ✅ |
| `aws.lambda.coldStart` (bool/numeric) | ✅ | ✅ |
| **`cwInitDuration` / `cloudWatchInitDuration`** | ❌ | ✅ |
| **`cwBilledDuration` / `cloudWatchBilledDuration`** | ❌ | ✅ |
| **`cwDuration` / `cloudWatchDuration`** | ❌ | ✅ |
| **`cwMaxMemoryUsed` / `maxMemoryUsed`** | ❌ | ✅ |
| **`cwMemorySize` / `memorySize`** | ❌ | ✅ |
| `newrelic.extensionVersion` | ❌ | ✅ |

`cw*` 系属性は extension が CloudWatch Logs の `REPORT` 行をパースして
attribute 化してくれるもの。**extension v2.5.x で追加された機能**。古い
extension では同じ Lambda を動かしても出てこない。

### なぜそうなるか

仕組み上、CloudWatch の `REPORT` 行 (例:
`REPORT RequestId: ...  Duration: 156.62 ms  Billed Duration: 157 ms  Init Duration: 290.07 ms ...`)
は Lambda が CloudWatch Logs に出力するもので、NR Extension は Logs API を
購読してこの行を拾い、構造化して NR collector へ送る。

この「REPORT 行を構造化して属性化する」処理が **extension v2.5.x で実装
された** ため、それ以前の extension では `cwInitDuration` 等が NR に到達
しない。Python の language layer は agent と extension をバンドルしている
ので、layer のバージョンがそのまま extension バージョンに直結する。

### 影響

- Python 編で「cold start (Init Duration) が NR で計測できない」と書いた件は
  **layer をアップグレードすれば解消**する可能性が高い
- 具体的には `NewRelicPython312` を新しいバージョン (extension v2.5.x が
  バンドルされたもの) に上げて再 deploy

### 推奨

`NewRelicPython312` の最新版で extension のバージョンを確認する。手順:

```sh
# 候補バージョンを探って、Description から extension version を確認
for v in 25 24 23 22 21 20; do
  echo -n "layer v$v: "
  aws lambda get-layer-version \
    --region ap-northeast-1 \
    --layer-name arn:aws:lambda:ap-northeast-1:451483290750:layer:NewRelicPython312 \
    --version-number $v \
    --query 'Description' --output text 2>&1 | head -1
done
```

`extension v2.5.x` が混ざるバージョンが見つかれば、それで Python 編を
再 deploy → `FROM AwsLambdaInvocation SELECT cwInitDuration` が出るか確認。

## 発見 2: Go cold start vs Python SnapStart restore

### Go (素の cold start)

```sql
FROM AwsLambdaInvocation
SELECT count(*), average(cwInitDuration), max(cwInitDuration), min(cwInitDuration)
WHERE entityName = 'lambda-snap-start-test-go'
FACET aws.lambda.coldStart
SINCE 1 hour ago
```

| | cold (true) |
| --- | --- |
| count | 4 |
| avg cwInitDuration | **291.5 ms** |
| min / max | 289 / 295 ms |

ばらつきがほぼ無い (実態 290 ms ± 3 ms)。Go の静的バイナリ起動 + NR Go agent
の `newrelic.NewApplication` 初期化を含めた時間。

### Python + SnapStart (CloudWatch ログから手で読んだ値)

```
INIT_REPORT Init Duration: 2510.65 ms    (snapshot 作成時、1 回だけ)
RESTORE_REPORT Restore Duration: 537.61 ms / 288.18 ms / ...  (各 restore で)
```

NR に構造化メトリクスとしては出ないが、CloudWatch 上では Restore Duration
~300〜700 ms。

### 比較

| ケース | cold start 中央値 | NR で構造化計測 |
| --- | --- | --- |
| Go (no SnapStart) | **~290 ms** | ✅ `cwInitDuration` |
| Python + SnapStart restore | **~300〜700 ms** | ❌ (extension 古い、かつ `restoreDuration` 属性は未実装) |
| Python なし SnapStart (参考) | ~2500 ms (Init Duration) | △ (extension 上げれば `cwInitDuration` で見える可能性) |

**「Python は SnapStart 入れて頑張って ~600 ms、Go は素で 290 ms」** という、
ランタイム特性の差がはっきり出る。SnapStart は Python の cold start 問題の
defensive な解決策、Go なら最初から不要、という構図。

## 発見 3: `"ap":"Unknown"` は serverless モードの仕様

httpbin に到達するリクエストヘッダの `Newrelic` ヘッダを base64 デコード
すると以下:

```json
{"ty":"App","ap":"Unknown","ac":"7674913","tx":"...","id":"...","tr":"...",...}
```

`ap` (application identifier) が "Unknown" のまま。Python 編で最初に観測
した時は「app name の設定漏れ」と私 (Claude) が決め付けて
`NEW_RELIC_APP_NAME` 環境変数を追加したが、その後の Go 編で
**env を正しく設定し、agent も `ConfigFromEnvironment` で読み込んでも、
やはり `ap` は "Unknown" のまま** であることを確認した。

### 理由

NR Go agent の `nrlambda.ConfigOption()` は serverless モードに切り替える
処理で、この状態では **agent は NR collector に登録しない** (= application
id をもらえない)。distributed trace ペイロードの `ap` フィールドは
application id を入れる場所なので、未登録時のフォールバックとして
"Unknown" 文字列が入る。

ソース確認: `newrelic-go-agent/v3/integrations/nrlambda/config.go` の
`ConfigOption` は `cfg.AppName` を一切触らない。`ConfigFromEnvironment` は
`NEW_RELIC_APP_NAME` を `cfg.AppName` には書き込むが、serverless mode 下では
これと distributed trace の `ap` フィールドが直接連動しない。

### 影響と回避

- **NR UI 側の entity 名 (`entityName`) は別経路で正しく付く**。
  `NEW_RELIC_APP_NAME` 環境変数 + serverless ingest 側のメタ情報から
  `lambda-snap-start-test-go` という名前で APM Services / NRQL に出る
- distributed trace ペイロードの `ap:Unknown` はそのまま残るが、
  これは外部サービス (httpbin など) に渡された時に相手側のログに残る
  程度の影響で、NR UI には影響しない
- 気持ち悪いだけで、機能的には問題なし

## 発見 4: SAM 周りハマりポイント集

### 4-1. `CAPABILITY_AUTO_EXPAND` が必須 (権限エラーに見える)

SAM templete の `Transform: AWS::Serverless-2016-10-31` は CloudFormation
Macro 扱い。Macro を実行する change set 作成には `CAPABILITY_AUTO_EXPAND`
capability が必要。これを付けないと **AdministratorAccess 持っていても**
以下のエラーが出る:

```
not authorized to perform: cloudformation:CreateChangeSet on resource:
arn:aws:cloudformation:ap-northeast-1:aws:transform/Serverless-2016-10-31
```

エラー文言が "not authorized" なので IAM 問題に見えるが実体は capability
不足。`samconfig.toml`:

```toml
capabilities = "CAPABILITY_IAM CAPABILITY_AUTO_EXPAND"
```

### 4-2. Transform の日付は `2016-10-31`

`AWSTemplateFormatVersion: '2010-09-09'` (CloudFormation のテンプレフォーマット
バージョン) と紛らわしいが、**SAM Transform は `AWS::Serverless-2016-10-31`**。
別の日付 (例: `2010-05-13`) を書くと、リージョンによって挙動が変わる:

- us-east-1: なぜか通った (レガシー互換と思われる)
- ap-northeast-1: change set 作成段階で deny

公式は `2016-10-31` のみ。これに統一すること。

### 4-3. Cross-account layer の version は `list` 不可、`get` で探る

NR の layer は `arn:aws:lambda:<region>:451483290750:layer:...` で、別アカウントの
リソース。`aws lambda list-layer-versions` は cross-account 不可で
`AccessDeniedException` になる。

代わりに **当てに行く方式** で探る:

```sh
for v in 100 90 80 75 70 65; do
  echo -n "v$v: "
  aws lambda get-layer-version \
    --region ap-northeast-1 \
    --layer-name arn:aws:lambda:ap-northeast-1:451483290750:layer:NewRelicLambdaExtension \
    --version-number $v \
    --query 'Description' --output text 2>&1 | head -1
done
```

存在しないバージョンは空文字、存在すれば Description が返る。二分探索で
最新版を絞れる。今回は **73 が extension v2.5.2 = 最新** だった (2026-05 時点)。

### 4-4. Docker 不要の Go ビルド (`BuildMethod: makefile`)

SAM の Go ビルドは Docker 必須に見えがちだが、`Metadata.BuildMethod: makefile`
を指定すれば任意の Makefile ターゲットを使える:

```yaml
Resources:
  GoFunction:
    Type: AWS::Serverless::Function
    Metadata:
      BuildMethod: makefile
    Properties:
      Handler: bootstrap
      Runtime: provided.al2023
      CodeUri: ./
```

```makefile
build-GoFunction:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
		go build -tags lambda.norpc -o $(ARTIFACTS_DIR)/bootstrap ./
```

ターゲット名は `build-<LogicalId>` 固定。ローカル Go でクロスコンパイル
するだけなので Docker 不要、CI でも軽い。`lambda.norpc` タグで legacy net/rpc
コードを除外して数 MB 削減。

## 推奨アクション

| 優先 | アクション |
| --- | --- |
| 高 | Python 編の NR layer を新しいバージョンに上げて、`cwInitDuration` が出るか再検証。出るなら Python 側でも cold start を NR 一本で観測できる |
| 中 | Python の SnapStart 検証 + Go の素 cold start 検証を 1 枚のダッシュボードに並べて、ランタイム別 cold start 特性として社内共有 |
| 低 | `ap:Unknown` 問題は NR Go agent / Python agent 両方で同じ。気になるなら NR support に「serverless モードで `ap` が `entity.name` ベースに置き換えられないか」と機能要望として上げる |

## 参考

- [Python 編の SnapStart × trace ID 衝突レポート](./snapstart-newrelic-trace-id-collision.md)
- [Go の `newrelic-go-agent/v3/integrations/nrlambda`](https://github.com/newrelic/go-agent/tree/master/v3/integrations/nrlambda)
- [New Relic Lambda Extension リリース履歴](https://github.com/newrelic/newrelic-lambda-extension/releases)
- [AWS SAM `BuildMethod: makefile`](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/building-custom-runtimes.html)
- [AWS CloudFormation Capabilities (`CAPABILITY_AUTO_EXPAND`)](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-macros.html)
