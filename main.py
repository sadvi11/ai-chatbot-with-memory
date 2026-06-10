from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from anthropic import Anthropic
import boto3
from datetime import datetime, timedelta
import os
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Chatbot with Memory", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

dynamodb = boto3.resource('dynamodb', region_name=os.getenv('AWS_REGION', 'us-east-1'))
table = dynamodb.Table(os.getenv('DYNAMODB_TABLE', 'chatbot-conversations'))

client = Anthropic()
MODEL = os.getenv('MODEL', 'claude-3-5-sonnet-20241022')

class ChatRequest(BaseModel):
    message: str
    user_id: str
    model_override: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    model_used: str
    user_id: str
    timestamp: str
    conversation_length: int

@app.get("/health")
async def health_check():
    try:
        table.table_status
        return {"status": "healthy", "timestamp": datetime.now().isoformat(), "model": MODEL}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Service unavailable")

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        user_id = request.user_id
        message = request.message
        model = request.model_override or MODEL
        
        try:
            response = table.get_item(Key={'user_id': user_id})
            history = response.get('Item', {}).get('messages', [])
        except:
            history = []
        
        messages = history + [{'role': 'user', 'content': message}]
        
        try:
            claude_response = client.messages.create(model=model, max_tokens=1000, messages=messages)
            assistant_message = claude_response.content[0].text
        except Exception as e:
            raise HTTPException(status_code=500, detail="Failed to generate response")
        
        new_message = {'role': 'assistant', 'content': assistant_message, 'model_version': model, 'timestamp': datetime.now().isoformat()}
        full_history = messages + [new_message]
        
        try:
            table.put_item(Item={'user_id': user_id, 'messages': full_history, 'last_model_version': model, 'created_at': history[0].get('timestamp', datetime.now().isoformat()) if history else datetime.now().isoformat(), 'last_updated': datetime.now().isoformat(), 'ttl': int((datetime.now() + timedelta(days=30)).timestamp())})
        except:
            pass
        
        return ChatResponse(response=assistant_message, model_used=model, user_id=user_id, timestamp=datetime.now().isoformat(), conversation_length=len(full_history))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/conversation/{user_id}")
async def get_conversation(user_id: str):
    try:
        response = table.get_item(Key={'user_id': user_id})
        if 'Item' not in response:
            raise HTTPException(status_code=404, detail="No conversation found")
        item = response['Item']
        return {'user_id': user_id, 'messages': item.get('messages', []), 'created_at': item.get('created_at', ''), 'last_updated': item.get('last_updated', '')}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed")

@app.get("/metrics/{user_id}")
async def get_metrics(user_id: str):
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
        return {"user_id": user_id, "message_count": len(messages), "models_used": models_used}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed")

@app.get("/")
async def root():
    return {"name": "AI Chatbot with Memory", "version": "1.0", "status": "ready"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
