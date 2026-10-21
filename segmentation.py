"""Deterministic source ranges; no model calls, rewriting, or normalization."""
from __future__ import annotations

from bisect import bisect_right
import re
import unicodedata


MAX_SOURCE_CHARACTERS = 12000
SARASHINA_CHUNK_CHARACTERS = 1200
MAX_CHUNK_CHARACTERS = 2000
_NEWLINE = r"(?:\r\n|[\r\n])"
_PARAGRAPH = re.compile(_NEWLINE + r"(?:[^\S\r\n]*" + _NEWLINE + r")+")
_LINE = re.compile(_NEWLINE)
_SENTENCE = re.compile(r'''[。！？!?]+[」』】）》〉”’"']*|\.+[」』】）》〉”’"']*(?=\s|$)''')
_QUOTES = {"「": "」", "『": "』", "“": "”", "‘": "’"}


class SegmentationError(ValueError):
    pass


def _extends_cluster(character):
    codepoint = ord(character)
    return (unicodedata.category(character) in {"Mn", "Mc", "Me"}
            or character in {"\u200c", "\u200d"}
            or 0x1F3FB <= codepoint <= 0x1F3FF or 0xE0020 <= codepoint <= 0xE007F)


def _safe_boundary(text, position):
    if position <= 0 or position >= len(text):
        return True
    if text[position-1] == "\r" and text[position] == "\n":
        return False
    if text[position-1] == "\u200d" or _extends_cluster(text[position]):
        return False
    # Keep a regional-indicator flag pair together if a boundary was extended
    # through a preceding joiner/combining sequence.
    if 0x1F1E6 <= ord(text[position]) <= 0x1F1FF:
        count, index = 0, position - 1
        while index >= 0 and 0x1F1E6 <= ord(text[index]) <= 0x1F1FF:
            count += 1
            index -= 1
        if count % 2:
            return False
    return True


def _quotation_ranges(text, protected):
    """Protect nested quotations only for splitting, never against editing.

    Existing code/URL ranges are opaque. Unclosed outer quotations extend to
    the end of the source so a long quote is rejected rather than cut silently.
    ASCII apostrophes and double quotes are deliberately not quote delimiters.
    """
    spans, stack, protected_index = [], [], 0
    for index, character in enumerate(text):
        while protected_index < len(protected) and protected[protected_index][1] <= index:
            protected_index += 1
        if protected_index < len(protected) and protected[protected_index][0] <= index < protected[protected_index][1]:
            continue
        if character in _QUOTES:
            stack.append((_QUOTES[character], index))
        elif stack and character == stack[-1][0]:
            # A typographic apostrophe in a word such as don’t does not close
            # the surrounding single quotation ‘don’t change this’.
            if (character == "’" and 0 < index < len(text)-1
                    and text[index-1].isascii() and text[index-1].isalnum()
                    and text[index+1].isascii() and text[index+1].isalnum()):
                continue
            start = stack[0][1]
            stack.pop()
            if not stack:
                spans.append((start, index + 1))
    if stack:
        spans.append((stack[0][1], len(text)))
    return spans


def source_ranges(text, maximum=SARASHINA_CHUNK_CHARACTERS, protected=()):
    """Return contiguous codepoint offsets, preferring paragraph boundaries.

    Slices keep the source's original bytes when UTF-8 encoded and joined.
    Protected ranges are whole-source codepoint offsets supplied by the editor.
    Oversized atomic material or a sentence without a safe break is rejected
    before the first generation request; no hard cut or partial result is used.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_SOURCE_CHARACTERS:
        raise SegmentationError("分割する原文は1〜12,000文字で指定してください。")
    if type(maximum) is not int or not 1 <= maximum <= MAX_CHUNK_CHARACTERS:
        raise SegmentationError("文章分割の文字上限が不正です。")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise SegmentationError("原文に不正なUnicode文字が含まれています。") from None
    intervals = []
    for span in protected:
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] <= span[1] <= len(text)):
            raise SegmentationError("保護範囲の情報が不正です。")
        if span[0] != span[1]:
            intervals.append(tuple(span))
    def merge(spans):
        merged = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    merged = merge(intervals)
    if any(end - start > maximum for start, end in merged):
        raise SegmentationError(f"コードやURLなど、途中で分けられない範囲が{maximum:,}字を超えています。その範囲を短くするか、校正対象から外して再実行してください。原文は変更していません。")
    quotations = _quotation_ranges(text, merged)
    if any(end - start > maximum for start, end in quotations):
        raise SegmentationError(f"一つの引用範囲が{maximum:,}字を超えています。引用を短くするか、閉じ引用符を確認して引用の外に段落の改行を加えてください。引用の途中では分割せず、原文も変更していません。")
    merged = merge([*merged, *quotations])
    starts = [start for start, _ in merged]

    def allowed(position):
        index = bisect_right(starts, position) - 1
        return (_safe_boundary(text, position)
                and (index < 0 or not merged[index][0] < position < merged[index][1]))

    boundaries = []
    for pattern in (_PARAGRAPH, _LINE, _SENTENCE):
        candidates = set()
        for match in pattern.finditer(text):
            end = match.end()
            while end < len(text) and not _safe_boundary(text, end):
                end += 1
            if allowed(end):
                candidates.add(end)
        boundaries.append(sorted(candidates))
    ranges, start = [], 0
    while start < len(text):
        limit = min(start + maximum, len(text))
        if limit == len(text):
            end = limit
        else:
            end = None
            for candidates in boundaries:
                index = bisect_right(candidates, limit) - 1
                if index >= 0 and candidates[index] > start:
                    end = candidates[index]
                    break
            if end is None:
                raise SegmentationError(f"{maximum:,}字以内に安全な区切りを見つけられません。一文を短くするか、段落の改行を加えて再実行してください。文章は切り詰めていません。")
        ranges.append({"start": start, "end": end})
        start = end
    return ranges
