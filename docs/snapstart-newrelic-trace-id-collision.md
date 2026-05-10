# New Relic Python agent × Lambda SnapStart で trace ID が衝突する問題

## TL;DR

AWS Lambda SnapStart を有効にした Python ランタイムで New Relic Python agent (newrelic-lambda-extension 経由) を使うと、**同一 snapshot から並列復元された複数のコンテナが、その「最初のトランザクション」で完全に同じ traceId / spanId を生成する**。Distributed Tracing UI 上で複数 invocation が 1 本の trace に潰れて見える。

原因は **NR Python agent が trace ID を Python の `random` モジュールから生成しており、`random` 内部 state が snapshot に焼き込まれてしまう**こと。

ユーザ側では `snapshot-restore-py` の `@register_after_restore` フックで `random.seed()` を呼べば回避できる。本来は NR agent 側で同等の hook を持っておくべきと考えられる。

---

## 検証環境

| 項目 | 値 |
| --- | --- |
| Lambda runtime | Python 3.12 |
| Architecture | x86_64 |
| SnapStart | `ON_PUBLISHED_VERSIONS` |
| 起動経路 | Function URL → Alias `live` (= published version) |
| New Relic Layer | `arn:aws:lambda:ap-northeast-1:451483290750:layer:NewRelicPython312:20` |
| New Relic Lambda Extension | v2.3.14 |
| ハンドラ実装 | `urllib.request` で外部 URL (`https://httpbin.org/get`) を 1 回 GET |

リポジトリ構成は本リポジトリ (`infrastructure/`, `src/handler.py`) を参照。

## 再現条件

- Lambda SnapStart 有効
- New Relic Python agent (Lambda Layer + Extension) で APM
- **同 snapshot からの並列 cold 復元** (= 短時間に複数リクエストを投げて scaling を強制)
- 各復元コンテナの **最初の invocation**

warm 再利用 (= 同コンテナの 2 回目以降の invocation) では衝突しない。

## 症状

`Function URL` に対して **6 並列 + 30 秒後の 1 単発 (warm hit) = 7 invocation** を投下した結果。

### invocation 数 vs ユニーク traceId 数

```sql
FROM AwsLambdaInvocation
SELECT count(*), uniqueCount(traceId)
WHERE entityName = 'lambda-snap-start-test'
SINCE 5 minutes ago
```

**修正前**:

```
count: 7
uniqueCount.traceId: 2  ← 6 並列の cold restore が 1 traceId に衝突、warm hit が独立で 2
```

**修正後**:

```
count: 7
uniqueCount.traceId: 7  ← 全 invocation で別 traceId
```

### 衝突した trace の span 構造

```sql
FROM Span
SELECT timestamp, name, id, parent.id, span.kind
WHERE traceId = '5b3105ddf7f8d91b9c574510624902de'
SINCE 1 hour ago LIMIT 30
```

12 span が返ってきたが、**id 列が 2 種類しかなかった**:

- `id=02e0ad953ca6efa0` `name=Function/lambda-snap-start-test` (parent.id=null) × 6
- `id=ba4fe43ed33ccf34` `name=External/httpbin.org/urllib2/` (parent.id=02e0ad953ca6efa0) × 6

つまり **6 invocation 全部が「同じ root span ID」「同じ external span ID」を生成している**。timestamp と duration は invocation ごとに異なるので、別 invocation であることは間違いない。

### ハンドラ側の挙動 (補強)

ハンドラ内で snapshot に焼かれる UUID と、本来コンテナ固有であるべき UUID を返すようにした上で 6 並列を叩くと:

| invocation | snapshot_uuid (snapshot 焼込) | container_uuid (修正前: random.seed しない) |
| --- | --- | --- |
| #1 | `42c9e00d-...` | `965e0a9a-...` |
| #2 | `42c9e00d-...` | (修正前は #1 と同じ値、修正後は別値) |
| ... | 同上 | 同上 |

NR の trace ID 衝突と完全に同じパターン (= snapshot に焼かれた seed が原因) であることが、ユーザコード側でも観測できる。

## 原因

NR Python agent は trace ID / span ID を生成するために **`random.getrandbits(...)`** を使用している (`newrelic.core.transaction` 系)。

Python の `random` モジュールは Mersenne Twister の内部 state を持ち:

1. `import random` 時に `os.urandom` から一度だけ自動 seed される
2. その内部 state は **プロセスメモリ上にあるただの bytes 配列**

SnapStart はプロセスのメモリ状態を丸ごと snapshot に焼くため、**`random` の内部 state も snapshot に含まれる**。結果:

