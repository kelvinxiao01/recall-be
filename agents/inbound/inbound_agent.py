from dotenv import load_dotenv
import logging
import os
from typing import Optional
from livekit import agents
from livekit.agents import JobContext, WorkerOptions, AgentSession, Agent, RunContext, function_tool
from livekit.plugins import deepgram, openai, cartesia, silero

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Business configuration
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Your Business")
BUSINESS_HOURS = os.getenv("BUSINESS_HOURS", "Mon-Fri 9AM-5PM")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "+1234567890")

SYSTEM_INSTRUCTIONS = f"""You are a professional receptionist for {BUSINESS_NAME}.

Your role is to:
- Answer calls professionally and courteously
- Greet callers with the business name
- Help with meeting scheduling requests
- Provide business information like hours ({BUSINESS_HOURS}) and phone number ({BUSINESS_PHONE})
- Take messages for staff members
- Answer basic questions about the business

Always be:
- Professional and empathetic
- Clear and concise
- Patient with caller questions
- Helpful within your capabilities

IMPORTANT CONVERSATION FLOW:
1. When callers want to schedule a meeting, collect their information:
   - Name
   - Phone number (if not auto-detected)
   - Preferred date/time
   - Purpose of meeting
2. Use take_message to record their information
3. Inform them that someone will call them back to confirm the appointment

Start each call by greeting the caller: "Thank you for calling {BUSINESS_NAME}, how may I help you today?"
"""


class ReceptionistAgent(Agent):
    def __init__(self):
        super().__init__(instructions=SYSTEM_INSTRUCTIONS)
        self.caller_phone = None

    @function_tool()
    async def get_business_hours(self, ctx: RunContext) -> str:
        """Get the business hours and availability information."""
        logger.info("Caller requested business hours")
        return f"Our business hours are {BUSINESS_HOURS}. We're happy to schedule a meeting during these times."

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
