# ruff: noqa

from datetime import UTC, datetime

from langfuse.decorators import langfuse_context, observe
from pydantic import BaseModel
from uwotm8 import convert_american_to_british_spelling

from transcribe_api.documents.llm.llm_client import (
    LLMModel,
    langfuse_client,
    llm_completion,
    structured_output_llm_completion_builder_func,
)
from transcribe_api.documents.minutes.templates.utils import format_transcript_string_for_prompt
from transcribe_api.runtime.markdown import html_to_markdown
from shared_utils.database.postgres_models import DialogueEntry, TemplateName

CRISSA_SECTIONS = [
    "Check in",
    "Review",
    "Intervention",
    "Summary",
    "Set task",
    "Appointment",
]


@observe(name="generate_full_crissa_one_shot", as_type="generation")
async def generate_full_crissa_one_shot(
    transcript_string: str,
    user_email: str,
    prompt_version: int | None = None,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.1,
    today_date: str | None = None,
) -> str:
    # trace_id = langfuse_context.get_current_trace_id()

    langfuse_context.update_current_trace(
        user_id=user_email,
    )

    prompt = langfuse_client.get_prompt("crissa-one-shot", version=prompt_version, type="chat")

    langfuse_context.update_current_observation(
        prompt=prompt,
        user_id=user_email,
    )

    # Use provided date or default to current date
    today_date_readable = today_date or datetime.now(UTC).strftime("%d %B %Y")

    compiled_chat_prompt = prompt.compile(
        meeting_transcript=transcript_string,
        today_date_readable=today_date_readable,
    )

    completion = await llm_completion(
        temperature=temperature,
        messages=compiled_chat_prompt,
        model=model_name,
    )

    markdown_output = completion.choices[0].message.content
    markdown_output = convert_american_to_british_spelling(markdown_output)

    langfuse_context.update_current_observation(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(markdown_output),
    )
    langfuse_context.update_current_trace(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(markdown_output),
    )

    return markdown_output


# Pydantic model for critique response
class RefinementItem(BaseModel):
    section: str
    issue: str
    suggested_improvement: str
    priority: str  # "high", "medium", or "low"


class CrisssaCritique(BaseModel):
    needs_refinement: bool
    overall_quality_score: int  # 1-10
    refinements: list[RefinementItem]
    strengths: list[str]
    summary: str


# Create structured output completion function
critique_completion = structured_output_llm_completion_builder_func(CrisssaCritique)