- snapshot から復元された全コンテナが **完全に同一の `random` 内部 state** で起動
- 各コンテナで `random.getrandbits(64)` を呼ぶと **全く同じ値が返る**
- NR の trace ID 生成も同じ値になる → trace ID 衝突

「**最初の 1 回**だけ衝突して、warm hit (= 2 回目) では衝突しない」のは、内部 state が 1 回呼び出すごとに進むため。同コンテナの 2 回目では既に state が独立して進んでいる。

これは AWS SnapStart のドキュメントが明確に「**乱数生成器 / 一意 ID は after-restore で再生成する必要がある**」と警告している、SnapStart の代表的な uniqueness violation の典型例。

## 影響

| 領域 | 影響 |
| --- | --- |
| Distributed Tracing UI | 複数の独立した invocation が 1 本の trace に潰れて表示される。リクエスト単位の追跡が事実上不可能 |
| trace ベースの分析 | trace ID で絞ると意図しない他リクエストの span まで含まれる |
| 外部呼び出しへの DT ヘッダ伝播 | 衝突した trace ID が呼び出し先サービスに渡されるので、相手側でも同 trace に紐付き混乱を招く可能性 |
| span ID ベースの重複排除 | NR の collector が将来的に span を重複として落とす可能性。現状は保持されているが保証は無い |

## 回避策 (本リポジトリの実装)

`src/requirements.txt` に AWS 公式の SnapStart hook ライブラリを追加:

```
snapshot-restore-py>=1.0.0
```

`src/handler.py` で hook を登録し、復元直後に `random.seed()` で再 seed する:

```python
import random
from snapshot_restore_py import register_after_restore

@register_after_restore
def after_restore():
    random.seed()  # os.urandom から再 seed → trace ID 衝突を解消
```

ポイント:

- **module load 時に decorator が実行される必要がある** (snapshot 撮影前に hook が登録されていなければならない)
- NR Lambda wrapper はユーザハンドラを **init phase で import する**ので、ハンドラのトップレベルに書けば snapshot 前に登録される (`module-init` ログが snapshot 時刻に出ていることを CloudWatch で確認済み)
- `random.seed()` は引数なしで呼ぶと `os.urandom` から再 seed される

## 推奨アクション

### 短期 (各プロジェクトで)

- SnapStart × NR Python の組合せを使っているプロジェクトは **上記の `@register_after_restore` を入れる**
- 既存 trace データの分析は **修正前後を区別**して扱う

### 中期 (NR への報告)

- NR Python agent の issue として upstream 報告:
  - リポジトリ: <https://github.com/newrelic/newrelic-python-agent>
  - 期待する修正: agent 内部で `snapshot-restore-py` の hook を自動登録 (もしくは agent が trace ID を `os.urandom` ベースで生成)
  - 関連: `newrelic_lambda_wrapper` 側で対応する手もある (<https://github.com/newrelic/newrelic-lambda-extension>)

## 参考

- [AWS Lambda SnapStart runtime hooks for Python](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-python.html)
- [snapshot-restore-py on PyPI](https://pypi.org/project/snapshot-restore-py/)
- [AWS Lambda SnapStart for Python and .NET (general availability)](https://aws.amazon.com/blogs/aws/aws-lambda-snapstart-for-python-and-net-functions-is-now-generally-available/)
- [New Relic Python agent (lambda_handler.py)](https://github.com/newrelic/newrelic-python-agent/blob/main/newrelic/api/lambda_handler.py)
- [New Relic Lambda Extension](https://github.com/newrelic/newrelic-lambda-extension)

## 補足: NR Lambda monitoring の SnapStart 対応状況

検証中に分かった隣接情報:

- NR は AWS Lambda の **SnapStart Restore Duration を構造化メトリクスとして取り込んでいない**
  - CloudWatch Logs の `REPORT` 行には `Restore Duration: ... ms`, `Billed Restore Duration: ... ms` が出る
  - しかし `AwsLambdaInvocation` の attributes、`ServerlessSample`、`Metric` (`aws.lambda.*`) のいずれにも `restoreDuration` / `initDuration` / `billedDuration` 相当が無い
- NR の Lambda monitoring UI では SnapStart 復元と通常の cold start を区別できない (`aws.lambda.coldStart=true` でまとめられる)
- NR Lambda Extension (Go バイナリ) 自体は snapshot 内で復元され、`mainLoop: waiting...` から続行する。Extension 自身は SnapStart 互換
- NR Python wrapper は agent 本体の初期化を **「最初の invocation」まで遅延** させており、これにより connection や heavy state を snapshot に焼かない設計になっている (= SnapStart 互換戦略)
