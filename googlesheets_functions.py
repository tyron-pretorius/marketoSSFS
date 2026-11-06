from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import time
import pandas as pd

MAX_REQUESTS_PER_MINUTE = 60
TIME_INTERVAL = 60 / MAX_REQUESTS_PER_MINUTE
SERVICE_ACCOUNT_FILE = 'inbound-footing-xxx-123.json'
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


# ---------------------------------------------------------------------------
# Progress reporting
#
# writeDF2Sheet above appends. A long-running batch also needs to *change* a
# row it already wrote - "200/1000 leads processed", then 400, then the final
# callback response - so a marketer watching the sheet can see it moving
# rather than waiting for one row to appear at the end.
# ---------------------------------------------------------------------------

# Headers change rarely, and a batch asks for them repeatedly.
_header_cache: dict[str, list[str]] = {}

# Where each batch's row is. Looked up once and remembered: a batch reports
# progress many times, the sheet only ever has rows appended, so the row never
# moves. Scanning the whole first column on every update is what exhausts the
# read quota on a sheet with tens of thousands of rows - and a throttled
# update does not raise, it simply never lands.
_row_index_cache: dict[str, int] = {}


def _column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA.

    Sheets runs past Z, and these logs get there: _split_long_text adds
    request_2, callback_object_2 and so on when a value will not fit in one
    cell. Arithmetic on a single character silently addresses the wrong
    column once there are more than 26 of them.
    """
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _headers(sheet_name: str, spreadsheet_id: str) -> list[str]:
    key = f"{spreadsheet_id}:{sheet_name}"
    if key not in _header_cache:
        time.sleep(TIME_INTERVAL)
        result = _get_service().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_name}!1:1"
        ).execute()
        values = result.get("values") or [[]]
        _header_cache[key] = values[0]
    return _header_cache[key]


def updateBatchRow(batch_id: str, updates: dict, sheet_name: str,
                   spreadsheet_id: str) -> bool:
    """Change named columns on the row whose first cell is `batch_id`.

    `batch_id` is the timestamp written when the batch started. Give it
    microsecond precision: Marketo fires parallel invocations inside the same
    second, and at second precision they all find the same row - the first
    batch collects everyone's progress while the rest sit unchanged.

    Returns False when the row or the headers cannot be found, so a logging
    problem never stops the work that is being logged.
    """
    headers = _headers(sheet_name, spreadsheet_id)
    if not headers:
        print(f"No headers on {sheet_name}; skipping batch update")
        return False

    cache_key = f"{spreadsheet_id}:{sheet_name}:{batch_id}"
    row_index = _row_index_cache.get(cache_key)

    if row_index is None:
        time.sleep(TIME_INTERVAL)
        column_a = _get_service().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_name}!A:A"
        ).execute().get("values", [])

        for i, row in enumerate(column_a[1:], start=2):   # skip the header
            if row and row[0] == batch_id:
                row_index = i
                break

        if row_index is None:
            print(f"Batch {batch_id} not found in {sheet_name}")
            return False
        _row_index_cache[cache_key] = row_index

    data = []
    for column_name, value in updates.items():
        if column_name not in headers:
            continue
        letter = _column_letter(headers.index(column_name))
        data.append({
            "range": f"{sheet_name}!{letter}{row_index}",
            "values": [[str(value)]],
        })

    if not data:
        print(f"No matching columns on {sheet_name} for {list(updates)}")
        return False

    time.sleep(TIME_INTERVAL)
    _get_service().spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute(num_retries=2)
    return True