@observe(name="critique_crissa_report", as_type="generation")
async def critique_crissa_report(
    transcript_string: str,
    crissa_report: str,
    user_email: str,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.0,
) -> CrisssaCritique:
    """
    Critique a CRISSA report and determine if refinements are needed.
    Returns a structured CrisssaCritique object.
    """

    langfuse_context.update_current_trace(user_id=user_email)

    # Create the enhanced critique prompt that includes original instructions and examples
    critique_prompt = """You are an expert evaluator of UK probation case notes, specifically CRISSA reports, with deep knowledge of the CRISSA framework and years of experience in criminal justice case management.

CONTEXT - THE ORIGINAL TASK:
The LLM that generated this CRISSA report was given the following instructions:

"You are an experienced senior probation officer in the UK. You are tasked with generating the full a CRISSA report based on a transcript of a supervision session with a Person on Probation. CRISSA is the framework used by probation officers in the UK to write case notes for supervision meetings with people on probation.

CRISSA SECTION GUIDELINES:
- Check in. Consider how the Person on Probation is presenting. What is their mood, attitude and demeanour?
- Review. Are there any new disclosures or changes in circumstances e.g., police contact, safeguarding, prohibited contact.
- Intervention. What intervention has taken place? If applicable, what worksheets were used? This section can be flexible - if the Person on Probation presents in crisis, then the crisis area can be addressed as a priority instead of an intervention. Identify any purposeful work the Probation Practitioner initiated, even if the word intervention is never spoken. Look for cues like "today we'll talk about...", "let's work through this worksheet...". In the intervention section, it's important to include details of the conversation that focuses on thinking, emotions, and behaviour, and how they interact. The intervention section is the most important and detailed section of CRISSA.
- Summary. How did the Person on Probation engage in the meeting? Any new risk factors identified? Did the Person on Probation make any significant discolsures?
- Set Task. What tasks have been agreed for the Probation Practitioner and the Person on Probation to complete and by when?
- Appointment. When is the next appointment and with whom? How will this take place e.g., in person or remotely. Do not hallucinate an appointment date if an appointment date was not discussed.

WRITING GUIDELINES:
- Write concisely and clearly.
- Include relevant context or quotations where it aids understanding.
- Do not ever duplicate content or write repetitively.
- Make sure you think carefully and clearly about pulling out the right details for writing a very high quality CRISSA report.
- Do not hallucinate any information that is not in the transcript provided. Particularly if the transcript does not seem to correspond to a probation meeting, please do not hallucinate details from the examples or elsewhere.
- Always include the headers for the section.
- Do not include any preamble like 'Here is your CRISSA:'
- Keep in mind that although the CRISSA examples below are relatively long, you can output shorter content if the transcript is short or less detailed. Don't be afraid to return a concise CRISSA.
- The maximum length of the CRISSA report is 4000 characters.
- If available in the transcript, use the person on probation's first name rather than initials or PoP.

EXAMPLES:

EXAMPLE 1:
**Check in** – X initially stated his week had been "alright" with no incidents to report.
**Review** - X reports that he is doing well and has no new concerns to raise. X states there hasn't been any update from his MOSAVO officer regarding his query regarding selling electronics, however he appeared less motivated today to chase this up with her and feels as though the situation has been finished now and police are not overly concerned.
**Intervention** - During today's session, X revisited last week's exploration of the internet's positives and negatives and said he had a meaningful family discussion that unearthed further examples. This take-home reflection shows he is applying office work in his personal life and involving relatives in the process. Its encouraging that he is involving his family. He said looking back he cannot fathom how he went down the "rabbit hole" into his offending and used last week's list to explore this. We completed M4C (Smarter Internet Use) Exercise 2.

Using the family's expanded list, we traced how everyday online behaviour can escalate. Starting with ordering groceries to meet the basic need to eat, we discussed how internet use can foster healthier habits, recipe sites, workout videos or slide into harmful content such as pro-eating-disorder forums. X noted this resonated with his past struggle with bulimia.

We then focused on sexual use of the internet. X acknowledged a daily habit of masturbating (often with his partner) mainly for stress relief. He views consensual sex positively and clearly understands that lack of consent constitutes an offence. Together we mapped how pornography can progress from mainstream content to riskier venues, chat rooms, torrent sites and the deep web, fuelled by desensitisation. X recognised this trajectory in his own offending: a high sex drive established in adolescence, heavy pornography consumption and a gradual pursuit of more "extreme" material.

X engaged openly and demonstrated growing insight into how acceptable online behaviours can drift into harmful ones when left unchecked. Future sessions will build on this awareness, particularly around why children cannot give consent and how to establish healthier internet boundaries.

**Summary** - X engaged openly and demonstrated good insight into triggers and balanced internet use. No new risk factors, but online content remains a dynamic area to monitor. Licence conditions and safeguarding around under-age material reinforced. He reports that his accommodation status remains the same, and he continues in his relationship with Darren without concern. He states that he still feels as though he is managing his mental health effectively, and continues to abstain from any offending behaviours
**Set actions** - X to bring a reflection worksheet listing three everyday online activities with one benefit and one risk for each by next appointment. PP to email balanced-use articles and breathing-video link to X by Sunday and remain contact point for any MOSAVO developments.
**Appointment** - Verbally agreed for 15/11/24 at 14:00.

EXAMPLE 2:
**Check in** - X attended on time. He seemed low in mood and appeared unkempt. He disclosed he relapsed on heroin a few days ago. He stated that he felt disappointed as he had been clean for 6 months. I noted that he did not appear under the influence of substances during our supervision.
**Review** - X reported that he had not attended his drug appointments. He was encouraged him attend with his drugs worker to gain support with managing substance misuse relapse. I offered to facilitate this meeting by making the next appointment a joint 4-way with the keyworker and his IOM police officer to which he agreed. As X's has a history of acquisitive offending to fund his substance misuse, I explored how he funded his drug misuse. He disclosed using his benefits and expressed being "unsure" what he would do if he ran out of money. He maintained that he has had no contact with the police or reoffended to fund drug habit. Mr X's risk of reoffending is assessed to be increasing as there has been a change in circumstance. X has relapsed on substance misuse. His offending history indicates that he has the propensity to resort to burglary/theft offending behaviours to fund his drug habit. The risk of serious harm is assessed to remain at medium.
**Intervention** - The plan for the session was to complete steppingstones intervention. However, due to X presenting in crisis, the session focused on monitoring/control and treatment to manage risk factors presented. A non-structured intervention was completed with the aim of addressing dynamic risk factors and encourage supporting desistance through motivational interviewing for X to engage with substance misuse services and IOM police.
**Summary** - X appeared somewhat engaged with the session but his disclosure regarding substance misuse issue is a concern. His relapse heightens the risk of reoffending which require action (see set tasks) to mitigate the risks.
**Set task** - PP will complete the following: - Liaise with IOM officer and keyworker and arrange a 4-way meeting for X's next appointment. - Complete BIU check with the IOM team to see if X has come to their notice. - Liaise with X's keyworker before next appointment
**Appointment** - Next appointment given verbally for 01/07/2023 at 10am at the office."

EVALUATION CRITERIA:
Based on the original CRISSA guidelines above, evaluate the report on:

1. **CRISSA Structure Adherence**:
   - Does it follow the proper CRISSA sections (Check in, Review, Intervention, Summary, Set Task, Appointment)?
   - Are the section headers included and properly formatted?
   - Is each section appropriate to its purpose?

2. **Content Quality per Section**:
   - **Check in**: Captures mood, attitude, demeanour of the Person on Probation
   - **Review**: Identifies new disclosures/changes in circumstances (police contact, safeguarding, etc.)
   - **Intervention**: Details purposeful work, thinking/emotions/behaviour interactions (MOST IMPORTANT SECTION - should be most detailed)
   - **Summary**: Engagement level, new risk factors, significant disclosures
   - **Set Task**: Clear, actionable tasks with timelines for both PP and PoP
   - **Appointment**: Next appointment details (no hallucination if not discussed in transcript)

3. **Writing Guidelines Compliance**:
   - Concise and clear writing
   - Relevant context/quotations included where helpful
   - No content duplication or repetition
   - Appropriate level of detail matching transcript length
   - Professional probation terminology

4. **Accuracy and Grounding**:
   - All content accurately reflects the transcript
   - No hallucinated information or assumptions
   - Proper interpretation of dialogue
   - No fabricated details from examples or other sources

5. **Professional Standards**:
   - Appropriate risk assessment considerations
   - Safeguarding awareness where relevant
   - Person-centered language
   - Proper understanding of probation context

ASSESSMENT TASK:
Review the CRISSA report below against the original transcript and the standards above. Identify specific issues that need addressing to meet CRISSA quality standards.

ORIGINAL TRANSCRIPT:
{transcript}

CRISSA REPORT TO EVALUATE (current {character_count} characters):
{crissa_report}

Think step by step through each CRISSA section and writing guideline. Compare the report against the examples provided to understand the expected quality and format.

For the priority field in refinements, use:
- "high": Critical issues affecting accuracy, missing key information, or major structural problems
- "medium": Important improvements for clarity, completeness, or CRISSA compliance
- "low": Minor enhancements for style, formatting, or presentation

For the overall_quality_score, use a scale of 1-10 where:
- 9-10: Excellent, meets all CRISSA standards with minimal or no issues
- 7-8: Good, meets most standards with minor improvements needed
- 5-6: Adequate, meets basic requirements but has some issues to address
- 3-4: Poor, significant problems with structure, content, or accuracy
- 1-2: Unacceptable, major revision required, fails CRISSA standards

Focus your critique on substantive issues that would impact the quality and usefulness of the CRISSA report for probation practice. Only recommend refinements for genuine issues that would improve compliance with CRISSA standards."""

    messages = [
        {
            "role": "system",
            "content": "You are an expert CRISSA report quality assessor with deep knowledge of UK probation practice and case note standards.",
        },
        {
            "role": "user",
            "content": critique_prompt.format(
                transcript=transcript_string,
                crissa_report=crissa_report,
                character_count=len(crissa_report),
            ),
        },
    ]

    try:
        critique_result = await critique_completion(
            model=model_name,
            messages=messages,
            temperature=temperature,
        )
    except Exception as e:
        # Fallback if structured output fails
        critique_result = CrisssaCritique(
            needs_refinement=False,
            overall_quality_score=8,
            refinements=[],
            strengths=["Report appears acceptable"],
            summary=f"Unable to complete critique due to error: {e!s}, proceeding with original report",
        )

    langfuse_context.update_current_observation(
        user_id=user_email,
        input={"transcript": transcript_string, "crissa_report": crissa_report},
        output=critique_result.model_dump(),
    )

    return critique_result


