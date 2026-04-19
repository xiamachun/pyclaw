"""Channel message formatting utilities.

Provides functions to adapt LLM output (standard Markdown) for messaging
platforms that have limited or no Markdown rendering support.

Functions:
    convert_markdown_tables_to_text: Replace Markdown tables with aligned
        plain-text tables (for platforms that don't render tables).
    adapt_markdown_for_dingtalk: Add trailing double-spaces for DingTalk
        hard line breaks.
    split_message_by_bytes: Split a long message into byte-limited chunks
        for platforms with message size limits (e.g. WeCom 2048 bytes).
    format_for_wecom: Full pipeline for WeCom messages.
    format_for_dingtalk: Full pipeline for DingTalk messages.
"""

import re
import unicodedata


def _east_asian_width(char: str) -> int:
    """Return display width of a character (2 for CJK, 1 otherwise).

    Args:
        char: A single character.

    Returns:
        Display width (1 or 2).
    """
    width_category = unicodedata.east_asian_width(char)
    return 2 if width_category in ("W", "F") else 1


def _display_width(text: str) -> int:
    """Calculate the display width of a string (CJK-aware).

    Args:
        text: Input string.

    Returns:
        Total display width.
    """
    return sum(_east_asian_width(ch) for ch in text)


def _pad_to_width(text: str, target_width: int) -> str:
    """Pad a string with spaces to reach target display width.

    Args:
        text: Input string.
        target_width: Desired display width.

    Returns:
        Padded string.
    """
    current_width = _display_width(text)
    padding = max(0, target_width - current_width)
    return text + " " * padding


def convert_markdown_tables_to_text(content: str) -> str:
    """Convert Markdown tables to aligned plain-text tables.

    Replaces pipe-delimited Markdown tables with space-aligned columns
    using Unicode box-drawing characters for the separator line.

    Args:
        content: Message content potentially containing Markdown tables.

    Returns:
        Content with tables converted to plain-text format.
    """
    lines = content.split("\n")
    result_lines: list[str] = []
    table_lines: list[str] = []

    def flush_table() -> None:
        """Process accumulated table lines and append formatted output."""
        if len(table_lines) < 2:
            result_lines.extend(table_lines)
            table_lines.clear()
            return

        # Parse cells from each row
        rows: list[list[str]] = []
        separator_indices: list[int] = []
        for idx, line in enumerate(table_lines):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # Detect separator row (e.g. |---|---|)
            if all(re.match(r"^:?-+:?$", c.strip()) for c in cells if c.strip()):
                separator_indices.append(idx)
            else:
                rows.append(cells)

        if not rows:
            result_lines.extend(table_lines)
            table_lines.clear()
            return

        # Normalize column count
        max_cols = max(len(row) for row in rows)
        for row in rows:
            while len(row) < max_cols:
                row.append("")

        # Calculate column widths (display-width aware)
        col_widths = [0] * max_cols
        for row in rows:
            for col_idx, cell in enumerate(row):
                col_widths[col_idx] = max(
                    col_widths[col_idx], _display_width(cell)
                )

        # Build formatted output
        for row_idx, row in enumerate(rows):
            formatted_cells = [
                _pad_to_width(cell, col_widths[col_idx])
                for col_idx, cell in enumerate(row)
            ]
            result_lines.append("  ".join(formatted_cells))
            # Add separator after header row
            if row_idx == 0 and separator_indices:
                separator = "  ".join("─" * w for w in col_widths)
                result_lines.append(separator)

        table_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(line)
        else:
            if table_lines:
                flush_table()
            result_lines.append(line)

    if table_lines:
        flush_table()

    return "\n".join(result_lines)


def adapt_markdown_for_dingtalk(text: str) -> str:
    """Adapt standard Markdown to DingTalk webhook markdown rendering.

    DingTalk markdown ignores single newlines between lines. To force a
    visible line break you need either a blank line (paragraph break) or
    two trailing spaces before the newline (hard break).

    This function adds trailing double-spaces to every content line that
    is not already a heading, table row, blank line, or horizontal rule.

    Args:
        text: Standard Markdown text.

    Returns:
        Text adapted for DingTalk markdown rendering.
    """
    output_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        is_blank = not stripped
        is_heading = stripped.startswith("#")
        is_table = stripped.startswith("|")
        is_hr = stripped in ("---", "***", "___")
        already_has_break = line.endswith("  ")

        if is_blank or is_heading or is_table or is_hr or already_has_break:
            output_lines.append(line)
        else:
            output_lines.append(line + "  ")
    return "\n".join(output_lines)


def split_message_by_bytes(
    message: str, max_bytes: int = 2000
) -> list[str]:
    """Split a message into chunks that fit within a byte limit.

    Tries to split at paragraph boundaries (double newline) first,
    then at single newlines, and finally hard-cuts as a last resort.
    Each chunk is labeled (1/N), (2/N), etc. when splitting occurs.

    Args:
        message: The full message text.
        max_bytes: Maximum UTF-8 bytes per chunk (default 2000,
            leaving buffer for WeCom's 2048 limit).

    Returns:
        A list of message chunks, each within the byte limit.
    """
    if len(message.encode("utf-8")) <= max_bytes:
        return [message]

    # Reserve space for chunk label like " (1/10)"
    label_reserve = 10
    effective_limit = max_bytes - label_reserve

    chunks: list[str] = []
    remaining = message

    while remaining:
        encoded = remaining.encode("utf-8")
        if len(encoded) <= effective_limit:
            chunks.append(remaining)
            break

        # Find a good cut point within the byte limit
        # Decode back from the byte boundary to avoid splitting mid-character
        truncated = encoded[:effective_limit].decode("utf-8", errors="ignore")
        cut_pos = len(truncated)

        # Try paragraph break first
        para_pos = truncated.rfind("\n\n")
        if para_pos > 0:
            cut_pos = para_pos
        else:
            # Try single newline
            nl_pos = truncated.rfind("\n")
            if nl_pos > 0:
                cut_pos = nl_pos

        chunks.append(remaining[:cut_pos].rstrip())
        remaining = remaining[cut_pos:].lstrip("\n")

    # Add chunk labels
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            "%s (%d/%d)" % (chunk, idx + 1, total)
            for idx, chunk in enumerate(chunks)
        ]

    return chunks


def format_for_wecom(content: str) -> list[str]:
    """Format a message for WeCom (Enterprise WeChat).

    Converts Markdown tables to plain-text and splits into chunks
    that fit within WeCom's 2048-byte text message limit.

    Args:
        content: Raw LLM response content.

    Returns:
        List of message chunks ready to send via WeCom API.
    """
    formatted = convert_markdown_tables_to_text(content)
    return split_message_by_bytes(formatted, max_bytes=2000)


def format_for_dingtalk(content: str) -> str:
    """Format a message for DingTalk text messages.

    Converts Markdown tables to plain-text aligned format since
    DingTalk session webhook only supports msgtype "text".

    Args:
        content: Raw LLM response content.

    Returns:
        Formatted message string.
    """
    return convert_markdown_tables_to_text(content)