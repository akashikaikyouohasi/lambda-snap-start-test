package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/events"
	"github.com/newrelic/go-agent/v3/integrations/nrlambda"
	"github.com/newrelic/go-agent/v3/newrelic"
)

type response struct {
	ExternalURL    string `json:"external_url"`
	ExternalStatus int    `json:"external_status"`
	ElapsedMS      int64  `json:"elapsed_ms"`
	BodyPreview    string `json:"external_body_preview"`
}

func handler(ctx context.Context, _ events.LambdaFunctionURLRequest) (events.LambdaFunctionURLResponse, error) {
	url := os.Getenv("EXTERNAL_URL")
	if url == "" {
		url = "https://httpbin.org/get"
	}

	txn := newrelic.FromContext(ctx)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return events.LambdaFunctionURLResponse{StatusCode: 500}, err
	}
	httpReq.Header.Set("User-Agent", "lambda-snap-start-test/go/1.0")
	if txn != nil {
		httpReq = newrelic.RequestWithTransactionContext(httpReq, txn)
	}

	client := &http.Client{Transport: newrelic.NewRoundTripper(http.DefaultTransport)}

	started := time.Now()
	resp, err := client.Do(httpReq)
	if err != nil {
		return events.LambdaFunctionURLResponse{StatusCode: 502}, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	elapsedMs := time.Since(started).Milliseconds()

	preview := string(body)
	if len(preview) > 500 {
		preview = preview[:500]
	}

	out, _ := json.Marshal(response{
		ExternalURL:    url,
		ExternalStatus: resp.StatusCode,
		ElapsedMS:      elapsedMs,
		BodyPreview:    preview,
	})

	return events.LambdaFunctionURLResponse{
		StatusCode: 200,
		Headers:    map[string]string{"Content-Type": "application/json"},
		Body:       string(out),
	}, nil
}

func main() {
	// nrlambda.ConfigOption() puts the agent into serverless mode (telemetry
	// goes via stdout for the New Relic Lambda Extension to forward).
	// newrelic.ConfigFromEnvironment() picks up NEW_RELIC_APP_NAME,
	// NEW_RELIC_DISTRIBUTED_TRACING_ENABLED, NEW_RELIC_LICENSE_KEY etc.
	app, err := newrelic.NewApplication(
		nrlambda.ConfigOption(),
		newrelic.ConfigFromEnvironment(),
	)
	if err != nil {
		log.Printf("newrelic.NewApplication: %v", err)
	}
	nrlambda.Start(handler, app)
}
