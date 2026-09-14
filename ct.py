import json
from pathlib import Path
import os

def count_user_data(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    
    user_count = len(data)
    active_subscriptions = sum(1 for user in data.values() if user['subscription'] == 'active')
    inactive_subscriptions = sum(1 for user in data.values() if user['subscription'] == 'inactive')
    total_request_count = sum(user['request_count'] for user in data.values())

    return {
        'user_count': user_count,
        'active_subscriptions': active_subscriptions,
        'inactive_subscriptions': inactive_subscriptions,
        'total_request_count': total_request_count
    }

if __name__ == '__main__':
    file_path = Path(os.environ.get('DATA_DIR', Path(__file__).resolve().parent)) / 'user_data.json'
    print(count_user_data(file_path))
