#!/usr/bin/env python3
import boto3
import os

region = os.getenv('AWS_REGION', 'us-east-1')
table_name = os.getenv('DYNAMODB_TABLE', 'chatbot-conversations')
dynamodb = boto3.client('dynamodb', region_name=region)

print(f"Creating DynamoDB table: {table_name}")

try:
    response = dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{'AttributeName': 'user_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'user_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    print("✅ Table created!")
except:
    print("⚠️ Table already exists")
