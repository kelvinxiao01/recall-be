import datetime
import os
import google.auth
#from datetime import datetime, timedelta
import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuration ---
# Set the path to your service account key file
# Make sure this file is in the same directory as your script or provide the full path
GOOGLE_CREDENTIALS_FILE = 'credentials.json'

# Set your Calendar ID
# Use 'primary' for the primary calendar of the service account
# Or use the specific calendar ID you found (e.g., 'yourcalendar@group.calendar.google.com')
CALENDAR_ID = 'primary' # Or your specific calendar ID, e.g., 'yourcalendar@group.calendar.google.com'

CALENDAR_ID = os.getenv("CALENDAR_ID", "medaroui99@gmail.com")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DEFAULT_MEETING_DURATION = int(os.getenv("DEFAULT_MEETING_DURATION", "60"))  

# --- End Configuration ---

def main():
    try:
        # Load credentials from the service account key file
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=['https://www.googleapis.com/auth/calendar.readonly'] # Use readonly scope for testing
        )

        # Build the service object for the Calendar API
        service = build('calendar', 'v3', credentials=credentials)

        # Call the Calendar API to fetch events
        now = datetime.datetime.utcnow().isoformat() + 'Z' # 'Z' indicates UTC time
        print("now", now)
        print(f'Getting the next 10 events from calendar: {CALENDAR_ID}')

        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        if not events:
            print('No upcoming events found.')
            return

        print('Upcoming events:')
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            print(f"{start} - {event['summary']}")


        start_time = "2025-11-04T22:00:00-04:00"
        start_time = datetime.datetime.fromisoformat(start_time)
        purpose = "test apiiiii"
        caller_name = "moddy"
        end_time = start_time + datetime.timedelta(minutes=60)
            
        event = {
            'summary': f'Meeting with {caller_name}',
            'description': f'Purpose: {purpose}\nPhone: {555-555-3433}',
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'America/New_York',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/New_York',
            },
            'attendees': [
                {'email': caller_name if '@' in caller_name else None}
            ] if '@' in caller_name else [],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},
                    {'method': 'popup', 'minutes': 30},
                ],
            },
        }
        
        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()
        print("created with success")

    except FileNotFoundError:
        print(f"Error: {GOOGLE_CREDENTIALS_FILE} not found. Make sure it's in the correct directory.")
    except HttpError as error:
        print(f"An HTTP error occurred: {error}")
        print("Please check:")
        print("  1. If the Google Calendar API is enabled for your project.")
        print("  2. If the service account has appropriate permissions (read access to the calendar).")
        print("  3. If the CALENDAR_ID is correct and the service account has access to it.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

if __name__ == '__main__':
    main()
