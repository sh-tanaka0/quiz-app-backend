# app/aws_clients.py
import boto3
from .config import settings

def get_s3_client():
    """S3クライアントを取得する"""
    return boto3.client(
        's3',
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
        endpoint_url=settings.aws_endpoint_url # LocalStack接続に必要
    )

def get_dynamodb_resource():
    """DynamoDBリソースを取得する"""
    return boto3.resource(
        'dynamodb',
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
        endpoint_url=settings.aws_endpoint_url # LocalStack接続に必要
    )

s3_client = get_s3_client()
dynamodb_resource = get_dynamodb_resource()
dynamodb_table = dynamodb_resource.Table(settings.dynamodb_table_name)