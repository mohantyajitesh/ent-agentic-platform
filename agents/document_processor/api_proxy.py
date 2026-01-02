"""
Document Processor AI - API Gateway Lambda Proxy
"""
import boto3
import json
import os
import uuid

AGENT_RUNTIME_ARN = os.environ.get('AGENT_RUNTIME_ARN')
REGION = os.environ.get('AWS_REGION', 'us-east-1')

agentcore_client = boto3.client('bedrock-agentcore', region_name=REGION)

def lambda_handler(event, context):
    try:
        # Parse request body
        if isinstance(event.get('body'), str):
            body = json.loads(event['body'])
        else:
            body = event.get('body', {}) or {}

        prompt = body.get('prompt', '')
        session_id = body.get('session_id', str(uuid.uuid4()))

        if not prompt:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'No prompt provided',
                    'status': 'error'
                })
            }

        # Session ID must be at least 33 characters
        if len(session_id) < 33:
            session_id = session_id + '-' + str(uuid.uuid4())

        # Invoke AgentCore
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps({'prompt': prompt})
        )

        # Read response - handle string or dict
        response_body = response['response'].read().decode('utf-8')
        
        try:
            result = json.loads(response_body)
            if isinstance(result, str):
                response_text = result
            else:
                response_text = result.get('response', result.get('result', response_body))
        except json.JSONDecodeError:
            response_text = response_body

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'response': response_text,
                'session_id': session_id,
                'status': 'success'
            })
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'error': str(e),
                'status': 'error'
            })
        }