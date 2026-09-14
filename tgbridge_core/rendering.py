"""Pure Telegram rendering and UTF-16-aware chunking."""

import re

def utf16_len(s):
    """Telegram's 4096 cap counts UTF-16 code units, not Python codepoints
    (astral emoji cost 2). Ported from hermes-agent gateway/platforms/base.py."""
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s, limit):
    """Longest prefix whose UTF-16 length <= limit; the codepoint-slice never
    lands mid-character (hermes-agent gateway/platforms/base.py)."""
    if utf16_len(s) <= limit:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def _wrap_markdown_tables(text):
    """Rewrite GFM pipe tables into bold-heading + bullet groups.

    Telegram HTML has no table entity, so raw pipe rows render as escape
    noise. Ported from hermes-agent gateway/platforms/helpers.py
    convert_table_to_bullets: tables inside fenced code blocks are left alone.
    """
    if "|" not in text or "-" not in text:
        return text

    def _split_row(line):
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def _render_block(block):
        headers = _split_row(block[0])
        if len(headers) < 2:
            return "\n".join(block)
        groups = []
        for index, row in enumerate(block[2:], start=1):
            cells = _split_row(row)
            while len(cells) < len(headers):
                cells.append("")
            cells = cells[: len(headers)]
            raw_heading = next((c for c in cells if c), f"Row {index}")
            bullets = [
                f"• {h}: {v}" for h, v in zip(headers, cells) if v != raw_heading
            ]
            # headings flatten inner bold (hermes _convert_header): a bold
            # cell would render <b><b>…</b></b>, same-type nesting Telegram
            # refuses — which would demote the whole chunk to plain text
            heading = re.sub(r"\*\*(.+?)\*\*", r"\1", raw_heading)
            groups.append("\n".join([f"**{heading}**", *bullets]))
        return "\n\n".join(groups)

    sep = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
    out, in_fence, lines, i = [], False, text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            and sep.match(lines[i + 1])
        ):
            block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                block.append(lines[j])
                j += 1
            out.append(_render_block(block))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def inline_html(line):
    """One markdown line -> Telegram HTML. Line-local by design: no entity
    ever spans lines, so per-chunk conversion stays balanced even when a
    chunk boundary lands mid-paragraph. Converted code spans and links are
    stashed hermes-style (format_message placeholders) so later substitutions
    never touch their contents."""
    s = esc(line)
    stash = []

    def _keep(m):
        key = f"\x00tg{len(stash)}\x00"
        stash.append((key, m.group(0)))
        return key

    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"<code>[^<]*</code>", _keep, s)
    s = MD_LINK.sub(r'<a href="\2">\1</a>', s)
    s = re.sub(r"<a href=[^>]*>[^<]*</a>", _keep, s)
    # headers flatten inner bold (hermes _convert_header strips redundant
    # bold markers — <b><b>…</b></b> is same-type nesting Telegram refuses,
    # which would demote the whole chunk to plain text)
    s = re.sub(
        r"^#{1,6}\s+(.*)",
        lambda m: "<b>" + re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)) + "</b>",
        s,
    )
    # markdown list markers -> Telegram's native bullet glyph (hermes tables
    # and lists render bullets as "• ", not literal -/*/+)
    s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)
    # ***x*** must run before ** or it is eaten as bold + stray asterisks
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # emphasis delimiters never flank whitespace and never hug word chars
    # (hermes format_message guards bullets via [^*\n]+; the space-flank rule
    # also kills "a * b * c" arithmetic and "* item *" false positives)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", s)
    for key, val in stash:
        s = s.replace(key, val)
    return s


def _quote_body(line):
    """Classify one blockquote line -> (is_quote, expandable, content).

    hermes _convert_blockquote: '> text' is a plain quote, '**> text' opens
    an expandable quote closed by a trailing '||'."""
    ls = line.lstrip()
    if ls.startswith("**> "):
        return True, True, ls[4:].rstrip()
    if ls.startswith(">") and (len(ls) == 1 or ls[1] in " >"):
        return True, False, ls.lstrip("> ").rstrip()
    return False, False, ""


