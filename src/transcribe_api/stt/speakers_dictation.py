import sentry_sdk

from transcribe_api.domain.models_dictation import DialogueEntry
from transcribe_api.documents.minutes.llm_calls import generate_speaker_predictions


def convert_input_dialogue_entries_to_dialogue_entries(
    entries: list[dict],
) -> list[DialogueEntry]:
    return [
        DialogueEntry(
            speaker=entry["speaker"],
            text=entry["text"],
            start_time=float(entry["offsetMilliseconds"]) / 1000,
            end_time=(float(entry["offsetMilliseconds"]) + float(entry["durationMilliseconds"])) / 1000,
        )
        for entry in entries
    ]


def group_dialogue_entries_by_speaker(
    entries: list[DialogueEntry],
) -> list[DialogueEntry]:
    grouped_entries: list[DialogueEntry] = []
    current_speaker = None
    current_entry = None

    for entry in entries:
        if entry.speaker != current_speaker:
            if current_entry:
                grouped_entries.append(current_entry)
            current_speaker = entry.speaker
            current_entry = DialogueEntry(
                speaker=current_speaker,
                text=entry.text,
                start_time=entry.start_time,
                end_time=entry.end_time,
            )
        elif current_entry:
            current_entry.text += f" {entry.text}"
            current_entry.end_time = entry.end_time

    if current_entry:
        grouped_entries.append(current_entry)

    return grouped_entries


def normalize_speaker_labels(entries: list[DialogueEntry]) -> list[DialogueEntry]:
    speaker_map: dict[str, str] = {}
    current_speaker_index = 0

    normalized_entries = []
    for entry in entries:
        if entry.speaker not in speaker_map:
            speaker_map[entry.speaker] = str(current_speaker_index)
            current_speaker_index += 1

        normalized_entries.append(
            DialogueEntry(
                speaker=speaker_map[entry.speaker],
                text=entry.text,
                start_time=entry.start_time,
                end_time=entry.end_time,
            )
        )

    return normalized_entries


def add_speaker_labels_to_dialogue_entries(
    entries: list[DialogueEntry],
) -> list[DialogueEntry]:
    return [
        DialogueEntry(
            speaker=f"Speaker {entry.speaker}",
            text=entry.text,
            start_time=entry.start_time,
            end_time=entry.end_time,
        )
        for entry in entries
    ]


async def process_speakers_and_dialogue_entries(
    dialogue_entries: list[DialogueEntry],
    user_email: str,
) -> list[DialogueEntry]:
    try:
        # Step 1: Group similar speakers together
        grouped_dialogue_entries = group_dialogue_entries_by_speaker(dialogue_entries)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return dialogue_entries

    try:
        # Step 2: Normalize speaker labels to numbers
        normalised_dialogue_entries = normalize_speaker_labels(grouped_dialogue_entries)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return grouped_dialogue_entries

    try:
        # Step 3: Add "Speaker" prefix
        labelled_dialogue_entries = add_speaker_labels_to_dialogue_entries(normalised_dialogue_entries)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return normalised_dialogue_entries

    try:
        # Step 4: Get speaker predictions
        speaker_predictions = await generate_speaker_predictions(labelled_dialogue_entries, user_email)
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return labelled_dialogue_entries

    try:
        # Step 5: Update entries with predicted names
        predicted_entries = []
        for entry in labelled_dialogue_entries:
            predicted_entries.append(
                DialogueEntry(
                    speaker=speaker_predictions.get(entry.speaker, entry.speaker),
                    text=entry.text,
                    start_time=entry.start_time,
                    end_time=entry.end_time,
                )
            )
        return predicted_entries  # noqa: TRY300
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return labelled_dialogue_entries
