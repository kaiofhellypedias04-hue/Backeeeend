from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import json
from datetime import datetime

from worker.adapter.executor import structured_execute
from worker.models import WorkerResult

def main():
    parser = argparse.ArgumentParser(description='NFS-e Audit Worker Runner')
    parser.add_argument('--payload', required=True, help='JSON payload file path')
    parser.add_argument('--execution-id', help='Override execution ID')
    parser.add_argument('--debug', action='store_true', help='Keep temp files, expose tempDir')
    args = parser.parse_args()
    
    try:
        with open(args.payload, 'r') as f:
            payload_dict = json.load(f)
        
        if args.execution_id:
            payload_dict['executionId'] = args.execution_id
        
        payload_dict['debug'] = args.debug
        
        print("Worker execution started...")
        
        result_dict = structured_execute(payload_dict)
        result = WorkerResult(**result_dict)
        
        print(json.dumps(result.to_dict(), default=str, indent=2))
        
        sys.exit(0 if result.status == 'completed' else 1)
        
    except FileNotFoundError:
        print(json.dumps({
            'status': 'failed',
            'errorCode': 'VALIDATION_ERROR',
            'errorMessage': f'Payload file not found: {args.payload}'
        }, indent=2))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({
            'status': 'failed',
            'executionId': payload_dict.get('executionId', 'unknown') if 'payload_dict' in locals() else 'unknown',
            'errorCode': 'UNEXPECTED_ERROR',
            'errorMessage': str(e)
        }, indent=2))
        sys.exit(1)

if __name__ == '__main__':
    main()

