#12/10/2025
"""
Document Processor Agent V2
===========================
A unified agent for extracting structured data from any document format,
with async Textract processing, signature detection, and confidence scoring.
 
Supported formats: PDF, Images, DOCX, XLSX, PPTX, EML, HTML, XML, JSON, TXT
Deployment: AWS Bedrock AgentCore Runtime
"""
 
import os
import uuid
import json
import base64
import time
import tempfile
from pathlib import Path
from typing import Optional, Literal, Dict, Any
from datetime import datetime
 
import boto3
from botocore.exceptions import ClientError
from strands import Agent, tool

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

# Conditional AgentCore import so CLI works standalone during local testing
try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
    AGENTCORE_AVAILABLE = True
except ImportError:
    AGENTCORE_AVAILABLE = False
 
#load_dotenv()

# Setup boto3 default session from environment BEFORE importing strands
# This ensures Strands SDK inherits the correct credentials


 
# =============================================================================
# CONFIGURATION
# =============================================================================

TEXTRACT_TIMEOUT_MINUTES = int(os.environ.get("TEXTRACT_TIMEOUT_MINUTES", "10"))
SIGNATURE_CONFIDENCE_THRESHOLD = float(os.environ.get("SIGNATURE_CONFIDENCE_THRESHOLD", "0.85"))
MAX_VISION_FILE_SIZE_MB = int(os.environ.get("MAX_VISION_FILE_SIZE_MB", "20"))
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4096"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
CLAUDE_MODEL_ID = os.environ.get("CLAUDE_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
IMAGE_MEDIA_TYPE = os.environ.get("IMAGE_MEDIA_TYPE", "image/jpeg")
DOC_TEXT_LIMIT = int(os.environ.get("DOC_TEXT_LIMIT", "50000"))
EXTRACTION_PROMPT_LIMIT = int(os.environ.get("EXTRACTION_PROMPT_LIMIT", "500"))
VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "8192"))
#MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID")
MEMORY_ID="document_processor_mem-C2y8M7BwiW"

# =============================================================================
# USAGE TRACKING
# =============================================================================

def _create_empty_usage() -> Dict[str, Any]:
    """Create empty usage tracking dict."""
    return {
        "document": {
            "size_bytes": 0,
            "size_kb": 0.0,
            "size_mb": 0.0,
            "pages": 0
        },
        "tokens": {
            "total_input": 0,
            "total_output": 0,
            "breakdown": {
                "vision": {"input": 0, "output": 0},
                "structured_extraction": {"input": 0, "output": 0}
            }
        },
        "textract_pages": 0
    }


# Global usage tracker (reset per request)
_usage: Dict[str, Any] = _create_empty_usage()


def reset_usage():
    """Reset usage tracking for new request."""
    global _usage
    _usage = _create_empty_usage()


def set_document_size(size_bytes: int):
    """Set document size in all units."""
    global _usage
    _usage["document"]["size_bytes"] = size_bytes
    _usage["document"]["size_kb"] = round(size_bytes / 1024, 2)
    _usage["document"]["size_mb"] = round(size_bytes / (1024 * 1024), 4)


def set_document_pages(pages: int):
    """Set document page count."""
    global _usage
    _usage["document"]["pages"] = pages
    _usage["textract_pages"] = pages


def add_tokens(component: str, input_tokens: int, output_tokens: int):
    """
    Add tokens for a specific component.
    
    Args:
        component: One of "vision", "structured_extraction"
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    """
    global _usage
    if component in _usage["tokens"]["breakdown"]:
        _usage["tokens"]["breakdown"][component]["input"] += input_tokens
        _usage["tokens"]["breakdown"][component]["output"] += output_tokens
        _usage["tokens"]["total_input"] += input_tokens
        _usage["tokens"]["total_output"] += output_tokens


def get_usage() -> Dict[str, Any]:
    """Get current usage with computed totals."""
    global _usage
    _usage["tokens"]["total"] = _usage["tokens"]["total_input"] + _usage["tokens"]["total_output"]
    return _usage.copy()


