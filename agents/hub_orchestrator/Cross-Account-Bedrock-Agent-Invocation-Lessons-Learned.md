# Cross-Account Bedrock Agent Invocation - Lessons Learned

## Overview

This document captures the challenges, solutions, and best practices discovered while implementing cross-account Bedrock Agent invocation for the Harbor AI Hub orchestrator.

**Scenario:** Hub orchestrator in Account A needs to invoke Bedrock Agents deployed in Account B.

| Component | Account | Resource |
|-----------|---------|----------|
| Hub Orchestrator | 843074507558 (Source) | AgentCore Runtime |
| Bedrock Agent | 152864141302 (Target) | Bedrock Agent PEGHWIVI5Y |

---

## Problem Statement

Hub orchestrator in Account A (843074507558) needed to invoke a Bedrock Agent in Account B (152864141302). Despite successful role assumption, agent invocation consistently failed with `ResourceNotFoundException`.

---

## What We Tried (And Why It Failed)

| Attempt | Approach | Why It Failed |
|---------|----------|---------------|
| 1 | Pass credentials directly to `boto3.client()` | boto3 sometimes ignores passed credentials if default credential chain is cached |
| 2 | Set `os.environ` variables | boto3 credential caching ignores runtime env var changes |
| 3 | Cache cross-account client | STS credentials expire after ~1 hour, cached client had stale credentials |

---

## Root Causes Identified

### 1. boto3 Credential Caching

```python
# PROBLEM: boto3.client() may ignore passed credentials
client = boto3.client(
    'bedrock-agent-runtime',
    aws_access_key_id=creds['AccessKeyId'],      # Sometimes ignored!
    aws_secret_access_key=creds['SecretAccessKey'],
    aws_session_token=creds['SessionToken']
)
```

boto3 has an internal credential resolver chain. Once credentials are resolved, they may be cached, causing new clients to ignore explicitly passed credentials.

### 2. Environment Variables Not Picked Up

```python
# PROBLEM: Setting env vars after boto3 is imported
os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
client = boto3.client('bedrock-agent-runtime')  # May use cached credentials
```

boto3 reads environment variables at initialization. Setting them at runtime doesn't guarantee new clients will use them.

### 3. Wrong Variable Used in Invocation

```python
# PROBLEM: Using global client instead of cross-account client
bedrock = get_cross_account_bedrock_client(account_id)  # Returns cross-account
response = bedrock_client.invoke_agent(...)  # BUG: Using wrong variable!
```

A simple variable naming mistake caused the code to use the global `bedrock_client` (configured for the local account) instead of the `bedrock` variable containing the cross-account client.

### 4. DynamoDB Data Format Issues

```python
# PROBLEM: Extra quote in alias_id
"IO1GTTHP0M""  # Had trailing quote, caused validation error

# PROBLEM: Capabilities stored as string, not list
"capabilities": "Extract text..."  # Iterated character by character
```

---

## The Solution

### 1. Use `boto3.Session` for Isolated Credentials

```python
def get_cross_account_bedrock_client(account_id: str):
    """Assume role using boto3.Session (isolated credentials)."""
    
    print(f"[Hub] Assuming role for {account_id}...")
    
    # Use existing client for STS call
    sts = boto3.client('sts', region_name=REGION)
    
    assumed = sts.assume_role(
        RoleArn=CROSS_ACCOUNT_ROLE_ARN,
        RoleSessionName=f"HubSession-{account_id}",
        ExternalId=CROSS_ACCOUNT_EXTERNAL_ID
    )
    
    creds = assumed['Credentials']
    
    # KEY FIX: Create isolated Session with assumed credentials
    session = boto3.Session(
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken'],
        region_name=REGION
    )
    
    # Verify identity using session's STS client
    identity = session.client('sts').get_caller_identity()
    print(f"[Hub] ✅ Identity: Account={identity['Account']}")
    
    if identity['Account'] != account_id:
        raise Exception(f"Expected account {account_id}, got {identity['Account']}")
    
    # Return client created FROM the session
    return session.client('bedrock-agent-runtime')
```

### 2. Use Correct Variable for Invocation

