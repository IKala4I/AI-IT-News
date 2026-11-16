"""Prepare a digest for Telegram: caption, rich-message parts and HTML fallback chunks.

Usage: python3 -I scripts/tg_prepare.py <digest.md> <out_dir>

Writes into out_dir:
  caption.txt        plain-text caption for sendDocument (<= 1000 chars)
  parts.json         list of Rich Markdown parts for sendRichMessage
  fallback.json      list (one entry per part) of lists of HTML chunks
                     (<= 4000 chars each) for sendMessage with parse_mode=HTML
"""
import html
import json
import os
import re
import sys

RICH_MAX_CHARS = 30000   # Telegram limit is 32768; keep headroom for part headers
RICH_MAX_BLOCKS = 450    # Telegram limit is 500
HTML_MAX_CHARS = 4000    # Telegram limit is 4096


def strip_md(text):
    """Markdown line -> plain text (for the caption)."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("**", "").replace("`", "").replace("\\$", "$")
    return html.unescape(text).strip()


def make_caption(md, date):
    lines, in_tldr = [], False
    for line in md.split("\n"):
        if line.startswith("## "):
            in_tldr = "TL;DR" in line
            continue
        if in_tldr and line.startswith("- "):
            lines.append(strip_md(line[2:]).rstrip(".") + ".")
    caption = f"AI/IT дайджест за {date}"
    if lines:
        caption += ": " + " ".join(lines)
    if len(caption) > 1000:
        caption = caption[:997].rsplit(" ", 1)[0] + "…"
    return caption


SPECIAL_LINE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||<|!\[|```)")


def normalize_rich(md):
    """Make the digest render in Telegram the way it reads in the file.

    Telegram's Rich Markdown follows GFM, so without this:
      * lines starting with "•" melt into one paragraph (soft line breaks),
      * a line that is only "#tools" turns into a heading instead of a hashtag,
      * "**+** ..." and "**−** ..." on adjacent lines end up on one line.
    """
    lines, in_code = [], False
    for line in md.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code:
            if line.startswith("• "):
                line = "- " + line[2:]
            if re.fullmatch(r"#[A-Za-z0-9_]+", line.strip()):
                line = "\\" + line.strip()
        lines.append(line)

    out, in_code = [], False
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (not in_code and line.strip() and nxt.strip()
                and not SPECIAL_LINE.match(line) and not SPECIAL_LINE.match(nxt)):
            line = line.rstrip() + "  "  # GFM hard line break
        out.append(line)
    return "\n".join(out)


def count_blocks(md):
    """Conservative estimate: every non-empty line outside code fences is a block."""
    blocks, in_code = 0, False
    for line in md.split("\n"):
        if line.startswith("```"):
            if not in_code:
                blocks += 1
            in_code = not in_code
            continue
        if not in_code and line.strip():
            blocks += 1
    return blocks


def fits(md):
    return len(md) <= RICH_MAX_CHARS and count_blocks(md) <= RICH_MAX_BLOCKS


def split_rich(md, date):
    """Split on section (##) and post (#hashtag line) boundaries only."""
    if fits(md):
        return [md]
    lines = md.split("\n")
    # Units: (section_header, text). A post ends with its hashtag line.
    units, section, buf, in_code = [], None, [], False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("# ") and section is None and not units:
            continue  # drop the H1 title; each part gets its own
        if not in_code and line.startswith("## "):
            if "\n".join(buf).strip():
                units.append((section, "\n".join(buf).strip()))
            buf, section = [], line
            continue
        buf.append(line)
        if not in_code and re.fullmatch(r"\\?#[A-Za-z0-9_]+", line.strip()):
            units.append((section, "\n".join(buf).strip()))
            buf = []
    if "\n".join(buf).strip():
        units.append((section, "\n".join(buf).strip()))

    groups, cur, cur_section = [], [], None
    for sec, text in units:
        chunk = text if (sec == cur_section and cur) else (f"{sec}\n\n{text}" if sec else text)
        candidate = "\n\n".join(cur + [chunk])
        if cur and not fits(candidate):
            groups.append(cur)
            cur = [f"{sec}\n\n{text}" if sec else text]
        else:
            cur.append(chunk)
        cur_section = sec
    if cur:
        groups.append(cur)
    total = len(groups)
    return [f"# AI/IT Дайджест — {date} (Частина {i}/{total})\n\n" + "\n\n".join(g)
            for i, g in enumerate(groups, 1)]


def inline_html(text):
    text = html.unescape(text.replace("\\$", "$").replace("\\#", "#")).rstrip()
    codes = []

    def keep(m):
        codes.append("<code>" + html.escape(m.group(1), quote=False) + "</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                  lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def to_html_chunks(md):
    blocks, cur, in_code, code = [], [], False, []
    for line in md.split("\n"):
        if line.startswith("```"):
            if in_code:
                cur.append("<pre>" + html.escape("\n".join(code), quote=False) + "</pre>")
                code, in_code = [], False
            else:
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            cur.append("<b>" + inline_html(m.group(2)) + "</b>")
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", line)
        if m:
            cur.append(f'<a href="{html.escape(m.group(2))}">🖼 {html.escape(m.group(1))}</a>')
            continue
        if line.startswith("- "):
            line = "• " + line[2:]
        cur.append(inline_html(line))
    if cur:
        blocks.append("\n".join(cur))

    chunks, buf = [], ""
    for block in blocks:
        while len(block) > HTML_MAX_CHARS:  # a single huge block: hard split on lines
            cut = block.rfind("\n", 0, HTML_MAX_CHARS)
            cut = cut if cut > 0 else HTML_MAX_CHARS
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) <= HTML_MAX_CHARS:
            buf = candidate
        else:
            chunks.append(buf)
            buf = block
    if buf:
        chunks.append(buf)
    return chunks


def main():
    src, out = sys.argv[1], sys.argv[2]
    md = open(src, encoding="utf-8").read().strip() + "\n"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(src))
    date = m.group(1) if m else ""
    parts = split_rich(normalize_rich(md), date)
    with open(os.path.join(out, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(make_caption(md, date))
    with open(os.path.join(out, "parts.json"), "w", encoding="utf-8") as f:
        json.dump(parts, f, ensure_ascii=False)
    with open(os.path.join(out, "fallback.json"), "w", encoding="utf-8") as f:
        json.dump([to_html_chunks(p) for p in parts], f, ensure_ascii=False)


if __name__ == "__main__":
    main()
