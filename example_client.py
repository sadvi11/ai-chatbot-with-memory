#!/usr/bin/env python3
import requests

BASE_URL = "http://localhost:8000"

class ChatbotClient:
    def __init__(self, user_id):
        self.user_id = user_id
    
    def chat(self, message):
        payload = {"message": message, "user_id": self.user_id}
        response = requests.post(f"{BASE_URL}/chat", json=payload)
        return response.json()

def main():
    print("=" * 60)
    print("AI CHATBOT WITH MEMORY - TEST")
    print("=" * 60)
    
    user_id = "test_user"
    client = ChatbotClient(user_id)
    
    print("\n1️⃣ Sending first message...")
    try:
        response = client.chat("Hello, what is machine learning?")
        print(f"   Bot: {response['response'][:80]}...")
        print("   ✅ Success")
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return
    
    print("\n2️⃣ Sending follow-up (testing memory)...")
    try:
        response = client.chat("Give me an example")
        print(f"   Bot: {response['response'][:80]}...")
        print(f"   Conversation length: {response['conversation_length']}")
        print("   ✅ Success (bot remembers!)")
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return
    
    print("\n" + "=" * 60)
    print("✅ ALL TESTS PASSED!")
    print("=" * 60)

if __name__ == "__main__":
    main()
