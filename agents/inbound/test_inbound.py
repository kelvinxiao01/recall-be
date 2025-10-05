from dotenv import load_dotenv
import logging
import os
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from livekit import agents
from livekit.agents import JobContext, WorkerOptions, AgentSession, Agent, RunContext, function_tool
from livekit.plugins import deepgram, openai, cartesia, silero

# Google Calendar imports
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Business configuration
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Your Business")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "Mon-Fri 9AM-5PM")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "+1234567890")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DEFAULT_MEETING_DURATION = int(os.getenv("DEFAULT_MEETING_DURATION", "60"))  # minutes

# Business hours configuration (24-hour format)
BUSINESS_START_HOUR = 9  # 9 AM
BUSINESS_END_HOUR = 17   # 5 PM
BUSINESS_DAYS = [0, 1, 2, 3, 4]  # Monday to Friday (0=Monday, 6=Sunday)

SYSTEM_INSTRUCTIONS = f"""You are a professional receptionist for {BUSINESS_NAME}.

Your role is to:
- Answer calls professionally and courteously
- Greet callers with the business name
- Help with meeting scheduling requests by checking real calendar availability
- Provide business information like hours ({BUSINESS_HOURS}) and phone number ({BUSINESS_PHONE})
- Take messages for staff members
- Answer basic questions about the business

Always be:
- Professional and empathetic
- Clear and concise
- Patient with caller questions
- Helpful within your capabilities

IMPORTANT CONVERSATION FLOW FOR SCHEDULING:
1. When callers want to schedule a meeting, ask for their preferred date and time
2. Use check_availability to verify if that time slot is available
3. If the slot is NOT available, use find_next_available_slot to suggest alternatives
4. Once a suitable time is found, collect their information:
   - Name
   - Phone number (if not auto-detected)
   - Purpose of meeting
5. Use schedule_meeting to book the appointment
6. Confirm the appointment details with the caller

Start each call by greeting the caller: "Thank you for calling {BUSINESS_NAME}, how may I help you today?"
"""


