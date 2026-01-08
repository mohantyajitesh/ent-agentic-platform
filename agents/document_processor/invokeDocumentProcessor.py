#!/usr/bin/env python3
"""
Document Processor Agent Invocation Script
Uses environment variables and STS for dynamic ARN construction.
"""
import boto3
import json
import uuid
import sys
import os

# Configuration from environment variables
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ID = os.environ.get("DOCUMENT_PROCESSOR_AGENT_ID", "")

# Initialize clients
sts_client = boto3.client('sts', region_name=AWS_REGION)
agentcore_client = boto3.client('bedrock-agentcore', region_name=AWS_REGION)


def get_account_id() -> str:
    """Dynamically discover the current AWS account ID using STS."""
    return sts_client.get_caller_identity()['Account']


def build_agent_runtime_arn(account_id: str, agent_id: str, region: str) -> str:
    """Construct the agent runtime ARN from components."""
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{agent_id}"


def invoke_agent(prompt: str) -> str:
    """Invoke the document processor agent."""
    if not AGENT_RUNTIME_ID:
        raise ValueError(
            "DOCUMENT_PROCESSOR_AGENT_ID environment variable not set. "
            "Set it to your agent runtime ID (e.g., 'document_processor-dvU2Bk3C7c')"
        )

    account_id = get_account_id()
    agent_arn = build_agent_runtime_arn(account_id, AGENT_RUNTIME_ID, AWS_REGION)

    print(f"[invoke] Account: {account_id}")
    print(f"[invoke] Agent ARN: {agent_arn}")

    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=f"session-{uuid.uuid4()}",
        payload=json.dumps({"prompt": prompt})
    )
    return response['response'].read().decode('utf-8')


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hello"
    result = invoke_agent(prompt)
    print(result)