# =============================================================================
# AWS CLIENTS (Lazy Initialization)
# =============================================================================
 
_clients = {}
 
def get_client(service: str):
    """Get or create a boto3 client."""
    if service not in _clients:
        _clients[service] = boto3.client(
            service,
            region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
    return _clients[service]


# =============================================================================
# TEXTRACT HELPERS
# =============================================================================
 
def extract_key_value_pairs(blocks: list, block_map: dict) -> list:
    """Extract key-value pairs from Textract blocks."""
    kvs = []
    for block in blocks:
        if block['BlockType'] == 'KEY_VALUE_SET' and 'KEY' in block.get('EntityTypes', []):
            key = ''
            value = ''
            key_confidence = block.get('Confidence', 0)
            value_confidence = 0
 
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for cid in rel['Ids']:
                        child = block_map.get(cid, {})
                        if child.get('BlockType') == 'WORD':
                            key += child.get('Text', '') + ' '
 
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'VALUE':
                    for vid in rel['Ids']:
                        value_block = block_map.get(vid, {})
                        value_confidence = value_block.get('Confidence', 0)
                        for vrel in value_block.get('Relationships', []):
                            if vrel['Type'] == 'CHILD':
                                for vcid in vrel['Ids']:
                                    vchild = block_map.get(vcid, {})
                                    if vchild.get('BlockType') == 'WORD':
                                        value += vchild.get('Text', '') + ' '
 
            if key.strip():
                kvs.append({
                    'key': key.strip(),
                    'value': value.strip(),
                    'confidence': round((key_confidence + value_confidence) / 2 / 100, 3)
                })
    return kvs
 
 
def extract_tables(blocks: list, block_map: dict) -> list:
    """Extract tables from Textract blocks."""
    tables = []
    for block in blocks:
        if block['BlockType'] == 'TABLE':
            table = {'page': block.get('Page', 1), 'rows': []}
            cells = []
 
            for rel in block.get('Relationships', []):
                if rel['Type'] == 'CHILD':
                    for cid in rel['Ids']:
                        cell = block_map.get(cid, {})
                        if cell.get('BlockType') == 'CELL':
                            text = ''
                            for crel in cell.get('Relationships', []):
                                if crel['Type'] == 'CHILD':
                                    for wcid in crel['Ids']:
                                        word = block_map.get(wcid, {})
                                        if word.get('BlockType') == 'WORD':
                                            text += word.get('Text', '') + ' '
                            cells.append({
                                'row': cell.get('RowIndex', 1),
                                'col': cell.get('ColumnIndex', 1),
                                'text': text.strip()
                            })
 
            if cells:
                max_row = max(c['row'] for c in cells)
                for row_idx in range(1, max_row + 1):
                    row_cells = sorted([c for c in cells if c['row'] == row_idx], key=lambda x: x['col'])
                    table['rows'].append([c['text'] for c in row_cells])
                tables.append(table)
    return tables
 
 
def extract_signatures(blocks: list, confidence_threshold: float = SIGNATURE_CONFIDENCE_THRESHOLD) -> dict:
    """Extract signatures from Textract blocks with confidence scoring."""
    signatures = []
    human_review_items = []
 
    for block in blocks:
        if block.get('BlockType') == 'SIGNATURE':
            confidence = block.get('Confidence', 0) / 100
            bbox = block.get('Geometry', {}).get('BoundingBox', {})
 
            status = 'valid' if confidence >= confidence_threshold else 'needs_review'
            if confidence < 0.5:
                status = 'invalid'
 
            sig = {
                'id': block.get('Id'),
                'page': block.get('Page', 1),
                'confidence': round(confidence, 3),
                'location': {
                    'left': round(bbox.get('Left', 0), 4),
                    'top': round(bbox.get('Top', 0), 4),
                    'width': round(bbox.get('Width', 0), 4),
                    'height': round(bbox.get('Height', 0), 4)
                },
                'status': status
            }
            signatures.append(sig)
 
            if status == 'needs_review':
                human_review_items.append({
                    'type': 'signature',
                    'id': sig['id'],
                    'page': sig['page'],
                    'confidence': sig['confidence'],
                    'reason': f"Confidence {confidence*100:.0f}% below {confidence_threshold*100:.0f}% threshold"
                })
 
    return {
        'signatures': signatures,
        'count': len(signatures),
        'valid_count': sum(1 for s in signatures if s['status'] == 'valid'),
        'human_review_items': human_review_items
    }
 
 
# =============================================================================
# TEXTRACT ASYNC TOOL (Core Processing)
# =============================================================================
 
@tool
def textract_async(
    s3_bucket: str,
    s3_key: str,
    output_key: str = None,
    confidence_threshold: float = SIGNATURE_CONFIDENCE_THRESHOLD
) -> str:
    """
    Extract text, forms, tables, and signatures from a document using Textract async API.
    Best for large/multi-page scanned PDFs.
 
    Args:
        s3_bucket: S3 bucket containing the document
        s3_key: S3 object key for the document
        output_key: S3 key for output JSON (auto-generated if not provided)
        confidence_threshold: Minimum confidence for valid signatures (0.0-1.0)
 
    Returns:
        JSON summary with extraction results and S3 output location
    """
    try:
        textract = get_client('textract')
        s3 = get_client('s3')

        # Get document size
        try:
            head = s3.head_object(Bucket=s3_bucket, Key=s3_key)
            set_document_size(head['ContentLength'])
        except Exception:
            pass
 
        # Start async analysis with all features
        response = textract.start_document_analysis(
            DocumentLocation={'S3Object': {'Bucket': s3_bucket, 'Name': s3_key}},
            FeatureTypes=['FORMS', 'TABLES', 'LAYOUT', 'SIGNATURES']
        )
        job_id = response['JobId']
 
        # Poll for completion with timeout
        timeout = time.time() + (TEXTRACT_TIMEOUT_MINUTES * 60)
        while time.time() < timeout:
            result = textract.get_document_analysis(JobId=job_id)
            status = result['JobStatus']
            if status in ['SUCCEEDED', 'FAILED']:
                break
            time.sleep(2)
        else:
            return json.dumps({'error': f'Textract job timeout after {TEXTRACT_TIMEOUT_MINUTES} minutes'})
 
        if status == 'FAILED':
            return json.dumps({'error': f"Textract job failed for {s3_key}"})
 
        # Collect all blocks (handle pagination)
        blocks = result.get('Blocks', [])
        next_token = result.get('NextToken')
        while next_token:
            result = textract.get_document_analysis(JobId=job_id, NextToken=next_token)
            blocks.extend(result.get('Blocks', []))
            next_token = result.get('NextToken')
 
        block_map = {b['Id']: b for b in blocks}
 
        # Track pages
        pages = len(set(b.get('Page', 1) for b in blocks))
        set_document_pages(pages)

        # Extract all components
        key_values = extract_key_value_pairs(blocks, block_map)
        tables = extract_tables(blocks, block_map)
        sig_result = extract_signatures(blocks, confidence_threshold)
 
        # Build output
        output = {
            'document': {
                'source': f"s3://{s3_bucket}/{s3_key}",
                'pages': pages,
                'processed_at': datetime.utcnow().isoformat() + 'Z'
            },
            'key_values': key_values,
            'tables': tables,
            'signatures': sig_result['signatures'],
            'summary': {
                'key_value_count': len(key_values),
                'table_count': len(tables),
                'signature_count': sig_result['count'],
                'valid_signatures': sig_result['valid_count']
            },
            'human_review': {
                'required': len(sig_result['human_review_items']) > 0,
                'items': sig_result['human_review_items']
            }
        }
 
        # Save to S3
        if not output_key:
            base_name = os.path.splitext(s3_key.split('/')[-1])[0]
            output_key = f"textract_output/{base_name}_extracted.json"
 
        s3.put_object(Bucket=s3_bucket, Key=output_key, Body=json.dumps(output, indent=2))
 
        return json.dumps({
            'status': 'success',
            'output_location': f"s3://{s3_bucket}/{output_key}",
            'summary': output['summary'],
            'human_review_required': output['human_review']['required'],
            'human_review_items': output['human_review']['items']
        }, indent=2)
 
    except Exception as e:
        return json.dumps({'error': str(e)})
 
 
# =============================================================================
# DOCUMENT LOADING METHODS
# =============================================================================
 
@tool
def load_document(
    file_path: str,
    method: Literal["unstructured", "docling", "textract", "vision"] = "unstructured"
) -> str:
    """
    Universal document loader. Extracts text/content from any supported format.
 
    Args:
        file_path: Path to document (local path or s3://bucket/key)
        method: Extraction method
            - "unstructured": Best for most documents (PDF, DOCX, XLSX, images, email)
            - "docling": Best for complex layouts with tables
            - "textract": Best for forms with key-value pairs (sync, for small docs)
            - "vision": Best for scanned docs, handwriting, complex visuals (uses Claude)
 
    Returns:
        Extracted text as markdown-formatted string
    """
    temp_file_path = None
    try:
        if file_path.startswith("s3://"):
            parts = file_path.replace("s3://", "").split("/", 1)
            bucket, key = parts[0], parts[1]
            suffix = Path(key).suffix
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            temp_file_path = temp_file.name
            temp_file.close()
            
            # Get size before download
            try:
                s3 = get_client('s3')
                head = s3.head_object(Bucket=bucket, Key=key)
                set_document_size(head['ContentLength'])
            except Exception:
                pass
            
            get_client('s3').download_file(bucket, key, temp_file_path)
            file_path = temp_file_path
 
        file_path = Path(file_path)
        if not file_path.exists():
            return f"Error: File not found: {file_path}"

        # Get size for local files
        try:
            set_document_size(file_path.stat().st_size)
        except Exception:
            pass
 
        if method == "unstructured":
            result = _load_unstructured(file_path)
        elif method == "docling":
            result = _load_docling(file_path)
        elif method == "textract":
            result = _load_textract_sync(file_path)
        elif method == "vision":
            result = _load_vision(file_path)
        else:
            result = f"Error: Unknown method: {method}"
       
        return result
       
    except ClientError as e:
        return f"Error downloading from S3: {e}"
    finally:
        if temp_file_path:
            try:
                os.unlink(temp_file_path)
            except:
                pass
 
 
def _load_unstructured(file_path: Path) -> str:
    """Load using unstructured library."""
    try:
        from unstructured.partition.auto import partition
        elements = partition(filename=str(file_path))
        parts = []
        for el in elements:
            el_type = type(el).__name__
            text = str(el)
            if el_type == "Title":
                parts.append(f"## {text}\n")
            elif el_type == "Table":
                parts.append(f"\n{text}\n")
            elif el_type == "ListItem":
                parts.append(f"- {text}")
            else:
                parts.append(text)
        return "\n".join(parts)
    except ImportError:
        return "Error: 'unstructured' not installed. Run: pip install unstructured[all-docs]"
    except Exception as e:
        return f"Error: {str(e)}"
 
 
def _load_docling(file_path: Path) -> str:
    """Load using IBM docling library."""
    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        return result.document.export_to_markdown()
    except ImportError:
        return "Error: 'docling' not installed. Run: pip install docling"
    except Exception as e:
        return f"Error: {str(e)}"
 
 
def _load_textract_sync(file_path: Path) -> str:
    """Load using Textract sync API (small docs only)."""
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
 
        response = get_client('textract').analyze_document(
            Document={'Bytes': file_bytes},
            FeatureTypes=['FORMS', 'TABLES', 'LAYOUT', 'SIGNATURES']
        )

        blocks = response.get('Blocks', [])
        pages = len(set(b.get('Page', 1) for b in blocks))
        set_document_pages(pages)
 
        lines = [b.get('Text', '') for b in blocks if b['BlockType'] == 'LINE']
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"
 
 
def _load_vision(file_path: Path) -> str:
    """Load using Claude vision capability."""
    try:
        from pdf2image import convert_from_path
        import io
 
        max_size = MAX_VISION_FILE_SIZE_MB * 1024 * 1024
        if file_path.stat().st_size > max_size:
            return f"Error: File too large ({file_path.stat().st_size // 1024 // 1024}MB > {MAX_VISION_FILE_SIZE_MB}MB)"
 
        suffix = file_path.suffix.lower()
        images_b64 = []
 
        if suffix == ".pdf":
            images = convert_from_path(str(file_path), dpi=150)
            for img in images[:10]:
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=85)
                images_b64.append(base64.b64encode(buffer.getvalue()).decode())
            set_document_pages(len(images))
        elif suffix in [".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".gif"]:
            with open(file_path, "rb") as f:
                images_b64.append(base64.b64encode(f.read()).decode())
            set_document_pages(1)
        else:
            return f"Vision not supported for {suffix}. Use 'unstructured' or 'docling'."
 
        content = [{"type": "image", "source": {"type": "base64", "media_type": IMAGE_MEDIA_TYPE, "data": img}} for img in images_b64]
        content.append({"type": "text", "text": "Extract all text, tables, and structured information. Output as markdown."})
 
        try:
            response = get_client('bedrock-runtime').invoke_model(
                modelId=CLAUDE_MODEL_ID,
                body=json.dumps({
                    "anthropic_version": ANTHROPIC_VERSION,
                    "max_tokens": VISION_MAX_TOKENS,
                    "messages": [{"role": "user", "content": content}]
                })
            )
        except Exception as e:
            print(f"DEBUG Vision Error: {e}")  # See actual error
            return f"Error: {str(e)}"
        
        result = json.loads(response["body"].read())
        
        # Track vision tokens
        usage = result.get("usage", {})
        add_tokens("vision", usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        
        return result["content"][0]["text"]
    except ImportError as e:
        return f"Error: Missing library: {e}"
    except Exception as e:
        return f"Error: {str(e)}"
 
 
# =============================================================================
# STRUCTURED EXTRACTION TOOL
# =============================================================================
 
@tool
def extract_structured_data(document_text: str, extraction_prompt: str, output_schema: str = None) -> str:
    """
    Extract structured data from document text using Claude.
 
    Args:
        document_text: Text content from load_document
        extraction_prompt: What to extract (e.g., "Extract shipper, consignee, cargo details")
        output_schema: Optional JSON schema for output structure
 
    Returns:
        Extracted data as JSON string
    """
    try:
        doc_limit = DOC_TEXT_LIMIT
        if len(document_text) > doc_limit:
            document_text = document_text[:doc_limit] + "... [truncated]"
       
        extraction_prompt = extraction_prompt[:EXTRACTION_PROMPT_LIMIT].strip()
       
        system = "You are a precise document extractor. Extract ONLY requested information. Return valid JSON. Use null for missing fields."
        prompt = f"Document:\n---\n{document_text}\n---\n\nExtract: {extraction_prompt}"
        if output_schema:
            prompt += f"\n\nSchema: {output_schema}"
        prompt += "\n\nRespond with ONLY valid JSON."
 
        response = get_client('bedrock-runtime').invoke_model(
            modelId=CLAUDE_MODEL_ID,
            body=json.dumps({
                "anthropic_version": ANTHROPIC_VERSION,
                "max_tokens": CLAUDE_MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]
            })
        )
        result = json.loads(response["body"].read())
        
        # Track extraction tokens
        usage = result.get("usage", {})
        add_tokens("structured_extraction", usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        
        return result["content"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"

 
# =============================================================================
# AGENT DEFINITION
# =============================================================================
 
SYSTEM_PROMPT = """You are a Document Processor Agent specialized in extracting structured data from any document format.

TOOLS:
1. **textract_async**: For large/multi-page scanned PDFs in S3. Extracts forms, tables, AND signatures with confidence scores.
2. **load_document**: For local files. Choose method based on document type:
   - "unstructured": Text-based PDFs, DOCX, XLSX, emails (fast, no AWS cost)
   - "docling": Complex layouts with tables (good for structured reports)
   - "textract": Small local files with forms/tables (requires AWS, sync API)
   - "vision": **BEST FOR SCANNED PDFs, IMAGE-BASED PDFs, OR PDFs WITH EMBEDDED IMAGES, PNG, JPEG** (uses Claude vision)
3. **extract_structured_data**: Extract specific fields into JSON using Claude.

DOCUMENT TYPE DETECTION:
- If PDF extraction with "unstructured" returns little/no text → document is likely scanned/image-based → retry with "vision" method
- For PDFs containing photos, screenshots, or handwritten content → use "vision" method
- For forms with checkboxes, signatures, or structured fields → use "textract" or "textract_async"

WORKFLOW:
1. Identify document location (local or S3) and type
2. For scanned PDFs or image-based documents:
   - In S3 → use textract_async
   - Local → use load_document with method="vision"
3. For text-based documents → use load_document with method="unstructured"
4. If extraction returns empty/minimal text, retry with "vision" method
5. If specific entity extraction needed → use extract_structured_data
6. Report confidence scores and human review requirements

SIGNATURE CONFIDENCE:
- >= 85%: Valid
- 50-85%: Needs human review  
- < 50%: Invalid

Always report when human review is required."""

# ============================================
# Agent Factory (with Memory Support)
# ============================================
def create_agent(session_id: str , actor_id: str ) -> Agent:
    """Create Document Processor Agent with optional memory."""
    session_manager = None
    
    # Configure memory if available
    if MEMORY_ID and MEMORY_IMPORTS_AVAILABLE:
        session_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=AgentCoreMemoryConfig(
                memory_id=MEMORY_ID,
                session_id=session_id,
                actor_id=actor_id or "doc-processor"
            ),
            region_name=REGION
        )
        print(f"[DocProcessor] Memory ENABLED - Session: {session_id}")
    else:
        print(f"[DocProcessor] Memory DISABLED - Stateless mode")
    
    return Agent(
        system_prompt=SYSTEM_PROMPT,
        tools=[
            textract_async,
            load_document,
            extract_structured_data
        ],
        session_manager=session_manager
    )


 
# =============================================================================
# ENTRYPOINTS
# =============================================================================

app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload: dict) -> dict:
    """AgentCore entrypoint with usage tracking and session support."""
    reset_usage()
    print(f"Entry Point")
        
    prompt = payload.get("prompt", "Hello")
    print(f"prompt obtained: {prompt}")
    session_id = payload.get("session_id","session-100")
    print(f"session_id obtained: {session_id}")
    actor_id = payload.get("actor_id", "doc-processor")
    # Pad session_id to 33 chars (AgentCore requirement)
    if len(session_id) < 33:
        session_id = session_id + "-" + "0" * (33 - len(session_id) - 1)
    
    # Create agent with session for memory support
    doc_agent = create_agent(session_id, actor_id)
    print(f"Created.. agentcore agent for session: {session_id}, actor: {actor_id}")
    response = doc_agent(f"{prompt}\n\n[session_id: {session_id}]")
    
    return json.dumps({
        "response": str(response),
        "session_id": session_id,
         "actor_id": actor_id,
         "memory_enabled": MEMORY_ID is not None,
         "usage": get_usage(),
         "status": "success"
    })
 
 
# Simple CLI processing (for local testing)
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # CLI mode - process argument directly
        prompt = " ".join(sys.argv[1:])
        session_id = input("Enter session_id :")
        print(f"[CLI] Testing with prompt: {prompt}")
        
        # Generate unique session ID for CLI
        #session_id = f"cli-{uuid.uuid4().hex[:24]}"
        print(f"[CLI] Generated session_id: {session_id}")
        
        result = invoke({"prompt": prompt, "session_id": session_id})
        print(f"[CLI] Response: {result}")
        print(f"[CLI] MEMORY IMPORTS STATUS: {MEMORY_IMPORTS_AVAILABLE}")
    else:
        # Server mode
        app.run()

