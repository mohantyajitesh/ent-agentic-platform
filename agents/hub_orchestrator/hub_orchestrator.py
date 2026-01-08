#!/usr/bin/env python3
"""
Harbor AI Hub - Dynamic Agent Orchestrator
Routes requests to spoke agents using LLM-based routing with DynamoDB agent registry
Supports multi-turn conversations via AgentCore Memory
"""

import os
import json
import uuid
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
import boto3

# Memory imports (wrapped in try/except for graceful fallback)
try:
    from bedrock_agentcore.memory.integrations.strands.config import (
        AgentCoreMemoryConfig, RetrievalConfig
    )
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager
    )
    MEMORY_IMPORTS_AVAILABLE = True
except ImportError:
    MEMORY_IMPORTS_AVAILABLE = False

# ============================================
# Configuration
# ============================================
REGION = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID")
#MEMORY_ID = None  # Disable memory by default for testing
AGENT_REGISTRY_TABLE = os.environ.get("AGENT_REGISTRY_TABLE", "cmc-harbor-ai-agent-registry")

# AWS Clients
bedrock_client = boto3.client('bedrock-agent-runtime', region_name=REGION)
agentcore_client = boto3.client('bedrock-agentcore', region_name=REGION)
dynamodb = boto3.resource('dynamodb', region_name=REGION)

# Cross-account configuration
LOCAL_ACCOUNT_ID = "843074507558"
CROSS_ACCOUNT_ROLE_ARN = "arn:aws:iam::152864141302:role/cmcCrossAccountBedrockInvokeRole"
CROSS_ACCOUNT_EXTERNAL_ID = "bedrock-cross-account-2024"  # Remove if not using external ID

# ============================================
# Cross-Account Support
# ============================================

def get_cross_account_bedrock_client(account_id: str):
    """Assume role using boto3.Session."""
    
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
    
    # Verify using session's STS
    identity = session.client('sts').get_caller_identity()
    print(f"[Hub] ✅ Identity: Account={identity['Account']}")
    
    if identity['Account'] != account_id:
        raise Exception(f"Expected account {account_id}, got {identity['Account']}")
    
    # Return client from session
    return session.client('bedrock-agent-runtime')


# ============================================
# Agent Registry (DynamoDB)
# ============================================
def load_agent_registry():
    """Load enabled agents from DynamoDB."""
    try:
        table = dynamodb.Table(AGENT_REGISTRY_TABLE)
        response = table.scan()
        agents = [a for a in response.get('Items', []) if a.get('enabled', True)]
        print(f"[Hub] Loaded {len(agents)} agents from registry")
        return agents
    except Exception as e:
        print(f"[Hub] Error loading registry: {e}")
        return []


def format_agent_catalog(agents: list) -> str:
    """Format agent list for LLM system prompt."""
    if not agents:
        return "No agents available."
    
    catalog = []
    for i, agent in enumerate(agents, 1):
        caps = ', '.join(agent.get('capabilities', []))
        domains = ', '.join(agent.get('domains', []))
        examples = ', '.join(agent.get('example_queries', [])[:3])
        
        catalog.append(f"""Agent {i}: {agent.get('name', 'Unknown')}
  ID: {agent.get('agent_id', 'N/A')}
  Description: {agent.get('description', 'No description')}
  Capabilities: {caps}
  Domains: {domains}
  Examples: {examples}""")
    
    return "\n\n".join(catalog)


# Load registry at startup
AGENT_REGISTRY = load_agent_registry()

# ============================================
# Tools
# ============================================
@tool
def invoke_spoke_agent(agent_id: str, user_request: str, session_id: str) -> str:
    """
    Invoke a spoke agent by ID from the registry.
    
    Args:
        agent_id: The agent ID from the catalog (e.g., "PEGHWIVI5Y")
        user_request: The user's request to forward
        session_id: Session ID for conversation continuity
    
    Returns:
        The spoke agent's response
    """
    # Look up agent in registry
    agent = next((a for a in AGENT_REGISTRY if a['agent_id'] == agent_id), None)
    
    if not agent:
        return f"Agent '{agent_id}' not found. Available: {[a['agent_id'] for a in AGENT_REGISTRY]}"
    
    agent_type = agent.get('agent_type', 'bedrock')  # Default to bedrock for backward compatibility
    print(f"[Hub] Routing to {agent['name']} ({agent_type}): {user_request[:80]}...")
    
    try:
        if agent_type == 'agentcore':
            # Invoke AgentCore agent via SDK
            agent_runtime_arn = agent.get('agent_runtime_arn')
            if not agent_runtime_arn:
                return f"Error: AgentCore agent '{agent_id}' missing agent_runtime_arn"
            
            # Prepare payload as JSON
            payload = json.dumps({
                "prompt": user_request,
                "session_id": session_id,
                "actor_id": agent.get('actor_id', 'hub-agent')
            }).encode('utf-8')
            
            response = agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=agent_runtime_arn,
                contentType='application/json',
                accept='application/json',
                runtimeSessionId=session_id,
                payload=payload
            )
            
            # Read streaming response
            response_body = response['response'].read().decode('utf-8')
            try:
                result = json.loads(response_body)

                # Handle double-encoded JSON string
                if isinstance(result, str):
                    result = json.loads(result)

                # Handle nested response structure
                if isinstance(result, dict):
                    return json.dumps(result, indent=2)
                else:
                    return str(result)
            
            except json.JSONDecodeError:
                return response_body
        
        else:
            # Invoke Bedrock Agent via SDK
            alias_id = agent.get('alias_id')
            if not alias_id:
                return f"Error: Bedrock agent '{agent_id}' missing alias_id"
            
            # Choose client based on account (local or cross-account)
            account_id = agent.get('account_id')
            print(f"[Hub DEBUG] account_id={account_id}, type={type(account_id)}, LOCAL={LOCAL_ACCOUNT_ID}")
    
            if account_id and account_id != LOCAL_ACCOUNT_ID:
                bedrock = get_cross_account_bedrock_client(account_id)
                print(f"account_id={account_id}, type={type(account_id)}, LOCAL={LOCAL_ACCOUNT_ID}")
    
            else:
                bedrock = bedrock_client
            
            response = bedrock.invoke_agent(
                agentId=agent_id,
                agentAliasId=alias_id,
                sessionId=session_id,
                inputText=user_request
            )
            
            result = ""
            for event in response.get("completion", []):
                if "chunk" in event and "bytes" in event["chunk"]:
                    result += event["chunk"]["bytes"].decode("utf-8")
            
            return result if result else "No response from Bedrock agent."
    
    except Exception as e:
        # ADD THIS - More detailed error logging
        print(f"[Hub] ERROR invoking {agent_id}: {type(e).__name__}: {e}")
        import traceback
        print(f"[Hub] Traceback: {traceback.format_exc()}")
        return f"Error: {str(e)}"


