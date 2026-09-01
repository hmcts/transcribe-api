"""Tests for markdown utilities."""

from bs4 import BeautifulSoup, NavigableString, Tag

from transcribe_api.runtime.markdown import html_to_markdown, markdown_to_html


def test_markdown_to_html_basic():
    """Test basic markdown to HTML conversion"""
    markdown_text = "**Hello** _world_"
    expected_html = "<p><strong>Hello</strong> <em>world</em></p>"
    assert markdown_to_html(markdown_text) == expected_html


def test_markdown_to_html_strip_option():
    """Test strip parameter in markdown_to_html"""
    markdown_text = "# Header"
    expected_html = "<h1>Header</h1>"
    assert markdown_to_html(markdown_text, strip=True) == expected_html
    assert markdown_to_html(markdown_text, strip=False).strip() == expected_html


def test_html_to_markdown_basic():
    """Test basic HTML to markdown conversion"""
    html_text = "<p><strong>Hello</strong> <em>world</em></p>"
    expected_markdown = "**Hello** _world_"
    assert html_to_markdown(html_text).strip() == expected_markdown


def test_roundtrip_conversion():
    """Test markdown -> HTML -> markdown conversion maintains meaning"""
    original_markdown = """
# Header

* List item 1
* List item 2

**Bold** and _italic_ text
"""
    html = markdown_to_html(original_markdown)
    result_markdown = html_to_markdown(html)

    # Convert both to HTML for comparison (since markdown can have different valid representations)
    assert markdown_to_html(result_markdown) == markdown_to_html(original_markdown)


def test_edge_cases():
    """Test edge cases and empty inputs"""
    assert markdown_to_html("") == ""
    assert html_to_markdown("") == ""
    assert markdown_to_html(" ") == ""
    assert html_to_markdown(" ").strip() == ""


def test_markdown_to_html_newlines():
    """Test how markdown newlines are converted to HTML"""
    markdown_text = """First line
Second line

Third line after blank line"""

    expected_html = "<p>First line\nSecond line</p>\n<p>Third line after blank line</p>"
    result = markdown_to_html(markdown_text)
    assert result == expected_html


def test_unordered_lists():
    """Test conversion of unordered lists"""
    markdown_text = """
* First item
* Second item
  * Nested item
  * Another nested item
* Third item
"""
    expected_html = """<ul>
<li>First item</li>
<li>Second item
<ul>
<li>Nested item</li>
<li>Another nested item</li>
</ul>
</li>
<li>Third item</li>
</ul>"""
    result = markdown_to_html(markdown_text.strip())
    assert result.strip() == expected_html.strip()


def _normalise_html_structure(actual_html: str) -> tuple:
    """Normalise a HTML string for robust comparison.

    This utility normalises HTML by parsing it into a tree structure for
    comparing the logical DOM hierarchy rather than exact string
    formatting. It strips whitespace from text nodes and ignores
    formatting differences like indentation. Mainly to avoid brittle
    tests.

    Parameters
    ----------
    actual_html : str
        The HTML string to test

    Returns
    -------
    tuple
        The normalised HTML structure
    """

    def _node_to_structure(node):
        """Convert a BeautifulSoup node into a normalized structure."""
        if isinstance(node, NavigableString):
            text = node.strip()
            return ("#text", text) if text else None

        if isinstance(node, Tag):
            children = []
            for child in node.children:
                struct = _node_to_structure(child)
                if struct is not None:
                    children.append(struct)
            # Ignore attributes for simpler comparison - extend if needed
            return (node.name, tuple(children))

        return None  # Comments, Doctype, etc.

    def _html_to_structure(html: str):
        """Parse HTML string into a normalized structure tuple."""
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for element in soup.contents:
            struct = _node_to_structure(element)
            if struct is not None:
                result.append(struct)
        return tuple(result)

    return _html_to_structure(actual_html)


def test_html_to_markdown_ordered_lists():
    """Test conversion of ordered lists (structural compare)"""
    markdown_text = """
    1. First item
    2. Second item
       1. Nested item
       2. Another nested item
    3. Third item
    """

    expected_html = """<ol>
      <li>First item</li>
      <li>Second item
        <ol>
          <li>Nested item</li>
          <li>Another nested item</li>
        </ol>
      </li>
      <li>Third item</li>
    </ol>"""

    actual_html = markdown_to_html(markdown_text, strip=True)

    assert _normalise_html_structure(actual_html) == _normalise_html_structure(expected_html), (
        "Returned HTML does not match expected HTML, examine the diff with `pytest -k 'test_html_to_markdown_ordered_lists' -vv`"
    )


def test_markdown_to_html_mixed_lists():
    """Test conversion of mixed ordered and unordered lists"""
    markdown_text = """
    * Unordered item
        1. Nested ordered item
        2. Second nested ordered item
    * Another unordered item
    """

    expected_html = """
    <ul>
      <li>Unordered item
        <ol>
          <li>Nested ordered item</li>
          <li>Second nested ordered item</li>
        </ol>
      </li>
      <li>Another unordered item</li>
    </ul>
    """
    assert _normalise_html_structure(markdown_to_html(markdown_text)) == _normalise_html_structure(expected_html), (
        "Returned HTML does not match expected HTML, examine the diff with `pytest -k 'test_markdown_to_html_mixed_lists' -vv`"
    )