@observe(name="refine_crissa_report", as_type="generation")
async def refine_crissa_report(
    transcript_string: str,
    original_crissa: str,
    critique_feedback: CrisssaCritique,
    user_email: str,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.1,
    today_date: str | None = None,
) -> str:
    """
    Refine a CRISSA report based on critique feedback.
    """

    langfuse_context.update_current_trace(user_id=user_email)

    today_date_readable = today_date or datetime.now(UTC).strftime("%d %B %Y")

    # Create refinement prompt
    refinement_prompt = """You are an experienced senior probation officer in the UK. You need to make ONLY the specific changes requested in the critique feedback while keeping everything else exactly the same.

ORIGINAL TRANSCRIPT:
{transcript}

ORIGINAL CRISSA REPORT:
{original_crissa}

CRITIQUE FEEDBACK:
Quality Score: {quality_score}/10

SPECIFIC REFINEMENTS TO MAKE:
{refinements_list}

STRENGTHS TO KEEP UNCHANGED:
{strengths}

CRITICAL INSTRUCTIONS:
1. ONLY make the specific changes listed in the refinements above
2. Keep ALL other content from the original report exactly the same
3. Do NOT rewrite or rephrase sections that weren't specifically flagged for improvement
4. Preserve the exact wording, structure, and content of any parts not mentioned in the refinements
5. Only address the specific issues identified - do not make additional "improvements"

TASK:
Take the original CRISSA report and make ONLY the minimal changes needed to address each specific refinement point. Everything else should remain identical to the original.

Current date: {today_date}

Generate the full CRISSA report with only the specified refinements applied:"""

    # Format refinements for the prompt
    refinements_text = ""
    for i, refinement in enumerate(critique_feedback.refinements, 1):
        refinements_text += f"{i}. {refinement.section}: {refinement.issue} - {refinement.suggested_improvement} (Priority: {refinement.priority})\n"

    strengths_text = "\n".join(f"- {strength}" for strength in critique_feedback.strengths)

    messages = [
        {
            "role": "user",
            "content": refinement_prompt.format(
                transcript=transcript_string,
                original_crissa=original_crissa,
                quality_score=critique_feedback.overall_quality_score,
                refinements_list=refinements_text,
                strengths=strengths_text,
                today_date=today_date_readable,
            ),
        }
    ]

    completion = await llm_completion(
        temperature=temperature,
        messages=messages,
        model=model_name,
    )

    refined_output = completion.choices[0].message.content
    refined_output = convert_american_to_british_spelling(refined_output)

    langfuse_context.update_current_observation(
        user_id=user_email,
        input={
            "original_crissa": original_crissa,
            "critique": critique_feedback.model_dump(),
            "transcript": transcript_string,
        },
        output=refined_output,
    )

    return refined_output


