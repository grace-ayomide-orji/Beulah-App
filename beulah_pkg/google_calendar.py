import os
from uuid import uuid4
import time
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TIMEZONE = 'Africa/Lagos'


def _admin_reminders():
    return {
        'useDefault': False,
        'overrides': [
            {'method': 'email', 'minutes': 24 * 60},
            {'method': 'popup', 'minutes': 60},
        ],
    }


def _get_calendar_service():
    required = {
        'GOOGLE_REFRESH_TOKEN': os.environ.get('GOOGLE_REFRESH_TOKEN'),
        'GOOGLE_CLIENT_ID': os.environ.get('GOOGLE_CLIENT_ID'),
        'GOOGLE_CLIENT_SECRET': os.environ.get('GOOGLE_CLIENT_SECRET'),
        'COUNSELOR_EMAIL': os.environ.get('COUNSELOR_EMAIL'),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing Google Calendar config: {', '.join(missing)}")

    creds = Credentials(
        None,  # no access token yet — this forces an immediate refresh using the refresh_token below
        refresh_token=os.environ.get('GOOGLE_REFRESH_TOKEN'),
        client_id=os.environ.get('GOOGLE_CLIENT_ID'),
        client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
        token_uri='https://oauth2.googleapis.com/token',
    )
    return build('calendar', 'v3', credentials=creds)


def _with_retry(func, max_attempts=3):
    """
    Retries a Google API call up to max_attempts times with a short
    exponential backoff (1s, 2s, 4s) before giving up entirely.
    This absorbs transient network blips and momentary Google-side
    errors automatically - no human involvement needed for those cases.
    Still raises the final exception if every attempt fails, so a
    genuine outage or bad token still surfaces as a real failure.
    """
    last_exception = None
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as e:
            last_exception = e
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # 1s, then 2s
    raise last_exception
 

def create_calendar_event(date_str, start_time_str, end_time_str, client_email, client_name, description=''):
    """
    date_str: 'YYYY-MM-DD', start/end_time_str: 'HH:MM'
    Returns {'event_id': ..., 'meet_link': ..., 'html_link': ...}
    Raises on any Google API error - caller decides how to handle that
    (e.g. fall back to demo-mode, or surface an error to the admin).
    """
    service = _get_calendar_service()

    event_body = {
        'summary': f'Counseling Session - {client_name}',
        'description': description,

        'start': {
            'dateTime': f'{date_str}T{start_time_str}:00',
            'timeZone': TIMEZONE
        },

        'end': {
            'dateTime': f'{date_str}T{end_time_str}:00',
            'timeZone': TIMEZONE
        },

        'attendees': [
            {
                'email': client_email,
                'displayName': client_name
            },
        ],

        'conferenceData': {
            'createRequest': {
                'requestId': f'beulah-{uuid4().hex}',
                'conferenceSolutionKey': {
                    'type': 'hangoutsMeet'
                },
            }
        },

        'reminders': _admin_reminders(),
    }

    calendar_id = os.environ.get(
        'GOOGLE_CALENDAR_ID',
        'primary'
    )
    
    event = _with_retry(
        lambda: service.events().insert(
            calendarId=calendar_id,
            body=event_body,
            conferenceDataVersion=1,
            sendUpdates='all',
        ).execute()
    )

    event_id = event.get('id')

    meet_link = ''

    # Google Meet conference creation is asynchronous. Poll the event briefly until conference generation completes.
    for attempt in range(6):

        conference_data = event.get('conferenceData', {})

        create_request = conference_data.get(
            'createRequest',
            {}
        )

        status = (
            create_request
            .get('status', {})
            .get('statusCode')
        )

        # Conference successfully created
        if status == 'success':
            for entry_point in conference_data.get(
                'entryPoints',
                []
            ):
                if entry_point.get('entryPointType') == 'video':
                    meet_link = entry_point.get('uri', '')
                    break

            if meet_link:
                break

        # Google explicitly reported failure
        if status == 'failure':
            raise RuntimeError(
                'Google Meet conference creation failed.'
            )

        # Still pending. Fetch the event again.
        if attempt < 5:
            time.sleep(1)

            event = _with_retry(
                lambda: service.events().get(
                    calendarId=calendar_id,
                    eventId=event_id
                ).execute()
            )


    return {
        'event_id': event_id,
        'meet_link': meet_link,
        'html_link': event.get('htmlLink', ''),
    }


def delete_calendar_event(event_id):
    if not event_id:
        return
    service = _get_calendar_service()
    _with_retry(lambda: service.events().delete(
        calendarId=os.environ.get('GOOGLE_CALENDAR_ID', 'primary'),
        eventId=event_id,
        sendUpdates='all',
    ).execute())


def update_calendar_event(event_id, date_str, start_time_str, end_time_str):
    if not event_id:
        return
    service = _get_calendar_service()
    _with_retry(lambda: service.events().patch(
        calendarId=os.environ.get('GOOGLE_CALENDAR_ID', 'primary'),
        eventId=event_id,
        body={
            'start': {'dateTime': f'{date_str}T{start_time_str}:00', 'timeZone': TIMEZONE},
            'end': {'dateTime': f'{date_str}T{end_time_str}:00', 'timeZone': TIMEZONE},
            'reminders': _admin_reminders(),
        },
        sendUpdates='all',
    ).execute())
