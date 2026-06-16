from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from anthropic import Anthropic
from anthropic import APIError, APIConnectionError
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
import os
import logging
import asyncio
from typing import Optional, List
from slowapi import Limiter
from slowapi.util import get_remote_address
import hmac
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_CONVERSATION_LENGTH = 100
MAX_MESSAGE_LENGTH = 2000
MIN_MESSAGE_LENGTH = 1
CLAUDE_API_TIMEOUT = 10.0
RATE_LIMIT = "10/minute"
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app = FastAPI(title="AI Chatbot with Memory", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization"],
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION', 'us-east-1'))
table = dynamodb.Table(os.getenv('DYNAMODB_TABLE', 'chatbot-conversations'))
client = Anthropic()

MODEL = os.getenv('MODEL', 'claude-3-5-sonnet-20241022')
SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    user_id: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    model_override: Optional[str] = None

    @field_validator('message')
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Message cannot be empty")
        return v.strip()

class ChatResponse(BaseModel):
    response: str
    model_used: str
    user_id: str
    timestamp: str
    conversation_length: int

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials) -> str:
    try:
        token = credentials.credentials
        parts = token.split(':')
        if len(parts) < 3:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = parts[0]
        message = ':'.join(parts[:-1])
        provided_signature = parts[-1]

        expected_signature = hmac.new(
            SECRET_KEY.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(provided_signature, expected_signature):
            raise HTTPException(status_code=401, detail="Invalid token")

        return user_id
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Token verification failed")
        raise HTTPException(status_code=401, detail="Authentication failed")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    return verify_token(credentials)

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = None
        self.is_open = False

    def record_success(self):
        self.failure_count = 0
        self.is_open = False

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True

    def can_execute(self) -> bool:
        if not self.is_open:
            return True
        recovery_time = datetime.now() - self.last_failure_time
        if recovery_time.total_seconds() > self.recovery_timeout:
            self.is_open = False
            self.failure_count = 0
            return True
        return False

claude_breaker = CircuitBreaker()

async def call_claude_api(messages: List[dict], model: str, timeout: float = CLAUDE_API_TIMEOUT) -> str:
    if not claude_breaker.can_execute():
        raise HTTPException(status_code=503, detail="Claude API unavailable")

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=1000,
                messages=messages
            ),
            timeout=timeout
        )
        claude_breaker.record_success()
        return response.content[0].text

    except asyncio.TimeoutError:
        claude_breaker.record_failure()
        logger.error("Claude API timeout")
        raise HTTPException(status_code=504, detail="Claude API timeout")
    except APIConnectionError as e:
        claude_breaker.record_failure()
        logger.error(f"Claude API connection error: {str(e)}")
        raise HTTPException(status_code=503, detail="Claude API error")
    except Exception as e:
        claude_breaker.record_failure()
        logger.exception("Claude API error")
        raise HTTPException(status_code=500, detail="Internal error")

async def get_conversation(user_id: str) -> List[dict]:
    try:
        response = table.get_item(Key={'user_id': user_id})
        if 'Item' not in response:
            return []
        messages = response['Item'].get('messages', [])
        return messages[-MAX_CONVERSATION_LENGTH:]
    except ClientError as e:
        logger.error(f"DynamoDB error: {str(e)}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    except Exception as e:
        logger.exception("Get conversation failed")
        raise HTTPException(status_code=500, detail="Internal error")

async def save_conversation(user_id: str, messages: List[dict], model: str) -> bool:
    try:
        messages_to_save = messages[-MAX_CONVERSATION_LENGTH:]
        item = {
            'user_id': user_id,
            'messages': messages_to_save,
            'last_model_version': model,
            'created_at': messages[0].get('timestamp', datetime.now().isoformat()) if messages else datetime.now().isoformat(),
            'last_updated': datetime.now().isoformat(),
            'ttl': int((datetime.now() + timedelta(days=30)).timestamp())
        }
        table.put_item(Item=item)
        return True
    except Exception as e:
        logger.error(f"Save conversation failed: {str(e)}")
        return False

@app.get("/health")
async def health_check():
    checks = {"api": True, "dynamodb": False, "claude_api": not claude_breaker.is_open}
    try:
        table.get_item(Key={'user_id': 'health-check'})
        checks["dynamodb"] = True
    except:
        pass
    
    if all(checks.values()):
        return {"status": "healthy", "timestamp": datetime.now().isoformat(), "model": MODEL, "checks": checks}
    else:
        raise HTTPException(status_code=503, detail="Service degraded")

@app.post("/chat", response_model=ChatResponse)
@limiter.limit(RATE_LIMIT)
async def chat(request: ChatRequest, current_user: str = Depends(get_current_user)):
    user_id = request.user_id
    
    if user_id != current_user:
        logger.warning(f"Unauthorized access attempt by {current_user} to {user_id}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        start_time = datetime.now()
        history = await get_conversation(user_id)
        messages = history + [{'role': 'user', 'content': request.message, 'timestamp': datetime.now().isoformat()}]
        
        assistant_message = await call_claude_api(messages=messages, model=request.model_override or MODEL)
        
        new_message = {
            'role': 'assistant',
            'content': assistant_message,
            'model_version': request.model_override or MODEL,
            'timestamp': datetime.now().isoformat()
        }
        
        full_history = messages + [new_message]
        await save_conversation(user_id, full_history, request.model_override or MODEL)
        
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        logger.info(f"Chat completed for {user_id} in {elapsed_ms:.0f}ms")

        return ChatResponse(
            response=assistant_message,
            model_used=request.model_override or MODEL,
            user_id=user_id,
            timestamp=datetime.now().isoformat(),
            conversation_length=len(full_history)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Chat failed for {user_id}")
        raise HTTPException(status_code=500, detail="Internal error")

@app.get("/conversation/{user_id}")
async def get_conversation_endpoint(user_id: str, current_user: str = Depends(get_current_user)):
    if user_id != current_user:
        logger.warning(f"Unauthorized access by {current_user}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        response = table.get_item(Key={'user_id': user_id})
        if 'Item' not in response:
            raise HTTPException(status_code=404, detail="No conversation found")
        
        item = response['Item']
        return {'user_id': user_id, 'messages': item.get('messages', []), 'created_at': item.get('created_at', ''), 'last_updated': item.get('last_updated', '')}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Get conversation failed")
        raise HTTPException(status_code=500, detail="Internal error")

@app.get("/metrics/{user_id}")
async def get_metrics(user_id: str, current_user: str = Depends(get_current_user)):
    if user_id != current_user:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        response = table.get_item(Key={'user_id': user_id})
        if 'Item' not in response:
            return {"user_id": user_id, "message_count": 0, "models_used": {}}
        
        messages = response['Item'].get('messages', [])
        models_used = {}
        for msg in messages:
            if 'model_version' in msg:
                model = msg['model_version']
                models_used[model] = models_used.get(model, 0) + 1
        
        return {"user_id": user_id, "message_count": len(messages), "models_used": models_used, "last_updated": response['Item'].get('last_updated')}

    except Exception as e:
        logger.exception("Get metrics failed")
        raise HTTPException(status_code=500, detail="Internal error")

@app.get("/")
async def root():
    return {"name": "AI Chatbot with Memory", "version": "2.0.0", "status": "ready", "auth_required": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
