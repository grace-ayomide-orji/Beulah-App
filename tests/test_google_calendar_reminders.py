import unittest
from unittest.mock import MagicMock, patch

from beulah_pkg.google_calendar import create_calendar_event, update_calendar_event


EXPECTED_ADMIN_REMINDERS = {
    'useDefault': False,
    'overrides': [
        {'method': 'email', 'minutes': 1440},
        {'method': 'popup', 'minutes': 60},
    ],
}


class GoogleCalendarReminderTests(unittest.TestCase):
    @patch('beulah_pkg.google_calendar._get_calendar_service')
    def test_created_event_has_admin_reminders_without_attendee_overrides(
        self,
        get_service,
    ):
        service = MagicMock()
        events = service.events.return_value
        events.insert.return_value.execute.return_value = {
            'id': 'event-1',
            'htmlLink': 'https://calendar.google.com/event-1',
            'conferenceData': {
                'createRequest': {'status': {'statusCode': 'success'}},
                'entryPoints': [
                    {
                        'entryPointType': 'video',
                        'uri': 'https://meet.google.com/example',
                    },
                ],
            },
        }
        get_service.return_value = service

        create_calendar_event(
            date_str='2030-01-07',
            start_time_str='10:00',
            end_time_str='11:00',
            client_email='client@example.com',
            client_name='Client Name',
            description='Counselling session',
        )

        event_body = events.insert.call_args.kwargs['body']
        self.assertEqual(event_body['reminders'], EXPECTED_ADMIN_REMINDERS)
        self.assertEqual(event_body['attendees'], [{
            'email': 'client@example.com',
            'displayName': 'Client Name',
        }])
        self.assertNotIn('reminders', event_body['attendees'][0])

    @patch('beulah_pkg.google_calendar._get_calendar_service')
    def test_rescheduled_event_reapplies_admin_reminders(self, get_service):
        service = MagicMock()
        events = service.events.return_value
        get_service.return_value = service

        update_calendar_event(
            event_id='event-1',
            date_str='2030-01-08',
            start_time_str='14:00',
            end_time_str='15:00',
        )

        patch_body = events.patch.call_args.kwargs['body']
        self.assertEqual(patch_body['reminders'], EXPECTED_ADMIN_REMINDERS)
        self.assertNotIn('attendees', patch_body)


if __name__ == '__main__':
    unittest.main()
