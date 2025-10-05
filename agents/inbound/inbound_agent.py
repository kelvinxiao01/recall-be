from dotenv import load_dotenv
import logging
import os
from typing import Optional
import datetime
from livekit import agents
from livekit.agents import JobContext, WorkerOptions, AgentSession, Agent, RunContext, function_tool, RoomInputOptions
from livekit.plugins import deepgram, openai, cartesia, silero, noise_cancellation, elevenlabs, google
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from supabase import create_client, Client


load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Business configuration
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "John Doe Legal")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "Mon-Fri 9AM-5PM")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "+19297173949")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY")

# Google Calendar configuration
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DEFAULT_MEETING_DURATION = int(os.getenv("DEFAULT_MEETING_DURATION", "60"))

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SK")  # Use secret key for backend operations

SYSTEM_INSTRUCTIONS = f"""You are a professional legal receptionist for {BUSINESS_NAME}.

IMPORTANT: Today's date is {datetime.datetime.now().strftime('%Y-%m-%d')} ({datetime.datetime.now().strftime('%A, %B %d, %Y')}).
When scheduling appointments, always use the year 2025 unless the caller explicitly specifies a different year.

Your role is to:
- Answer calls professionally with appropriate legal office formality
- Greet callers with the firm name
- Schedule client consultations and appointments
- Provide office information like hours ({BUSINESS_HOURS}) and phone number ({BUSINESS_PHONE})
- Take messages for attorneys and staff members
- Handle inquiries about case status, documentation, and general legal services

IMPORTANT: You cannot provide legal advice. If callers ask legal questions, politely explain that you'll need to have an attorney call them back, or offer to schedule a consultation.

Always be:
- Professional, courteous, and discreet
- Respectful of client confidentiality
- Clear and precise in communication
- Patient and empathetic with caller concerns
- Helpful within your administrative capabilities

IMPORTANT CONVERSATION FLOW FOR SCHEDULING CONSULTATIONS:
1. When callers want to schedule a consultation or appointment:
   - Ask for their preferred date and time
   - Optionally use get_available_slots to check if a specific date is free
2. Once you have the date/time, collect:
   - Caller's full name
   - Nature of legal matter or consultation purpose
   - DO NOT ask for phone number - it is automatically detected from the call
3. Use schedule_appointment to book the consultation directly on the calendar
4. Confirm the appointment details with the caller

For general messages (NOT scheduling): Use take_message only for non-scheduling inquiries such as case status questions, document requests, or callback requests.

Start each call by greeting the caller: "Thank you for calling {BUSINESS_NAME}, how may I assist you today?"
"""


