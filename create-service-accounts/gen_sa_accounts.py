# -*- coding: utf-8 -*-
import errno
import os
import pickle
import sys
import time
from argparse import ArgumentParser
from base64 import b64decode
from glob import glob
from json import loads
from random import choice
from time import sleep

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/cloud-platform',
          'https://www.googleapis.com/auth/iam']

# --- Helper Functions ---

def _generate_id(prefix='saf-'):
    chars = '-abcdefghijklmnopqrstuvwxyz1234567890'
    return prefix + ''.join(choice(chars) for _ in range(25)) + choice(chars[1:])

def _def_batch_resp(id, resp, exception):
    if exception is not None:
        if not str(exception).startswith('<HttpError 429'):
            print(f"Batch request error: {exception}")
        sleep(0.3)

# --- Core Logic for Service Accounts ---

def _list_sas(iam, project):
    try:
        resp = iam.projects().serviceAccounts().list(name='projects/' + project, pageSize=100).execute()
        return resp.get('accounts', [])
    except HttpError as e:
        print(f"Error listing service accounts for project {project}: {e}")
        return []

def _create_accounts_batch(iam, project, count):
    batch = iam.new_batch_http_request(callback=_def_batch_resp)
    for _ in range(count):
        aid = _generate_id('mfc-')
        batch.add(iam.projects().serviceAccounts().create(
            name=f'projects/{project}',
            body={'accountId': aid, 'serviceAccount': {'displayName': aid}}
        ))
    batch.execute()

def _create_remaining_accounts(iam, project):
    print(f"Starting service account creation in project: {project}")
    sa_count = len(_list_sas(iam, project))
    CHUNK_SIZE = 10
    DELAY_BETWEEN_CHUNKS = 5

    while sa_count < 100:
        to_create = min(CHUNK_SIZE, 100 - sa_count)
        print(f"  - Have {sa_count}/100 service accounts. Creating next {to_create}...")
        _create_accounts_batch(iam, project, to_create)
        print(f"  - Batch of {to_create} submitted. Waiting {DELAY_BETWEEN_CHUNKS} seconds...")
        time.sleep(DELAY_BETWEEN_CHUNKS)
        sa_count = len(_list_sas(iam, project))
    
    print(f"✅ Successfully ensured 100 service accounts exist in {project}.")

# ##################################################################
# ##############  这里是本次修正的核心  ##############
#  重写了密钥下载函数，使其更健壮、可重试，并优化了文件名
# ##################################################################
def _create_sa_keys(iam, project, path):
    print(f"\nStarting key creation and download for project: {project}")
    os.makedirs(path, exist_ok=True)
    
    all_sas = _list_sas(iam, project)
    if not all_sas:
        print(f"  - No service accounts found in {project}. Aborting key download.")
        return

    print(f"  - Found {len(all_sas)} service accounts. Checking for existing keys...")
    
    keys_to_create_map = {}
    for sa in all_sas:
        try:
            # 每个SA最多有10个key，我们只在没有key时才创建
            keys = iam.projects().serviceAccounts().keys().list(name=sa['name']).execute().get('keys', [])
            if not keys:
                keys_to_create_map[sa['name']] = sa['email']
        except HttpError as e:
            print(f"  - Warning: Could not list keys for {sa.get('email', 'Unknown SA')}: {e}")

    if not keys_to_create_map:
        print("✅ All service accounts already have keys. No new keys needed.")
        return

    print(f"  - Found {len(keys_to_create_map)} service accounts without keys. Preparing to create them...")
    
    # 分批创建密钥
    sa_names = list(keys_to_create_map.keys())
    CHUNK_SIZE = 10
    DELAY_BETWEEN_CHUNKS = 5

    for i in range(0, len(sa_names), CHUNK_SIZE):
        chunk = sa_names[i:i + CHUNK_SIZE]
        print(f"  - Creating keys for batch {i//CHUNK_SIZE + 1} ({len(chunk)} accounts)...")
        
        key_dump = []
        def key_creation_callback(id, resp, exception):
            if exception:
                _def_batch_resp(id, resp, exception)
            else:
                key_dump.append({
                    "email": keys_to_create_map[resp['name'].split('/keys/')[0]],
                    "key_data": b64decode(resp['privateKeyData']).decode('utf-8')
                })

        batch = iam.new_batch_http_request(callback=key_creation_callback)
        for name in chunk:
            batch.add(iam.projects().serviceAccounts().keys().create(
                name=name,
                body={'privateKeyType': 'TYPE_GOOGLE_CREDENTIALS_FILE', 'keyAlgorithm': 'KEY_ALG_RSA_2048'}
            ))
        batch.execute()

        # 将下载的密钥写入文件
        for key_info in key_dump:
            filename = os.path.join(path, f"{key_info['email'].split('@')[0]}.json")
            with open(filename, 'w') as f:
                f.write(key_info['key_data'])
        
        print(f"  - Wrote {len(key_dump)} key files. Waiting {DELAY_BETWEEN_CHUNKS} seconds...")
        time.sleep(DELAY_BETWEEN_CHUNKS)

    print(f"✅ Key download process complete. JSON files are in '{path}' folder.")

# --- Main Authentication and Orchestration ---

def serviceaccountfactory(credentials, token, path, create_sas, download_keys):
    creds = None
    if os.path.exists(token):
        with open(token, 'rb') as t:
            creds = pickle.load(t)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials, SCOPES)
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            auth_url, _ = flow.authorization_url(prompt='consent')
            print(f'Please go to this URL to authorize:\n{auth_url}')
            code = input('Paste the authorization code here: ')
            flow.fetch_token(code=code)
            creds = flow.credentials
        with open(token, 'wb') as t:
            pickle.dump(creds, t)

    iam = build('iam', 'v1', credentials=creds)

    if create_sas:
        _create_remaining_accounts(iam, create_sas)
    
    if download_keys:
        _create_sa_keys(iam, download_keys, path)

if __name__ == '__main__':
    parse = ArgumentParser(description='A tool to create Google service accounts and download their keys.')
    parse.add_argument('--path', '-p', default='accounts', help='Directory to output the credential files.')
    parse.add_argument('--token', default='token_sa.pickle', help='Pickle token file path.')
    parse.add_argument('--credentials', default='credentials.json', help='Credentials file path.')
    parse.add_argument('--create-sas', help='Create 100 service accounts in the specified project ID.')
    parse.add_argument('--download-keys', help='Download keys for service accounts in the specified project ID.')
    args = parse.parse_args()

    if not (args.create_sas or args.download_keys):
        print("Error: You must specify either --create-sas or --download-keys (or both).")
        sys.exit(1)

    serviceaccountfactory(
        credentials=args.credentials,
        token=args.token,
        path=args.path,
        create_sas=args.create_sas,
        download_keys=args.download_keys
    )
