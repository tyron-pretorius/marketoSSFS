from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import time
import pandas as pd

MAX_REQUESTS_PER_MINUTE = 60
TIME_INTERVAL = 60 / MAX_REQUESTS_PER_MINUTE
SERVICE_ACCOUNT_FILE = 'inbound-footing-412823-c2ffc4659d84.json'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def _get_service():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds)


def writeDF2Sheet(df: pd.DataFrame, sheet_name: str, spreadsheet_id: str):

    if df.empty:
        return

    time.sleep(TIME_INTERVAL)
    body = {"values": df.astype(str).values.tolist()}
    _get_service().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A2",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body=body
    ).execute()

def writeRow2Sheet(row, sheet_name, spreadsheet_id ):
    time.sleep(TIME_INTERVAL)
    
    service = _get_service()

    row_data = row.values.tolist()
    values = [row_data]

    range_name = f'{sheet_name}!A2'

    # Call the Sheets API
    sheet = service.spreadsheets()
    body = {
        'values': values
    }
    result = sheet.values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body=body).execute()

    print(f"{result.get('updates').get('updatedCells')} cells updated.")