#!/usr/bin/env python3
import boto3, json, uuid, sys

AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:843074507558:runtime/document_processor-dvU2Bk3C7c"

prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hello"

client = boto3.client('bedrock-agentcore', region_name='us-east-1')
response = client.invoke_agent_runtime(
    agentRuntimeArn=AGENT_ARN,
    runtimeSessionId=f"session-{uuid.uuid4()}",
    payload=json.dumps({"prompt": prompt})
)
print(response['response'].read().decode('utf-8'))