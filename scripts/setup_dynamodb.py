"""Optional AWS step. Run only after credentials are supplied; never run at startup."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import boto3
from botocore.exceptions import ClientError
from gridmind.config import settings


def main():
    parser = argparse.ArgumentParser(description='Create the GRIDMIND operational event table when AWS is ready')
    parser.add_argument('--create',action='store_true',required=True)
    parser.parse_args()
    client = boto3.client('dynamodb',region_name=settings.aws_region,endpoint_url=settings.dynamodb_endpoint_url)
    try:
        table = client.describe_table(TableName=settings.dynamodb_table)['Table']
        actual = {(k['AttributeName'],k['KeyType']) for k in table['KeySchema']}
        if actual != {('PK','HASH'),('SK','RANGE')}:
            raise RuntimeError('Existing table has incompatible keys; no changes made')
        print('Compatible DynamoDB event table already exists.')
    except client.exceptions.ResourceNotFoundException:
        client.create_table(TableName=settings.dynamodb_table,BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName':'PK','KeyType':'HASH'},{'AttributeName':'SK','KeyType':'RANGE'}],
            AttributeDefinitions=[{'AttributeName':'PK','AttributeType':'S'},{'AttributeName':'SK','AttributeType':'S'}])
        client.get_waiter('table_exists').wait(TableName=settings.dynamodb_table)
        print('DynamoDB operational event table created.')


if __name__ == '__main__':
    main()