@tool
def list_available_agents() -> str:
    """List all available agents and their capabilities."""
    if not AGENT_REGISTRY:
        return "No agents registered."
    
    lines = ["Available assistants:\n"]
    for agent in AGENT_REGISTRY:
        name = agent.get('name', 'Unknown')
        description = agent.get('description', 'No description')[:100]
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)


@tool
def refresh_agent_registry() -> str:
    """Reload agent registry from DynamoDB."""
    global AGENT_REGISTRY
    AGENT_REGISTRY = load_agent_registry()
    return f"Registry refreshed. {len(AGENT_REGISTRY)} agents loaded."

# ============================================
# System Prompt Builder
# ============================================
def build_system_prompt(agents: list) -> str:
    """Build system prompt with current agent catalog."""
    catalog = format_agent_catalog(agents)
    
    return f"""You are Harbor AI Hub, the intelligent request router for Crowley Maritime.

## Available Agents

{catalog}

## Instructions

1. Analyze the user's request
2. Select the best matching agent based on description, capabilities, and domains
3. Use invoke_spoke_agent with the agent_id, user's full request, and session_id
4. Present the response clearly

**IMPORTANT:** When a spoke agent returns usage metrics (tokens, document size, etc.), 
always include them at the end of your response in this format:

---
**📊 Usage Metrics:**
- Document: [size] KB, [pages] pages
- Tokens: [input] input, [output] output
- Textract pages: [count]

If no agent matches, use list_available_agents to show options.
If asked what you can do, use list_available_agents.

Remember context from earlier in the conversation."""

# ============================================
# Agent Factory (with Memory Support)
# ============================================
def create_agent(session_id: str, actor_id: str) -> Agent:
    """Create Hub Agent with optional memory."""
    session_manager = None
    
    # Configure memory if available
    if MEMORY_ID and MEMORY_IMPORTS_AVAILABLE:
        session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=AgentCoreMemoryConfig(
                memory_id=MEMORY_ID,
                session_id=session_id,
                actor_id=actor_id
            ),
            region_name=REGION
        )
        print(f"[Hub] Memory ENABLED - Session: {session_id}")
    else:
        print(f"[Hub] Memory DISABLED - Stateless mode")
    
    return Agent(
        system_prompt=build_system_prompt(AGENT_REGISTRY),
        tools=[invoke_spoke_agent, list_available_agents, refresh_agent_registry],
        session_manager=session_manager
    )

# ============================================
# AgentCore Runtime
# ============================================
app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload: dict) -> dict:
    """Main entry point for AgentCore Runtime."""
    prompt = payload.get("prompt", "")
    session_id = payload.get("session_id") or f"session-{uuid.uuid4().hex[:20]}"
    actor_id = payload.get("actor_id", "default-user")
    
    if not prompt:
        return {"response": "No prompt provided", "status": "error"}
    
    # Pad session_id to 33 chars (AgentCore requirement)
    print(f"Padding to session_id: {session_id}")
    if len(session_id) < 33:
        session_id = session_id + "-" + "0" * (33 - len(session_id) - 1)
    
    # Create agent per-request (with memory and dynamic catalog)
    print(f"Creating.. agentcore agent for session: {session_id}, actor: {actor_id}")
    hub_agent = create_agent(session_id, actor_id)
    print(f"Created.. agentcore agent for session: {session_id}, actor: {actor_id}")

    response = hub_agent(f"{prompt}\n\n[session_id: {session_id}]")
    
    return {
        "response": str(response),
        "session_id": session_id,
        "actor_id": actor_id,
        "memory_enabled": MEMORY_ID is not None,
        "agents_loaded": len(AGENT_REGISTRY),
        "status": "success"
    }

# ============================================
# Start the server (required for AgentCore)
# ============================================
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # CLI mode - process argument directly
        prompt = " ".join(sys.argv[1:])
        print(f"[CLI] Testing with prompt: {prompt}")
        
        # Generate unique session ID for CLI
        session_id = f"cli-{uuid.uuid4().hex[:24]}"
        print(f"[CLI] Generated session_id: {session_id}")
        
        result = invoke({"prompt": prompt, "session_id": session_id})
        print(f"[CLI] Response: {result}")
    else:
        # Server mode
        app.run()