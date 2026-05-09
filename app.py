#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infrastructure.lambda_snap_start_stack import LambdaSnapStartStack


app = cdk.App()

LambdaSnapStartStack(
    app,
    "LambdaSnapStartStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
    ),
)

app.synth()