def md_to_html(md, in_pre=False, pre_lang=""):
    """Markdown -> Telegram HTML. Returns (html, in_pre_after, pre_lang_after)
    so a fenced block split across chunks stays a valid <pre> in every chunk,
    carrying the original language tag (hermes truncate_message carry_lang)."""
    out = []
    if in_pre:
        # continuation chunk reopens the carried fence with its language tag
        out.append(
            f'<pre><code class="language-{esc(pre_lang)}">' if pre_lang else "<pre>"
        )
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            tag = line.lstrip()[3:].strip()
            lang = tag.split()[0] if tag else ""
            if in_pre:
                out.append("</code></pre>" if pre_lang else "</pre>")
            else:
                # language tag -> Telegram's <code class="language-x">,
                # rendered with syntax highlighting in official clients
                out.append(
                    f'<pre><code class="language-{esc(lang)}">' if lang else "<pre>"
                )
            in_pre = not in_pre
            pre_lang = lang
            i += 1
            continue
        if in_pre:
            out.append(esc(line))
            i += 1
            continue
        is_quote, expandable, _ = _quote_body(line)
        if is_quote:
            # merge consecutive quote lines into ONE blockquote (hermes
            # treats quote blocks as blocks, not per-line entities — N
            # stacked boxes is visual noise); **> makes it expandable
            j, parts = i, []
            while j < len(lines):
                q_is, q_exp, q_body = _quote_body(lines[j])
                if not q_is:
                    break
                expandable = expandable or q_exp
                parts.append(q_body)
                j += 1
            if expandable and parts and parts[-1].endswith("||"):
                # hermes: trailing || is the expandable-quote end marker
                parts[-1] = parts[-1][:-2].rstrip()
            inner = "\n".join(inline_html(p) for p in parts)
            open_tag = "<blockquote expandable>" if expandable else "<blockquote>"
            out.append(open_tag + inner + "</blockquote>")
            i = j
            continue
        out.append(inline_html(line))
        i += 1
    if in_pre:
        out.append("</code></pre>" if pre_lang else "</pre>")
    return "\n".join(out), in_pre, pre_lang


def _strip_html_markup(md):
    """Markdown -> clean plain text for the fallback send (hermes-agent's
    _strip_mdv2 contract: the resend must never show raw **/```/[]()
    syntax that the failed formatted attempt would have consumed)."""
    s = re.sub(r"```[^\n]*\n?", "", md)
    s = re.sub(r"``([^`]+)``", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"\|\|(.+?)\|\|", r"\1", s)
    s = MD_LINK.sub(r"\1 (\2)", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^> ?", "", s, flags=re.MULTILINE)
    return s.rstrip()


def _balanced(html):
    """True when every <tag> in the chunk is closed and nesting matches —
    an unbalanced chunk must never be offered to Telegram as HTML."""
    stack = []
    for m in re.finditer(r"<(/?)([a-z][a-z0-9-]*)(?:\s[^>]*)?>", html):
        if m.group(1):
            if not stack or stack[-1] != m.group(2):
                return False
            stack.pop()
        else:
            stack.append(m.group(2))
    return not stack


def split_chunks(text, limit=3900):
    if utf16_len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while rest:
        if utf16_len(rest) <= limit:
            chunks.append(rest)
            break
        region = _prefix_within_utf16_limit(rest, limit)
        cut = region.rfind("\n\n")
        if cut < 1:
            cut = region.rfind("\n")
        if cut < 1:
            cut = region.rfind(" ")
        if cut < 1:
            cut = len(region)
        # Never cut inside an inline code span: an odd number of unescaped
        # backticks means the split lands in an open span (hermes-agent
        # truncate_message); pull the cut back before the unpaired backtick.
        candidate = rest[:cut]
        if (candidate.count("`") - candidate.count("\\`")) % 2 == 1:
            last = candidate.rfind("`")
            while last > 0 and candidate[last - 1] == "\\":
                last = candidate.rfind("`", 0, last)
            safe = max(candidate.rfind("\n", 0, last), candidate.rfind(" ", 0, last))
            if safe >= 1 and safe >= cut // 4:
                cut = safe
        if cut < 1:
            cut = 1  # degenerate budget: always consume one codepoint
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [c for c in chunks if c] or [""]

