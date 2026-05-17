# SAM の Metadata.BuildMethod=makefile では `build-<LogicalId>` という命名で
# ターゲットが呼ばれる。ここでは template.yaml の GoFunction に対応する。

build-GoFunction:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
		go build -tags lambda.norpc -o $(ARTIFACTS_DIR)/bootstrap ./

.PHONY: build-GoFunction
