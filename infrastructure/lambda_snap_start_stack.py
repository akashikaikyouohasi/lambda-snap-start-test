import sys

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_lambda as lambda_,
)
from constructs import Construct


# New Relic publishes Lambda layers under this account in every supported region.
# https://layers.newrelic-external.com/
NEW_RELIC_LAYER_ACCOUNT = "451483290750"
NEW_RELIC_PYTHON_LAYER_NAME = "NewRelicPython312"


class LambdaSnapStartStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        license_key = self.node.try_get_context("new_relic_license_key")
        account_id = self.node.try_get_context("new_relic_account_id")
        layer_version = str(
            self.node.try_get_context("new_relic_layer_version") or "20"
        )
        external_url = (
            self.node.try_get_context("external_url") or "https://httpbin.org/get"
        )

        if not license_key or not account_id:
            print(
                "[LambdaSnapStartStack] WARNING: New Relic context is missing. "
                "Synth will continue with placeholder values so that `cdk bootstrap` "
                "and `cdk synth` work, but the deployed Lambda will not report to "
                "New Relic until you redeploy with "
                "`-c new_relic_license_key=<KEY> -c new_relic_account_id=<ID>`.",
                file=sys.stderr,
            )
            license_key = license_key or "PLACEHOLDER_LICENSE_KEY"
            account_id = account_id or "PLACEHOLDER_ACCOUNT_ID"

        nr_layer_arn = (
            f"arn:aws:lambda:{self.region}:{NEW_RELIC_LAYER_ACCOUNT}:"
            f"layer:{NEW_RELIC_PYTHON_LAYER_NAME}:{layer_version}"
        )
        nr_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "NewRelicLayer", nr_layer_arn
        )

        function = lambda_.Function(
            self,
            "SnapStartFunction",
            function_name="lambda-snap-start-test",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            # The New Relic wrapper takes over as the entry point and dispatches
            # to the real handler defined in NEW_RELIC_LAMBDA_HANDLER.
            handler="newrelic_lambda_wrapper.handler",
            code=lambda_.Code.from_asset("src"),
            memory_size=512,
            timeout=Duration.seconds(15),
            snap_start=lambda_.SnapStartConf.ON_PUBLISHED_VERSIONS,
            layers=[nr_layer],
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "NEW_RELIC_LAMBDA_HANDLER": "handler.lambda_handler",
                "NEW_RELIC_LICENSE_KEY": license_key,
                "NEW_RELIC_ACCOUNT_ID": account_id,
                "NEW_RELIC_APP_NAME": "lambda-snap-start-test",
                "NEW_RELIC_LAMBDA_EXTENSION_ENABLED": "true",
                "NEW_RELIC_EXTENSION_SEND_FUNCTION_LOGS": "false",
                "NEW_RELIC_DISTRIBUTED_TRACING_ENABLED": "true",
                # Crank up logging so we can observe SnapStart behaviour:
                # - the Python agent logs init / restore lifecycle at debug
                # - the Go extension logs its checkpoint/restore handling
                "NEW_RELIC_LOG_LEVEL": "debug",
                # Send the Python agent's log to stderr so CloudWatch captures it,
                # otherwise it goes to a file inside the container that nothing reads.
                "NEW_RELIC_LOG": "stderr",
                "NEW_RELIC_EXTENSION_LOG_LEVEL": "DEBUG",
                "NEW_RELIC_DEBUG_ON_ERROR": "true",
                "EXTERNAL_URL": external_url,
            },
        )

        alias = lambda_.Alias(
            self,
            "LiveAlias",
            alias_name="live",
            version=function.current_version,
        )

        # Function URL is attached to the alias so invocations always go through
        # a published version (which is what SnapStart accelerates).
        function_url = alias.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
        )

        CfnOutput(self, "FunctionName", value=function.function_name)
        CfnOutput(self, "FunctionAliasArn", value=alias.function_arn)
        CfnOutput(self, "FunctionUrl", value=function_url.url)