```python
if account_id and account_id != LOCAL_ACCOUNT_ID:
    bedrock = get_cross_account_bedrock_client(account_id)
else:
    bedrock = bedrock_client

# USE THE VARIABLE, NOT THE GLOBAL
response = bedrock.invoke_agent(  # ✅ Correct
    agentId=agent_id,
    agentAliasId=alias_id,
    sessionId=session_id,
    inputText=user_request
)
```

---

## Why `boto3.Session` Works

| Approach | Credential Scope | Isolation |
|----------|-----------------|-----------|
| `boto3.client(..., aws_access_key_id=...)` | Per-client (unreliable) | ❌ May be ignored |
| `os.environ['AWS_ACCESS_KEY_ID'] = ...` | Global (unreliable) | ❌ Caching issues |
| `boto3.Session(...).client(...)` | Per-session (reliable) | ✅ Fully isolated |

`boto3.Session` creates a completely isolated credential context. All clients created from that session are guaranteed to use the session's credentials.

---

## Shell Script vs Python Comparison

Understanding why the shell script worked helps explain the Python solution.

### Shell Script (Works)

```bash
# Assume role and capture output
ROLE_OUTPUT=$(aws sts assume-role \
    --role-arn arn:aws:iam::152864141302:role/cmcCrossAccountBedrockInvokeRole \
    --role-session-name BedrockSession-$(date +%s) \
    --external-id "bedrock-cross-account-2024" \
    --output json)

# Export credentials as environment variables
export AWS_ACCESS_KEY_ID=$(echo $ROLE_OUTPUT | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $ROLE_OUTPUT | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $ROLE_OUTPUT | jq -r '.Credentials.SessionToken')

# New process, reads env vars fresh
aws bedrock-agent-runtime invoke-agent ...
```

**Why it works:** Each AWS CLI call is a new process that reads environment variables fresh.

### Python Equivalent (Works)

```python
# Assume role
assumed = sts.assume_role(
    RoleArn=CROSS_ACCOUNT_ROLE_ARN,
    RoleSessionName=f"HubSession-{account_id}",
    ExternalId=CROSS_ACCOUNT_EXTERNAL_ID
)

creds = assumed['Credentials']

# Create isolated session (equivalent to shell's export)
session = boto3.Session(
    aws_access_key_id=creds['AccessKeyId'],
    aws_secret_access_key=creds['SecretAccessKey'],
    aws_session_token=creds['SessionToken']
)

# Create client from session
client = session.client('bedrock-agent-runtime')
```

**Why it works:** `boto3.Session` creates an isolated credential context, similar to how a new shell process reads fresh environment variables.

---

## IAM Setup Required

### Account A (Hub - Source Account)

The hub orchestrator's execution role needs permission to assume the cross-account role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumesCrossAccountRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::152864141302:role/cmcCrossAccountBedrockInvokeRole"
    }
  ]
}
```

### Account B (Spoke - Target Account)

Create a role that Account A can assume:

**Trust Policy (Who can assume this role):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::843074507558:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "bedrock-cross-account-2024"
        }
      }
    }
  ]
}
```

**Permission Policy (What the role can do):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeAgent",
      "Resource": [
        "arn:aws:bedrock:us-east-1:152864141302:agent/*",
        "arn:aws:bedrock:us-east-1:152864141302:agent-alias/*/*"
      ]
    }
  ]
}
```

Or attach the AWS managed policy `AmazonBedrockFullAccess` for broader permissions.

---

## DynamoDB Registry Configuration

### Correct Entry Format

```json
{
  "agent_id": "PEGHWIVI5Y",
  "alias_id": "IO1GTTHP0M",
  "name": "CrowleyVesselOperationsAssistant",
  "description": "Handles vessel information, emergency procedures, and document cataloging for Crowley Maritime fleet.",
  "agent_type": "bedrock_agent",
  "account_id": "152864141302",
  "enabled": true,
  "capabilities": ["Vessel info", "Emergency procedures", "Document Cataloging"],
  "domains": ["Shipping", "vessels", "maritime"],
  "example_queries": ["What vessels do we have?", "Tell me about vessel Taino"]
}
```

### Key Fields

| Field | Purpose | Cross-Account? |
|-------|---------|----------------|
| `agent_id` | Bedrock Agent ID | Required |
| `alias_id` | Bedrock Agent Alias ID | Required |
| `agent_type` | `"bedrock_agent"` or `"agentcore"` | Required |
| `account_id` | Target AWS account ID | **Required for cross-account** |

The `account_id` field triggers the cross-account flow. If missing or matches local account, local invocation is used.

---

## Complete Code Implementation

### Configuration Constants

```python
import os
import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
LOCAL_ACCOUNT_ID = "843074507558"
CROSS_ACCOUNT_ROLE_ARN = "arn:aws:iam::152864141302:role/cmcCrossAccountBedrockInvokeRole"
CROSS_ACCOUNT_EXTERNAL_ID = "bedrock-cross-account-2024"