class CalendarManager:
    """Manages Google Calendar operations"""
    
    def __init__(self):
        self.service = None
        self._initialize_calendar()
    
    def _initialize_calendar(self):
        """Initialize Google Calendar API connection"""
        try:
            # Use service account credentials
            creds = service_account.Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=['https://www.googleapis.com/auth/calendar']
            )
            self.service = build('calendar', 'v3', credentials=creds)
            logger.info("✓ Google Calendar API initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Google Calendar API: {e}")
            logger.error("Make sure you have a valid service account JSON file")
    
    def check_availability(self, start_time: datetime, duration_minutes: int = DEFAULT_MEETING_DURATION) -> bool:
        """Check if a time slot is available"""
        if not self.service:
            logger.error("Calendar service not initialized")
            return False
        
        try:
            end_time = start_time + timedelta(minutes=duration_minutes)
            
            # Query for events in the time range
            events_result = self.service.events().list(
                calendarId=CALENDAR_ID,
                timeMin=start_time.isoformat() + 'Z',
                timeMax=end_time.isoformat() + 'Z',
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            # If no events found, the slot is available
            is_available = len(events) == 0
            logger.info(f"Availability check: {start_time} - {'Available' if is_available else 'Busy'}")
            return is_available
            
        except HttpError as error:
            logger.error(f"Calendar API error: {error}")
            return False
    
    def find_next_available_slots(self, preferred_date: datetime, num_slots: int = 3) -> List[Dict]:
        """Find the next available time slots starting from preferred date"""
        if not self.service:
            return []
        
        available_slots = []
        current_date = preferred_date.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
        max_days_ahead = 14  # Look up to 2 weeks ahead
        days_checked = 0
        
        while len(available_slots) < num_slots and days_checked < max_days_ahead:
            # Skip if not a business day
            if current_date.weekday() not in BUSINESS_DAYS:
                current_date += timedelta(days=1)
                days_checked += 1
                continue
            
            # Check each hour slot during business hours
            for hour in range(BUSINESS_START_HOUR, BUSINESS_END_HOUR):
                slot_time = current_date.replace(hour=hour, minute=0)
                
                # Skip past times
                if slot_time <= datetime.now():
                    continue
                
                if self.check_availability(slot_time, DEFAULT_MEETING_DURATION):
                    available_slots.append({
                        'datetime': slot_time,
                        'formatted': slot_time.strftime('%A, %B %d at %I:%M %p')
                    })
                    
                    if len(available_slots) >= num_slots:
                        break
            
            current_date += timedelta(days=1)
            days_checked += 1
        
        return available_slots
    
    def create_event(self, start_time: datetime, caller_name: str, phone_number: str, 
                     purpose: str, duration_minutes: int = DEFAULT_MEETING_DURATION) -> Optional[str]:
        """Create a calendar event and return the event ID"""
        if not self.service:
            logger.error("Calendar service not initialized")
            return None
        
        try:
            end_time = start_time + timedelta(minutes=duration_minutes)
            
            event = {
                'summary': f'Meeting with {caller_name}',
                'description': f'Purpose: {purpose}\nPhone: {phone_number}',
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
            
            created_event = self.service.events().insert(
                calendarId=CALENDAR_ID,
                body=event
            ).execute()
            
            logger.info(f"✓ Event created: {created_event.get('id')}")
            return created_event.get('id')
            
        except HttpError as error:
            logger.error(f"Failed to create calendar event: {error}")
            return None


class ReceptionistAgent(Agent):
    def __init__(self):
        super().__init__(instructions=SYSTEM_INSTRUCTIONS)
        self.caller_phone = None
        self.calendar_manager = CalendarManager()

    @function_tool()
    async def get_business_hours(self, ctx: RunContext) -> str:
        """Get the business hours and availability information."""
        logger.info("Caller requested business hours")
        return f"Our business hours are {BUSINESS_HOURS}. We're happy to schedule a meeting during these times."

    @function_tool()
    async def check_availability(
        self,
        ctx: RunContext,
        date: str,
        time: str
    ) -> str:
        """Check if a specific date and time is available for scheduling. 
        Args:
            date: Date in format 'YYYY-MM-DD' or natural language like 'tomorrow', 'next Monday'
            time: Time in format like '2:00 PM', '14:00', '2pm'
        """
        try:
            # Parse the date and time
            from dateutil import parser
            datetime_str = f"{date} {time}"
            appointment_time = parser.parse(datetime_str)
            
            # Check if it's during business hours
            if appointment_time.weekday() not in BUSINESS_DAYS:
                return f"Sorry, we're closed on {appointment_time.strftime('%A')}s. We're open {BUSINESS_HOURS}."
            
            if appointment_time.hour < BUSINESS_START_HOUR or appointment_time.hour >= BUSINESS_END_HOUR:
                return f"Sorry, that time is outside our business hours ({BUSINESS_HOURS})."
            
            # Check calendar availability
            is_available = self.calendar_manager.check_availability(appointment_time)
            
            if is_available:
                return f"Great news! {appointment_time.strftime('%A, %B %d at %I:%M %p')} is available."
            else:
                return f"I'm sorry, {appointment_time.strftime('%A, %B %d at %I:%M %p')} is already booked."
                
        except Exception as e:
            logger.error(f"Error checking availability: {e}")
            return "I'm having trouble checking that date and time. Could you please repeat it?"

    @function_tool()
    async def find_next_available_slot(
        self,
        ctx: RunContext,
        preferred_date: Optional[str] = None
    ) -> str:
        """Find the next available appointment slots. Use this when the requested time is not available.
        Args:
            preferred_date: Starting date to search from (optional, defaults to today)
        """
        try:
            from dateutil import parser
            
            if preferred_date:
                start_date = parser.parse(preferred_date)
            else:
                start_date = datetime.now()
            
            available_slots = self.calendar_manager.find_next_available_slots(start_date, num_slots=3)
            
            if not available_slots:
                return "I'm sorry, I couldn't find any available slots in the next two weeks. Let me take your information and someone will call you back to find a suitable time."
            
            response = "Here are the next available times:\n"
            for i, slot in enumerate(available_slots, 1):
                response += f"{i}. {slot['formatted']}\n"
            
            response += "\nWhich of these times works best for you?"
            return response
            
        except Exception as e:
            logger.error(f"Error finding available slots: {e}")
            return "I'm having trouble finding available times. Let me take your information and someone will call you back."

    @function_tool()
    async def schedule_meeting(
        self,
        ctx: RunContext,
        caller_name: str,
        date: str,
        time: str,
        purpose: str,
        phone_number: Optional[str] = None
    ) -> str:
        """Schedule a confirmed meeting appointment in the calendar.
        Args:
            caller_name: Name of the caller
            date: Date in format 'YYYY-MM-DD' or natural language
            time: Time in format like '2:00 PM'
            purpose: Purpose or reason for the meeting
            phone_number: Contact phone number (optional if auto-detected)
        """
        try:
            from dateutil import parser
            
            datetime_str = f"{date} {time}"
            appointment_time = parser.parse(datetime_str)
            phone = phone_number or self.caller_phone or "Not provided"
            
            # Double-check availability
            if not self.calendar_manager.check_availability(appointment_time):
                return "I apologize, but that time slot was just booked. Let me find another available time for you."
            
            # Create the calendar event
            event_id = self.calendar_manager.create_event(
                start_time=appointment_time,
                caller_name=caller_name,
                phone_number=phone,
                purpose=purpose
            )
            
            if event_id:
                response = f"Perfect! I've scheduled your appointment for {appointment_time.strftime('%A, %B %d at %I:%M %p')}. "
                response += f"You'll receive a confirmation, and we look forward to meeting with you, {caller_name}!"
                logger.info(f"✓ Meeting scheduled: {caller_name} on {appointment_time} (Event ID: {event_id})")
                return response
            else:
                return "I've recorded your meeting request, but had trouble adding it to the calendar. Someone will call you back to confirm."
                
        except Exception as e:
            logger.error(f"Error scheduling meeting: {e}")
            return "I've recorded your information. Someone from our team will call you back to confirm the appointment."

    @function_tool()
    async def take_message(
        self,
        ctx: RunContext,
        caller_name: str,
        phone_number: Optional[str] = None,
        message: Optional[str] = None
    ) -> str:
        """Record a general message from the caller (not for meeting scheduling)."""
        logger.info(f"Taking message from {caller_name}")
        logger.info(f"Phone: {phone_number or self.caller_phone or 'Not provided'}")
        logger.info(f"Message: {message or 'General inquiry'}")
        
        return f"Thank you, {caller_name}. I've recorded your message. Someone from our team will call you back shortly."


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
        tts=cartesia.TTS(
            model="sonic-2",
            voice="098d5e5f-9ba9-486d-873e-4b1943f20d62",  # Professional voice
        ),
        vad=silero.VAD.load(),
    )

    # Start the session with the custom agent
    await session.start(
        room=ctx.room,
        agent=receptionist_agent,
    )

    # Generate initial greeting
    await session.generate_reply(
        instructions=f"Greet the caller: 'Thank you for calling {BUSINESS_NAME}, how may I help you today?'"
    )


if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="inbound-agent"))