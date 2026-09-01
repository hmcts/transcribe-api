import textwrap

import commonmark
import html2text
from langfuse.decorators import observe


@observe(name="markdown_to_html", as_type="conversion")
def markdown_to_html(markdown_text: str, strip: bool = True) -> str:
    """
    Convert markdown text to HTML using CommonMark (for better nested list support).

    Args:
        markdown_text (str): The markdown text to convert
        strip (bool): Whether to strip leading/trailing whitespace from the result (default: True)

    Returns:
        str: The converted HTML
    """
    # Remove uniform indent from all lines of the markdown text
    markdown_text = textwrap.dedent(markdown_text)
    parser = commonmark.Parser()
    renderer = commonmark.HtmlRenderer()
    ast = parser.parse(markdown_text)
    html = renderer.render(ast)
    return html.strip() if strip else html


@observe(name="html_to_markdown", as_type="conversion")
def html_to_markdown(html_text: str) -> str:
    """
    Convert HTML text to markdown.

    Args:
        html_text (str): The HTML formatted text

    Returns:
        str: Markdown formatted text
    """
    # Initialize HTML to text converter
    h = html2text.HTML2Text()

    # Configure converter settings
    h.body_width = 0  # Disable line wrapping
    h.ignore_links = False
    h.ignore_images = False
    h.ignore_emphasis = False
    h.ignore_tables = False

    # Convert HTML to markdown
    markdown_text = h.handle(html_text)
    return markdown_text.strip()  # Remove trailing whitespace
