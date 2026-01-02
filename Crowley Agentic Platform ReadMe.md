Crowley Agentic Platform
========================

Components:-

1. Agents
    1.1 Document Processor - Strands Agent
    1.2 Hub Orchestrator - Strands Agent
    1.3 Vessel Operations Assistant - Bedrock Agent

2. Registry - DynamoDB table for registy
3. Lambdas - Proxy for Document Processor, Proxy for Hub Orchestrator
4. Lambdas - Action groups for Bedrock agent
5. API Gateway - API for Document Processor, API for Hub Orchestrator
6. Frontend App
    5.1 package.json
    5.2 tsconfig.json
    5.3 app/layout.tsx
    5.4 app/page.tsx
    5.5 app/globals.css
    5.6 app/api/upload-url/route.ts


    Github Repo Structure:
    ======================

    harbor-ai-platform/
├── README.md
├── .gitignore
│
├── infrastructure/
│   ├── README.md
│   ├── dynamodb/
│   │   ├── registry-table.yaml      # CloudFormation or
|   │   └── registry-data.json       # Initial data for the table
│   ├── api-gateway/
│   │   ├── doc-processor-api.yaml
│   │   └── hub-orchestrator-api.yaml
│   ├── iam/
│   │   ├── doc-processor-role.yaml
│   │   ├── hub-orchestrator-role.yaml
│   │   └── proxy-lambda-roles.yaml
│   └── s3/
│       └── doc-processor-bucket.yaml
│
├── agents/
│   ├── document-processor/
│   │   ├── README.md
│   │   ├── requirements.txt
│   │   ├── agent.py
│   │   └── Dockerfile
│   ├── hub-orchestrator/
│   │   ├── README.md
│   │   ├── requirements.txt
│   │   ├── agent.py
│   │   └── Dockerfile
│   └── vessel-operations/
│       ├── README.md
│       └── agent-config.json        # Bedrock agent export
│
├── lambdas/
│   ├── doc-processor-proxy/
│   │   ├── index.py
│   │   └── requirements.txt
│   ├── hub-orchestrator-proxy/
│   │   ├── index.py
│   │   └── requirements.txt
│   └── vessel-ops-action-groups/
│       ├── handler.py
│       └── requirements.txt
│
├── frontend/
│   ├── README.md
│   ├── package.json
│   ├── tsconfig.json
│   ├── next.config.js
│   ├── .env.example
│   └── src/
│       └── app/
│           ├── layout.tsx
│           ├── page.tsx
│           ├── globals.css
│           └── api/
│               └── upload-url/
│                   └── route.ts
│
├── scripts/
│   ├── deploy-dynamodb.sh
│   ├── deploy-lambdas.sh
│   └── deploy-frontend.sh
│
└── docs/
    ├── architecture.md
    ├── setup-guide.md
    └── runbook.md


Steps to migrate DynamoDB table to new account
==============================================

1. Go to IAC Generator, and generate cloudformation template for the dynamoDB table in the current account. This exports only the structure of the table without data. Download the YAML file and place in your local folder
2. Also login to AWS account from Powershell or terminal simultaneously and run the below command to export contents in JSON format.
   ```bash
   # Export scan output
    aws dynamodb scan --table-name cmc-harbor-ai-agent-registry --profile crowley-corp-qa --output json > scan-output.json

# Transform to batch-write format (PowerShell script)
    $scan = Get-Content scan-output.json | ConvertFrom-Json
    $batchFormat = @{
        "cmc-harbor-ai-agent-registry" = $scan.Items | ForEach-Object {
            @{ "PutRequest" = @{ "Item" = $_ } }
            }
        }
$batchFormat | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 registry-data.json
   ```
3. Save the table data in the same folder. Open in notedpad ++ and change encoding to UTF-8
    To validate the data in the json file, you can run -> Get-Content infrastructure/dynamodb/registry-data.json
4. As a next step now lets create the github repo and start pushing the infrastructure code.
    in powershell, cd C:\Users\mohanaj\git\ent-agentic-platform
    git init
    git remote add origin https://github.com/YOUR_USERNAME/ent-agentic-platform.git
    New-Item .gitignore
    ```   
    Add this content to `.gitignore`:
    ```
    .env
    .env.local
    node_modules/
    .next/
    *.log

    git add .
    git status
    git commit -m "Initial commit: DynamoDB infrastructure"
    git branch -M main
    git push -u origin main


Thats it. Your structure will appear in GitHub as:
```
ent-agentic-platform/
├── .gitignore
└── infrastructure/
    └── dynamodb/
        ├── cmc-harbor-ai-agent-registry-template-1767380953610.yaml
        └── registry-data.json


5. Now lets bring this data into the target AWS Account. We will do this using a Github CICD based approach

    - aws configure sso #Enter all values and configure a profile for target environment if logging in for the first time
    - aws sts get-caller-identity #Confirm that you are in target environment

    Create a json file called github-deployer-policy and save it in repo root folder in local. Paste below - 
    {
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "dynamodb:*",
        "iam:PassRole"
      ],
      "Resource": "*"
    }
  ]
}

    - aws iam create-user --user-name github-actions-deployer  #Create a new user in AWS target environment and keep note of this username for future permissions. YOu can create it in multiple environments if doing this for the first time. Following this we can setup multiple environments in Github Repo to enable manual environment based trigger from Github to each environment.
    - aws iam put-user-policy `
          --user-name github-actions-deployer `
          --policy-name DeploymentPolicy `
          --policy-document file://github-deployer-policy.json `
          --profile crowley-corp-qa

    - aws iam create-access-key --user-name github-actions-deployer --profile crowley-corp-qa  #Generate the key and secret and save it for later use

    As next steps,

        Go to your repo: https://github.com/YOUR_USERNAME/ent-agentic-platform
        Settings → Environments → New Environment
        Create 2 environments - qa and dev
        Add these two secrets and region one by one per environment:

            AWS_ACCESS_KEY_ID: <value>
            AWS_SECRET_ACCESS_KEY: <value>
            AWS_REGION: us-east-1

 6. Create Github Actions Workflow

    In your powershell, while being in     cd C:\Users\mohanaj\git\ent-agentic-platform
        mkdir -p .github/workflows
        New-Item .github/workflows/deploy-dynamodb.yaml

        paste into the yaml document:

        name: Deploy DynamoDB

name: Deploy DynamoDB

on:
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        required: true
        type: choice
        options:
          - dev
          - qa

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: ${{ github.event.inputs.environment }}
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION }}

      - name: Deploy CloudFormation stack
        run: |
          aws cloudformation deploy \
            --template-file infrastructure/dynamodb/cmc-harbor-ai-agent-registry-template-1767380953610.yaml \
            --stack-name harbor-ai-agent-registry \
            --no-fail-on-empty-changeset

      - name: Load seed data
        run: |
          aws dynamodb batch-write-item \
            --request-items file://infrastructure/dynamodb/registry-data.json


    7. Next type Git Status in powershell to see current branch and status of pending files. Make sure to put files you don't want to synch in .gitignore
        git add .
        git commit -m "Update workflow to manual dispath with dev/qa environments"
        git push

        Go to Actions tab in the repo
        Select Deploy DynamoDB
        Click Run workflow
        Select dev from dropdown
        Click "Run Workflow" button

**=======================================================This concludes DynamoDB deployment via CICD==============================================================**

**========================================================Document Processor Agent Migration=======================================================================**


