from transcribe_api.domain.models_dictation import TemplateMetadata

# Define individual template variables
general_template = TemplateMetadata(
    name="General",
    description="Standard meeting summary with key points, decisions, and action items",
    category="common",
)

crissa_template = TemplateMetadata(
    name="Crissa",
    description="Check-in, review, intervention/implementation, summary, set task, appointment",
    category="common",
)


# Collect templates into a list
all_templates = [
    general_template,
    crissa_template,
]


def get_all_templates() -> list[TemplateMetadata]:
    """Return all template categories and their templates."""
    return all_templates