# Local clients
bedrock_client = boto3.client('bedrock-agent-runtime', region_name=REGION)
```

### Cross-Account Client Function

```python
def get_cross_account_bedrock_client(account_id: str):
    """Assume role using boto3.Session (isolated credentials)."""
    
    print(f"[Hub] Assuming role for {account_id}...")
    
    sts = boto3.client('sts', region_name=REGION)
    
    assumed = sts.assume_role(
        RoleArn=CROSS_ACCOUNT_ROLE_ARN,
        RoleSessionName=f"HubSession-{account_id}",
        ExternalId=CROSS_ACCOUNT_EXTERNAL_ID
    )
    
    creds = assumed['Credentials']
    
    # Create isolated session
    session = boto3.Session(
        aws_access_key_id=creds['AccessKeyId'],
        aws_secret_access_key=creds['SecretAccessKey'],
        aws_session_token=creds['SessionToken'],
        region_name=REGION
    )
    
    # Verify identity
    identity = session.client('sts').get_caller_identity()
    print(f"[Hub] ✅ Identity: Account={identity['Account']}")
    
    if identity['Account'] != account_id:
        raise Exception(f"Expected account {account_id}, got {identity['Account']}")
    
    return session.client('bedrock-agent-runtime')
```

### Agent Invocation Logic

```python
def invoke_bedrock_agent(agent: dict, user_request: str, session_id: str) -> str:
    """Invoke Bedrock Agent (local or cross-account)."""
    
    agent_id = agent.get('agent_id')
    alias_id = agent.get('alias_id')
    account_id = agent.get('account_id')
    
    if not alias_id:
        return f"Error: Missing alias_id for agent {agent_id}"
    
    # Choose client based on account
    if account_id and account_id != LOCAL_ACCOUNT_ID:
        print(f"[Hub] Using cross-account client for {account_id}")
        bedrock = get_cross_account_bedrock_client(account_id)
    else:
        print(f"[Hub] Using local client")
        bedrock = bedrock_client
    
    # Invoke agent
    response = bedrock.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=user_request
    )
    
    # Process streaming response
    result = ""
    for event in response.get("completion", []):
        if "chunk" in event and "bytes" in event["chunk"]:
            result += event["chunk"]["bytes"].decode("utf-8")
    
    return result if result else "No response from agent."
```

---

## Debugging Checklist

When cross-account invocation fails, check these in order:

| # | Check | How to Verify | Expected |
|---|-------|---------------|----------|
| 1 | STS AssumeRole permission | Check hub's IAM role | Has `sts:AssumeRole` |
| 2 | Trust policy | Check target role | Allows source account |
| 3 | External ID matches | Compare code vs policy | Exact match |
| 4 | Role assumption succeeds | Log output | No errors |
| 5 | Identity is target account | `get_caller_identity()` | Shows target account ID |
| 6 | Agent ID correct | Bedrock console | Matches exactly |
| 7 | Alias ID correct | Bedrock console | No typos, no extra chars |
| 8 | Using correct variable | Code review | `bedrock.invoke_agent` not `bedrock_client` |

### Log Messages to Look For

```
✅ Success Flow:
[Hub] Assuming role for 152864141302...
[Hub] ✅ Identity: Account=152864141302    ← Must show TARGET account
[Hub] Invoking agent PEGHWIVI5Y...
[Hub] ✅ Got response: ...

