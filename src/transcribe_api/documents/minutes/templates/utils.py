from transcribe_api.domain.models_dictation import DialogueEntry


def format_transcript_string_for_prompt(dialogue_entries: list[DialogueEntry], include_index: bool = True) -> str:
    """
    Generates a transcript string with optional index in square brackets, speaker, and text for each entry.
    Example with index: "[0] Alice: Hello\n[1] Bob: Hi"
    Example without index: "Alice: Hello\nBob: Hi"
    """
    return "\n".join(
        [
            (f"[{idx}] {item.speaker}: {item.text}" if include_index else f"{item.speaker}: {item.text}")
            for idx, item in enumerate(dialogue_entries)
        ]
    )
