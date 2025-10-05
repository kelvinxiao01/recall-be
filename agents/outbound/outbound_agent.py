from __future__ import annotations

import asyncio
import logging
import os
import json
import datetime
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from livekit import agents, api, rtc
from livekit.agents import (
    JobContext,
    WorkerOptions,
    AgentSession,
    Agent,
    RunContext,
    function_tool,
    get_job_context,
    cli,
    RoomInputOptions
)
from livekit.plugins import deepgram, openai, cartesia, silero, google
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from supabase import create_client, Client

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-reminder-agent")

# Business configuration
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "John Doe Legal")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "+19297173949")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "Mon-Fri 9AM-5PM")
outbound_trunk_id = os.getenv("OUTBOUND_SIP_TRUNK_ID")
twilio_caller_id = os.getenv("TWILIO_CALLER_ID")

# Google Calendar configuration
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DEFAULT_MEETING_DURATION = int(os.getenv("DEFAULT_MEETING_DURATION", "60"))

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SK")  # Use secret key for backend operations

OUTBOUND_SYSTEM_INSTRUCTIONS = f"""You are calling customers on behalf of {BUSINESS_NAME} to follow up on missed appointments.

IMPORTANT: Today's date is {datetime.datetime.now().strftime('%Y-%m-%d')} ({datetime.datetime.now().strftime('%A, %B %d, %Y')}).
When scheduling appointments, always use the year 2025 unless the caller explicitly specifies a different year.

Your role is to:
- Politely inform customers they missed their scheduled appointment
- Apologize for any inconvenience and offer to reschedule
- Help them find a new appointment time
- Answer questions about the appointment
- IMMEDIATELY detect if you've reached voicemail and hang up

CRITICAL VOICEMAIL DETECTION - CALL detected_answering_machine() IMMEDIATELY IF YOU HEAR:
- "Thanks for the call" - VOICEMAIL DETECTED
- "Configure your number's voice URL" - VOICEMAIL DETECTED
- "to change this message" - VOICEMAIL DETECTED
- "leave a message" or "after the beep" - VOICEMAIL DETECTED
- Any automated greeting or robotic voice - VOICEMAIL DETECTED
- ANY pre-recorded message - VOICEMAIL DETECTED

YOU MUST call the detected_answering_machine() function the INSTANT you hear any voicemail indicators!

IMPORTANT CONVERSATION BEHAVIOR:
- WAIT for the other party to speak first before saying anything
- Listen carefully to determine if it's a real person or voicemail
- If you detect voicemail phrases, IMMEDIATELY call detected_answering_machine() - DO NOT CONTINUE TALKING
- If it's a real person, then identify yourself: "Hello, this is {BUSINESS_NAME} calling about your missed appointment"
- State the purpose clearly and provide original meeting details
- If customer wants to reschedule, collect their preferred date/time and use schedule_appointment() to book it on the calendar
- You can optionally use get_available_slots() to check availability for a specific date
- DO NOT ask for phone number - it is automatically detected from the call

Keep conversations SHORT and to the point. Most calls should be under 2 minutes.

Business Info:
- Phone: {BUSINESS_PHONE}
- Hours: {BUSINESS_HOURS}
"""