class ReceptionistAgent(Agent):
    def __init__(self):
        super().__init__(instructions=SYSTEM_INSTRUCTIONS)
        self.caller_phone = None
        self.caller_name = None
        self.meeting_date = None
        self._calendar_service = None
        self.call_start_time = datetime.datetime.now()
        self.call_notes = []  # Track important events during the call

    def add_note(self, note: str):
        """Add a note to the call history."""
        self.call_notes.append(note)
        logger.info(f"Call note added: {note}")

    def _get_calendar_service(self):
        """Initialize and return Google Calendar service."""
        if self._calendar_service is None:
            credentials = service_account.Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=['https://www.googleapis.com/auth/calendar']
            )
            self._calendar_service = build('calendar', 'v3', credentials=credentials)
        return self._calendar_service

    @function_tool()
    async def get_business_hours(self, ctx: RunContext) -> str:
        """Get the business hours and availability information."""
        logger.info("Caller requested business hours")
        return f"Our business hours are {BUSINESS_HOURS}. We're happy to schedule a meeting during these times."

    @function_tool()
    async def get_available_slots(
        self,
        ctx: RunContext,
        date: str
    ) -> str:
        """Check available time slots for a specific date. Date should be in format YYYY-MM-DD."""
        logger.info(f"Checking availability for {date}")

        try:
            service = self._get_calendar_service()

            # Parse the date and create time range for the day
            target_date = datetime.datetime.fromisoformat(date)
            time_min = target_date.replace(hour=0, minute=0, second=0).isoformat() + 'Z'
            time_max = target_date.replace(hour=23, minute=59, second=59).isoformat() + 'Z'

            # Fetch events for the day
            events_result = service.events().list(
                calendarId=CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])

            if not events:
                return f"The calendar is completely free on {date}. What time would work best for you?"

            # Build response with busy times
            response = f"On {date}, the following times are already booked:\n"
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))

                # Parse and format times
                start_time = datetime.datetime.fromisoformat(start.replace('Z', '+00:00'))
                end_time = datetime.datetime.fromisoformat(end.replace('Z', '+00:00'))

                response += f"- {start_time.strftime('%I:%M %p')} to {end_time.strftime('%I:%M %p')}\n"

            response += "\nWhat time would you prefer for your appointment?"
            return response

        except HttpError as error:
            logger.error(f"Calendar API error: {error}")
            return "I'm having trouble accessing the calendar right now. Let me take your preferred time and we'll confirm availability shortly."
        except Exception as e:
            logger.error(f"Error checking availability: {e}")
            return "I'm having trouble checking availability. What time works best for you and we'll confirm it?"

    @function_tool()
    async def schedule_appointment(
        self,
        ctx: RunContext,
        caller_name: str,
        date_time: str,
        purpose: Optional[str] = None,
        phone_number: Optional[str] = None
    ) -> str:
        """
        Schedule an appointment on the calendar.

        Args:
            caller_name: Name of the person scheduling
            date_time: Date and time in ISO format (YYYY-MM-DDTHH:MM:SS) or parseable format
            purpose: Purpose of the meeting
            phone_number: Contact phone number
        """
        logger.info(f"Scheduling appointment for {caller_name} at {date_time}")

        try:
            service = self._get_calendar_service()

            # Parse the start time
            start_time = datetime.datetime.fromisoformat(date_time)
            end_time = start_time + datetime.timedelta(minutes=DEFAULT_MEETING_DURATION)

            # Use caller_phone if phone_number not provided
            contact_phone = phone_number or self.caller_phone

            # Create event
            event = {
                'summary': f'Meeting with {caller_name}',
                'description': f'Purpose: {purpose or "Not specified"}\nPhone: {contact_phone or "Not provided"}',
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

            logger.info(f"Event created: {created_event.get('htmlLink')}")

            # Store caller name and meeting date for call history
            if not self.caller_name:
                self.caller_name = caller_name
            self.meeting_date = start_time.isoformat()
            logger.info(f"✓ Meeting date set for call history: {self.meeting_date}")

            formatted_time = start_time.strftime('%A, %B %d at %I:%M %p')
            self.add_note(purpose or 'Not specified')
            return f"Perfect! I've scheduled your appointment for {formatted_time}. You should receive a confirmation shortly."

        except HttpError as error:
            logger.error(f"Calendar API error: {error}")
            return f"I've noted your request for {date_time}, but I'm having trouble accessing the calendar. Someone will call you back to confirm."
        except Exception as e:
            logger.error(f"Error scheduling appointment: {e}")
            return f"I've recorded your appointment request. Someone will call you back to confirm the details."

    @function_tool()
    async def take_message(
        self,
        ctx: RunContext,
        caller_name: str,
        phone_number: Optional[str] = None,
        message: Optional[str] = None,
        preferred_date: Optional[str] = None,
        preferred_time: Optional[str] = None
    ) -> str:
        """Record a message or meeting request from the caller. Use this when caller wants to schedule a meeting or leave a message."""
        logger.info(f"Taking message from {caller_name}")
        logger.info(f"Phone: {phone_number or self.caller_phone or 'Not provided'}")
        logger.info(f"Message: {message or 'Meeting request'}")
        logger.info(f"Preferred date: {preferred_date or 'Not specified'}")
        logger.info(f"Preferred time: {preferred_time or 'Not specified'}")

        # Store caller name for call history
        if not self.caller_name:
            self.caller_name = caller_name

        # Add note about the message - just the message content
        self.add_note(message or 'Meeting request')

        # In the future, this would integrate with a scheduling system or database
        response = f"Thank you, {caller_name}. I've recorded your "

        if preferred_date or preferred_time:
            response += "meeting request"
            if preferred_date:
                response += f" for {preferred_date}"
            if preferred_time:
                response += f" at {preferred_time}"
        else:
            response += "message"

        response += ". Someone from our team will call you back shortly to confirm."

        return response


async def write_call_history_to_supabase(
    phone_number: Optional[str],
    caller_name: Optional[str],
    meeting_date: Optional[str],
    notes: list[str]
):
    """Write call history to Supabase call_history table."""
    try:
        # Initialize Supabase client
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Combine notes into a single string
        notes_text = "; ".join(notes) if notes else "No specific notes"

        # Prepare data for insertion
        call_data = {
            "notes": notes_text,
            "phone_number": phone_number,
            "name": caller_name,
            "meeting_date": meeting_date
        }

        logger.info(f"Writing call history to Supabase: {call_data}")

        # Insert into call_history table
        result = supabase.table("call_history").insert(call_data).execute()

        logger.info(f"✓ Call history written successfully: {result}")

    except Exception as e:
        logger.error(f"Failed to write call history to Supabase: {e}")


async def entrypoint(ctx: JobContext):
    logger.info(f"Starting receptionist agent for room: {ctx.room.name}")

    # Create custom agent instance
    receptionist_agent = ReceptionistAgent()

    # Extract caller phone number from room name or participant metadata
    caller_phone = None
    try:
        logger.info(f"=== CALLER ID EXTRACTION ===" )
        logger.info(f"Room name: '{ctx.room.name}'")
        logger.info(f"Remote participants count: {len(ctx.room.remote_participants) if ctx.room.remote_participants else 0}")

        # Method 1: Check room name for caller info when coming from SIP
        import re
        phone_match = re.search(r'_(\+\d{11,15})_', ctx.room.name)
        if phone_match:
            caller_phone = phone_match.group(1)
            logger.info(f"✓ Extracted caller phone from room name: {caller_phone}")
        else:
            # Fallback: try broader pattern
            phone_match = re.search(r'(\+\d{11,15})', ctx.room.name)
            if phone_match:
                caller_phone = phone_match.group(1)
                logger.info(f"✓ Extracted caller phone from room name (fallback): {caller_phone}")

        # Method 2: Check for participants and their metadata (for SIP headers)
        if ctx.room.remote_participants:
            for i, participant in enumerate(ctx.room.remote_participants.values()):
                logger.info(f"--- Participant {i+1} ---")
                logger.info(f"Identity: '{participant.identity}'")
                logger.info(f"Metadata: '{participant.metadata}'")

                # Check participant identity for phone number
                if hasattr(participant, 'identity') and participant.identity:
                    phone_match = re.search(r'(\+?1?\d{10,15})', participant.identity)
                    if phone_match:
                        caller_phone = phone_match.group(1)
                        logger.info(f"✓ Extracted caller phone from participant identity: {caller_phone}")
                        break

                # Check participant metadata if available
                if hasattr(participant, 'metadata') and participant.metadata:
                    if 'X-From' in str(participant.metadata) or 'from' in str(participant.metadata).lower():
                        phone_match = re.search(r'(\+?1?\d{10,15})', str(participant.metadata))
                        if phone_match:
                            caller_phone = phone_match.group(1)
                            logger.info(f"✓ Extracted caller phone from participant metadata: {caller_phone}")
                            break
        else:
            logger.info("No remote participants found yet")

        # Set caller phone on the agent if found
        if caller_phone:
            receptionist_agent.caller_phone = caller_phone
            logger.info(f"✓ Auto-populated caller phone: {caller_phone}")
        else:
            logger.info("❌ No caller phone number detected")

        logger.info(f"=== END CALLER ID EXTRACTION ===")

    except Exception as e:
        logger.warning(f"Could not extract caller phone number: {e}")

    # Create AgentSession with voice pipeline components
    session = AgentSession(
        stt=deepgram.STT(
            model="nova-3",
            language="en",
        ),
        llm=openai.LLM(
            model="gpt-4o-mini",
            temperature=0.6,
        ),
        # llm=google.LLM(
        #     model="gemini-2.5-flash",
        # ),
        tts=cartesia.TTS(
            model="sonic-2",
            voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",  # Professional voice
        ),
        # tts=cartesia.TTS(
        #     model="sonic-2",
        #     voice="5755fb89-cd45-4871-bb79-acdb878c8af6",  # Professional voice
        # ),
        # tts=elevenlabs.TTS(
        #     voice_id="cNYrMw9glwJZXR8RwbuR",
        #     model="eleven_multilingual_v2"
        # ),
        vad=silero.VAD.load(),
    )

    # Start the session with the custom agent
    await session.start(
        room=ctx.room,
        agent=receptionist_agent,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    # Generate initial greeting
    await session.generate_reply(
        instructions=f"Greet the caller: 'Thank you for calling {BUSINESS_NAME}, how may I assist you today?'"
    )

    # Register callback to write to Supabase when call ends
    @ctx.room.on("participant_disconnected")
    def on_disconnect(participant):
        logger.info("Participant disconnected, writing to Supabase...")
        import asyncio
        asyncio.create_task(write_call_history_to_supabase(
            phone_number=receptionist_agent.caller_phone,
            caller_name=receptionist_agent.caller_name,
            meeting_date=receptionist_agent.meeting_date,
            notes=receptionist_agent.call_notes
        ))


if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="inbound-agent"))
