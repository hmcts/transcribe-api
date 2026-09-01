# ruff: noqa: T201

import asyncio

import pytest

from transcribe_api.domain.models_dictation import DialogueEntry
from transcribe_api.documents.minutes.templates.crissa import generate_full_crissa


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generate_full_crissa():
    """Test the generate_full_crissa function with sample data."""

    # Create sample dialogue entries
    sample_dialogue_entries = [
        DialogueEntry(
            speaker="Probation Officer",
            text="Good morning, Mr. Smith. Thank you for coming in today. How have you been since our last meeting?",
            start_time=0.0,
            end_time=5.2,
        ),
        DialogueEntry(
            speaker="Mr. Smith",
            text="I've been doing well, thank you. I've been keeping up with my community service hours and staying out of trouble.",
            start_time=5.5,
            end_time=12.1,
        ),
        DialogueEntry(
            speaker="Probation Officer",
            text="That's excellent to hear. Let's review your progress. You've completed 40 hours of your 120-hour community service requirement. How are you finding the work at the local charity?",
            start_time=12.4,
            end_time=22.8,
        ),
        DialogueEntry(
            speaker="Mr. Smith",
            text="It's been really positive actually. I'm helping with their food bank distribution, and it feels good to be giving back to the community. The staff there are very supportive.",
            start_time=23.1,
            end_time=32.5,
        ),
        DialogueEntry(
            speaker="Probation Officer",
            text="That's wonderful. Now, let's discuss your employment situation. Have there been any changes since our last meeting?",
            start_time=32.8,
            end_time=39.2,
        ),
        DialogueEntry(
            speaker="Mr. Smith",
            text="Yes, I started a new job at a warehouse last week. It's full-time, Monday to Friday, and the pay is decent. My supervisor knows about my situation and has been understanding.",
            start_time=39.5,
            end_time=49.8,
        ),
        DialogueEntry(
            speaker="Probation Officer",
            text="Excellent progress. For our next steps, I'd like you to continue with your community service schedule. Can you commit to completing another 20 hours by our next appointment in two weeks?",
            start_time=50.1,
            end_time=61.3,
        ),
        DialogueEntry(
            speaker="Mr. Smith",
            text="Absolutely, I can do that. I'll speak with the charity coordinator to arrange the additional hours around my work schedule.",
            start_time=61.6,
            end_time=69.2,
        ),
        DialogueEntry(
            speaker="Probation Officer",
            text="Perfect. Let's schedule our next appointment for the same time in two weeks. Keep up the excellent work, Mr. Smith.",
            start_time=69.5,
            end_time=76.8,
        ),
    ]

    # Test email
    test_user_email = "test@example.com"

    print("🚀 Starting CRISSA generation test...")
    print(f"📝 Sample dialogue entries: {len(sample_dialogue_entries)} entries")
    print(f"👤 Test user email: {test_user_email}")
    print("-" * 50)

    try:
        # Call the function
        result = await generate_full_crissa(
            dialogue_entries=sample_dialogue_entries,
            user_email=test_user_email,
            # Optional parameters with defaults:
            # prompt_version=None,
            # model_name=LLMModel.VERTEX_GEMINI_25_PRO,
            # temperature=0.1
        )

        print("✅ CRISSA generation completed successfully!")
        print("-" * 50)
        print("📄 Generated CRISSA Minutes:")
        print(result)
        print("-" * 50)
        print(f"📊 Result length: {len(result)} characters")

    except Exception as e:
        print(f"❌ Error during CRISSA generation: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    # Run the async test function
    asyncio.run(test_generate_full_crissa())