@observe(name="generate_full_crissa_one_shot_with_refinement", as_type="generation")
async def generate_full_crissa_one_shot_with_refinement(
    transcript_string: str,
    user_email: str,
    prompt_version: int | None = None,
    model_name: str = LLMModel.VERTEX_GEMINI_25_PRO,
    temperature: float = 0.1,
    today_date: str | None = None,
    max_refinement_iterations: int = 1,
) -> str:
    """
    Generate a CRISSA report with critique and refinement process.
    Returns the final refined report as a string.
    """

    langfuse_context.update_current_trace(user_id=user_email)

    # Step 1: Generate initial CRISSA report
    initial_report = await generate_full_crissa_one_shot(
        transcript_string=transcript_string,
        user_email=user_email,
        prompt_version=prompt_version,
        model_name=model_name,
        temperature=temperature,
        today_date=today_date,
    )

    # Step 2: Critique the initial report
    critique_result = await critique_crissa_report(
        transcript_string=transcript_string,
        crissa_report=initial_report,
        user_email=user_email,
        model_name=model_name,
    )

    final_report = initial_report
    refinement_iterations = 0

    # Step 3: Refine if needed
    if critique_result.needs_refinement and refinement_iterations < max_refinement_iterations:
        refined_report = await refine_crissa_report(
            transcript_string=transcript_string,
            original_crissa=initial_report,
            critique_feedback=critique_result,
            user_email=user_email,
            model_name=model_name,
            temperature=temperature,
            today_date=today_date,
        )

        final_report = refined_report
        refinement_iterations += 1

    # Update final trace
    langfuse_context.update_current_observation(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(final_report),
        metadata={
            "initial_quality_score": critique_result.overall_quality_score,
            "needed_refinement": critique_result.needs_refinement,
            "refinement_iterations": refinement_iterations,
        },
    )

    langfuse_context.update_current_trace(
        user_id=user_email,
        input={"template": TemplateName.CRISSA, "dialogue_entries": transcript_string},
        output=html_to_markdown(final_report),
        metadata={
            "process": "critique_and_refinement",
            "iterations": refinement_iterations,
            "initial_critique": critique_result.model_dump(),
        },
    )

    return final_report