class OutboundReminderAgent(Agent):
    def __init__(self, meeting_data: Dict[str, Any]):
        super().__init__(instructions=OUTBOUND_SYSTEM_INSTRUCTIONS)
        self.meeting_data = meeting_data
        self.call_completed = False
        self.voicemail_detected = False
        self._calendar_service = None
        self.call_notes = []  # Track important events during the call

        # Parse meeting information from metadata
        self.customer_phone = meeting_data.get('phone_number', 'Unknown')
        self.customer_name = meeting_data.get('customer_name', 'the customer')
        self.meeting_date = meeting_data.get('meeting_date', 'your scheduled time')
        self.meeting_time = meeting_data.get('meeting_time', '')
        self.meeting_purpose = meeting_data.get('meeting_purpose', 'your meeting')
        self.new_meeting_date = None  # Will store rescheduled meeting time

        # Keep reference to participant for call management
        self.participant: rtc.RemoteParticipant | None = None

        logger.info(f"OutboundReminderAgent initialized for {self.customer_phone}")

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

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        """Helper function to hang up the call by deleting the room"""
        job_ctx = get_job_context()
        await job_ctx.api.room.delete_room(
            api.DeleteRoomRequest(
                room=job_ctx.room.name,
            )
        )

    @function_tool()
    async def get_meeting_details(self, ctx: RunContext) -> str:
        """Get the original missed meeting details to share with the customer if they ask"""
        logger.info("Customer requested meeting details")

        details = f"Your original meeting was scheduled"
        if self.meeting_date:
            details += f" for {self.meeting_date}"
        if self.meeting_time:
            details += f" at {self.meeting_time}"
        if self.meeting_purpose:
            details += f" regarding {self.meeting_purpose}"

        return details

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
    async def confirm_meeting(self, ctx: RunContext) -> str:
        """Call this when customer confirms they will attend the meeting"""
        logger.info("Customer confirmed they will attend the meeting")
        self.call_completed = True
        self.add_note("Confirmed attendance for original appointment")

        # Write to Supabase before ending call
        await write_call_history_to_supabase(
            phone_number=self.customer_phone,
            caller_name=self.customer_name,
            meeting_date=self.meeting_date,  # Keep original meeting date
            notes=self.call_notes
        )

        # End the call after confirmation
        await ctx.wait_for_playout()
        await self.hangup()

        return "Great! We look forward to seeing you. Thank you!"

    @function_tool()
    async def schedule_appointment(
        self,
        ctx: RunContext,
        date_time: str,
        purpose: Optional[str] = None
    ) -> str:
        """
        Schedule a new appointment on the calendar to replace the missed one.

        Args:
            date_time: Date and time in ISO format (YYYY-MM-DDTHH:MM:SS) or parseable format
            purpose: Purpose of the meeting (optional, will use original purpose if not provided)
        """
        logger.info(f"Rescheduling appointment for {self.customer_name} at {date_time}")

        try:
            service = self._get_calendar_service()

            # Parse the start time
            start_time = datetime.datetime.fromisoformat(date_time)
            end_time = start_time + datetime.timedelta(minutes=DEFAULT_MEETING_DURATION)

            # Use original purpose if not provided
            meeting_purpose = purpose or self.meeting_purpose

            # Create event
            event = {
                'summary': f'Meeting with {self.customer_name}',
                'description': f'Purpose: {meeting_purpose}\nPhone: {self.customer_phone}\nRescheduled from: {self.meeting_date}',
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': 'America/New_York',
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': 'America/New_York',
                },
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

            # Store new meeting date for call history (as ISO timestamp)
            self.new_meeting_date = start_time.isoformat()
            logger.info(f"✓ New meeting date set for call history: {self.new_meeting_date}")

            formatted_time = start_time.strftime('%A, %B %d at %I:%M %p')
            self.add_note(f"Rescheduled from {self.meeting_date} to {formatted_time}. Purpose: {meeting_purpose}")

            # Write to Supabase with new meeting date
            await write_call_history_to_supabase(
                phone_number=self.customer_phone,
                caller_name=self.customer_name,
                meeting_date=self.new_meeting_date,  # Use new rescheduled date
                notes=self.call_notes
            )

            return f"Perfect! I've rescheduled your appointment for {formatted_time}. You should receive a confirmation shortly. Is there anything else I can help you with?"

        except HttpError as error:
            logger.error(f"Calendar API error: {error}")
            return f"I've noted your preferred time of {date_time}, but I'm having trouble accessing the calendar. Someone will call you back to confirm the rescheduled appointment."
        except Exception as e:
            logger.error(f"Error scheduling appointment: {e}")
            return f"I've recorded your rescheduling request. Someone will call you back to confirm the new appointment details."

    @function_tool()
    async def detected_answering_machine(self, ctx: RunContext) -> str:
        """URGENT: Call this tool IMMEDIATELY when you hear ANY voicemail phrases like 'Thanks for the call', 'Configure your number', 'leave a message', or ANY automated greeting. DO NOT continue talking - just call this function instantly!"""
        logger.info("Voicemail detected by agent - hanging up immediately")
        self.voicemail_detected = True
        self.call_completed = True
        self.add_note("Voicemail detected - no answer")

        # Write to Supabase before ending call
        await write_call_history_to_supabase(
            phone_number=self.customer_phone,
            caller_name=self.customer_name,
            meeting_date=None,  # No new meeting date for voicemail
            notes=self.call_notes
        )

        # Hang up immediately without leaving any message
        await self.hangup()

        return "Voicemail detected - hung up immediately"

    @function_tool()
    async def end_call_successful(self, ctx: RunContext) -> str:
        """Call this when the conversation is complete and customer is informed"""
        logger.info("Call completed successfully")
        self.call_completed = True
        self.add_note("Call completed successfully")

        # Write to Supabase before ending call
        # Use new_meeting_date if rescheduled, otherwise None
        await write_call_history_to_supabase(
            phone_number=self.customer_phone,
            caller_name=self.customer_name,
            meeting_date=self.new_meeting_date,  # Will be None if not rescheduled
            notes=self.call_notes
        )

        # Wait for final message to play out, then hang up
        await ctx.wait_for_playout()
        await self.hangup()

        return "Thank you! Have a great day!"


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
    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect()

    # Extract meeting data from job metadata (passed from agent dispatch)
    meeting_data = {}
    phone_number = None

    try:
        if ctx.job.metadata:
            meeting_data = json.loads(ctx.job.metadata)
            phone_number = meeting_data.get("phone_number")
            logger.info(f"Extracted meeting data from job metadata: {meeting_data}")
        else:
            logger.error("No job metadata available")
            ctx.shutdown()
            return

    except Exception as e:
        logger.error(f"Error extracting meeting data from job metadata: {e}")
        ctx.shutdown()
        return

    if not phone_number:
        logger.error("No phone number provided for outbound call")
        ctx.shutdown()
        return

    # Get trunk ID from metadata if available, fallback to environment variable
    trunk_id = meeting_data.get("sip_trunk_id") or outbound_trunk_id
    if not trunk_id:
        logger.error("No SIP trunk ID configured in metadata or environment")
        ctx.shutdown()
        return

    # Get caller ID from metadata or environment variable
    caller_id = meeting_data.get("caller_id") or twilio_caller_id
    if not caller_id:
        logger.error("No caller ID configured. Set TWILIO_CALLER_ID environment variable with your authorized Twilio phone number")
        ctx.shutdown()
        return

    logger.info(f"📱 Using caller ID: {caller_id}")

    # Create the outbound reminder agent
    agent = OutboundReminderAgent(meeting_data)
    participant_identity = phone_number

    # Create agent session with voice pipeline components
    session = AgentSession(
        stt=deepgram.STT(
            model="nova-3",
            language="en",
        ),
        llm=openai.LLM(
            model="gpt-4o-mini",
            temperature=0.3,
        ),
        # llm=google.LLM(
        #     model="gemini-2.5-flash",
        # ),
        tts=cartesia.TTS(
            model="sonic-2",
            voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",  # Professional voice
        ),
        vad=silero.VAD.load(),
    )

    # Start the session first before dialing, to ensure that when the user picks up
    # the agent does not miss anything the user says
    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(
                # Enable noise cancellation if available
                # noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        )
    )

    # `create_sip_participant` starts dialing the user
    try:
        logger.info(f"🔥 OUTBOUND CALL DEBUG - Starting call process")
        logger.info(f"📞 Target: {phone_number}")
        logger.info(f"🏢 Trunk ID: {trunk_id}")
        logger.info(f"🏠 Room: {ctx.room.name}")
        logger.info(f"👤 Participant Identity: {participant_identity}")
        logger.info(f"🔗 LiveKit URL: {os.getenv('LIVEKIT_URL')}")

        # Log the complete SIP request details
        sip_request = api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=trunk_id,
            sip_call_to=phone_number,
            sip_number=caller_id,  # CRITICAL: Set the authorized caller ID
            participant_identity=participant_identity,
            participant_name=BUSINESS_NAME,
            # Use wait_until_answered to ensure we get a real person
            wait_until_answered=True,
        )

        logger.info(f"🛠️ SIP Request Details:")
        logger.info(f"   room_name: {sip_request.room_name}")
        logger.info(f"   sip_trunk_id: {sip_request.sip_trunk_id}")
        logger.info(f"   sip_call_to: {sip_request.sip_call_to}")
        logger.info(f"   sip_number: {sip_request.sip_number}")
        logger.info(f"   participant_identity: {sip_request.participant_identity}")
        logger.info(f"   participant_name: {sip_request.participant_name}")
        logger.info(f"   wait_until_answered: {sip_request.wait_until_answered}")

        logger.info(f"🚀 Creating SIP participant - initiating call...")

        # Create SIP participant with improved voicemail detection settings
        sip_task = asyncio.create_task(
            ctx.api.sip.create_sip_participant(sip_request)
        )

        # Wait for SIP participant creation with a longer timeout since we're waiting for answer
        try:
            logger.info(f"⏱️ Waiting for SIP participant creation (60s timeout)...")
            sip_participant = await asyncio.wait_for(sip_task, timeout=60.0)
            logger.info(f"✅ SIP participant created successfully!")
            logger.info(f"🎉 Customer answered! Call connected.")
            logger.info(f"📊 SIP Participant Details:")
            logger.info(f"   Identity: {sip_participant.participant_identity if hasattr(sip_participant, 'participant_identity') else 'N/A'}")
            logger.info(f"   SIP Call ID: {sip_participant.sip_call_id if hasattr(sip_participant, 'sip_call_id') else 'N/A'}")
        except asyncio.TimeoutError:
            logger.error("❌ TIMEOUT: Call was not answered within 60 seconds")
            logger.error("   This could indicate:")
            logger.error("   1. SIP routing issue between LiveKit and Twilio")
            logger.error("   2. Twilio BYOC trunk misconfiguration")
            logger.error("   3. Target number unreachable/busy")
            sip_task.cancel()
            ctx.shutdown()
            return

        # Wait for the agent session start
        await session_started

        # Since wait_until_answered=True, the participant should be ready
        try:
            participant = await asyncio.wait_for(
                ctx.wait_for_participant(identity=participant_identity),
                timeout=10.0  # Short timeout since call should already be answered
            )
            logger.info(f"Customer answered! Participant joined: {participant.identity}")

            agent.set_participant(participant)

            # Wait briefly to detect if this is voicemail vs real person
            logger.info("Waiting to detect if voicemail or real person...")
            await asyncio.sleep(2.0)  # Give time for voicemail greeting to start

            # Don't generate initial greeting immediately - let the person/voicemail speak first
            # The agent will respond based on what it hears

        except asyncio.TimeoutError:
            logger.info("Customer did not answer within 60 seconds - ending call")
            ctx.shutdown()
            return

    except api.TwirpError as e:
        logger.error(f"🚨 TWIRP ERROR - SIP participant creation failed!")
        logger.error(f"   Error message: {e.message}")
        logger.error(f"   SIP status code: {e.metadata.get('sip_status_code', 'N/A')}")
        logger.error(f"   SIP status: {e.metadata.get('sip_status', 'N/A')}")
        logger.error(f"   Full metadata: {e.metadata}")
        logger.error("   📋 Troubleshooting steps:")
        logger.error("   1. Check LiveKit SIP trunk configuration")
        logger.error("   2. Verify Twilio BYOC trunk settings")
        logger.error("   3. Confirm SIP credentials are correct")
        logger.error("   4. Check LiveKit → Twilio routing")
        ctx.shutdown()
    except Exception as e:
        logger.error(f"💥 UNEXPECTED ERROR during outbound call!")
        logger.error(f"   Error type: {type(e).__name__}")
        logger.error(f"   Error details: {str(e)}")
        logger.error(f"   Phone: {phone_number}")
        logger.error(f"   Trunk: {trunk_id}")
        logger.error(f"   Room: {ctx.room.name}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        ctx.shutdown()


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
