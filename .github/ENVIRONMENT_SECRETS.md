# GitHub Environment Secrets Configuration

This document lists all required secrets for deploying the Crowley Agentic Platform.

## Environment Setup

Create the following GitHub environments in your repository settings:
- `dev`
- `qa`
- `prod`

Each environment needs the secrets listed below.

---

## Required Secrets (All Environments)

### AWS Credentials

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS IAM access key ID | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM secret access key | `wJalr...` |
| `AWS_REGION` | AWS region for deployment | `us-east-1` |

> **Security Note**: Consider migrating to OIDC authentication in the future to avoid long-lived credentials.

---

## Document Processor Agent Secrets

| Secret Name | Description | Example |
|-------------|-------------|---------|
| `DOCUMENT_PROCESSOR_ECR_REPOSITORY` | ECR repository name (not full URI) | `bedrock-agentcore-document_processor` |
| `DOCUMENT_PROCESSOR_AGENT_ID` | Bedrock AgentCore runtime ID | `document_processor-dvU2Bk3C7c` |
| `DOCUMENT_PROCESSOR_MEMORY_ID` | AgentCore memory ID (optional) | `document_processor_mem-C2y8M7BwiW` |

---

## How to Set Up Secrets

### Via GitHub UI

1. Go to repository **Settings** > **Secrets and variables** > **Actions**
2. Click **New repository secret** or select an environment
3. Add each secret with its value

### Via GitHub CLI

```bash
# Set repository-level secret
gh secret set AWS_ACCESS_KEY_ID --body "AKIA..."

# Set environment-specific secret
gh secret set DOCUMENT_PROCESSOR_AGENT_ID --env dev --body "document_processor-abc123"
gh secret set DOCUMENT_PROCESSOR_AGENT_ID --env qa --body "document_processor-xyz789"
```

---

## Environment-Specific Values

Each environment will have different values for agent-specific secrets:

| Environment | Agent ID Pattern | Notes |
|-------------|------------------|-------|
| `dev` | `document_processor-<dev-id>` | Development/testing |
| `qa` | `document_processor-<qa-id>` | QA/staging |
| `prod` | `document_processor-<prod-id>` | Production |

The AWS credentials may be the same across environments (single AWS account) or different (multi-account setup).

---

## Adding a New Agent

When adding a new agent (e.g., `hub_orchestrator`), add these secrets:

| Secret Name | Description |
|-------------|-------------|
| `<AGENT_NAME>_ECR_REPOSITORY` | ECR repository for the agent |
| `<AGENT_NAME>_AGENT_ID` | Bedrock AgentCore runtime ID |
| `<AGENT_NAME>_MEMORY_ID` | AgentCore memory ID (if using memory) |

Then create a new workflow file (e.g., `deploy-hub-orchestrator.yaml`) that calls the reusable template.

---

## Verifying Secrets Are Set

Run this command to list configured secrets:

```bash
gh secret list --env dev
gh secret list --env qa
```