❌ Failure Indicators:
[Hub] ✅ Identity: Account=843074507558    ← Shows SOURCE account = credentials not applied
ResourceNotFoundException                   ← Agent doesn't exist in that account
ExpiredToken                               ← Cached credentials expired
ValidationException                        ← Bad agent_id or alias_id format
```

---

## Common Pitfalls

### 1. Credential Caching

```python
# ❌ WRONG: May use cached credentials
client = boto3.client('bedrock-agent-runtime', aws_access_key_id=...)

# ✅ CORRECT: Isolated session
session = boto3.Session(aws_access_key_id=...)
client = session.client('bedrock-agent-runtime')
```

### 2. Variable Naming

```python
# ❌ WRONG: Using global instead of local variable
bedrock = get_cross_account_bedrock_client(account_id)
response = bedrock_client.invoke_agent(...)  # Uses wrong client!

# ✅ CORRECT: Use the returned client
bedrock = get_cross_account_bedrock_client(account_id)
response = bedrock.invoke_agent(...)
```

### 3. Caching Expired Credentials

```python
# ❌ WRONG: Caching client with temp credentials
_clients = {}
if account_id in _clients:
    return _clients[account_id]  # May be expired!

# ✅ CORRECT: Fresh credentials each time (or implement expiry check)
def get_cross_account_bedrock_client(account_id):
    # Always assume role fresh
    assumed = sts.assume_role(...)
```

### 4. DynamoDB Data Quality

```python
# ❌ WRONG: Extra characters
"alias_id": "IO1GTTHP0M\""  # Trailing quote

# ✅ CORRECT: Clean data
"alias_id": "IO1GTTHP0M"
```

---

## Testing Script

Use this shell script to validate cross-account setup:

```bash
#!/bin/bash
echo "🔄 Testing cross-account Bedrock invocation..."

# Assume role
ROLE_OUTPUT=$(aws sts assume-role \
    --role-arn arn:aws:iam::152864141302:role/cmcCrossAccountBedrockInvokeRole \
    --role-session-name TestSession-$(date +%s) \
    --external-id "bedrock-cross-account-2024" \
    --output json)

if [ $? -eq 0 ]; then
    export AWS_ACCESS_KEY_ID=$(echo $ROLE_OUTPUT | jq -r '.Credentials.AccessKeyId')
    export AWS_SECRET_ACCESS_KEY=$(echo $ROLE_OUTPUT | jq -r '.Credentials.SecretAccessKey')
    export AWS_SESSION_TOKEN=$(echo $ROLE_OUTPUT | jq -r '.Credentials.SessionToken')
    
    echo "✅ Role assumed"
    echo "📍 Identity:"
    aws sts get-caller-identity
    
    # Test with Python
    python3 << 'EOF'
import boto3
client = boto3.client('bedrock-agent-runtime', region_name='us-east-1')
response = client.invoke_agent(
    agentId='PEGHWIVI5Y',
    agentAliasId='IO1GTTHP0M',
    sessionId='test-12345678901234567890',
    inputText='Hello'
)
result = ""
for event in response.get("completion", []):
    if "chunk" in event:
        result += event["chunk"]["bytes"].decode()
print(f"✅ Response: {result[:200]}...")
EOF
    
    # Cleanup
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
else
    echo "❌ Failed to assume role"
fi
```

---

## Key Takeaways

1. **Always use `boto3.Session`** for cross-account operations - it guarantees credential isolation

2. **Verify identity** after assuming role to confirm credentials are applied correctly

3. **Don't cache clients** with temporary credentials without implementing expiry checks

4. **Validate DynamoDB data** carefully - one stray character breaks everything

5. **Variable naming matters** - easy to accidentally use the wrong client variable

6. **Test with shell script first** - isolates AWS configuration issues from code issues

7. **Check CloudWatch logs** - they reveal the actual error vs. generic error messages

---

## References

- [AWS STS AssumeRole Documentation](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)
- [boto3 Session Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html)
- [Amazon Bedrock Agents Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agents.html)
- [Cross-Account IAM Roles](https://docs.aws.amazon.com/IAM/latest/UserGuide/tutorial_cross-account-with-roles.html)

---

*Document created: December 2025*
*Harbor AI Hub - Crowley Maritime Corporation*
