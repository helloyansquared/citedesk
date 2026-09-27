"""Text normalisation and a small language-agnostic tokenizer for keyword search.

No NLP dependencies: accents are folded ("réparation" == "reparation"), plurals are trimmed
("batteries" == "battery"), and Chinese/Japanese/Korean text, which has no spaces, is indexed
as overlapping character pairs so "保修期" still matches "保修".
"""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")

# Only the most frequent function words; BM25's IDF handles the rest.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could do does did for from had has have how i if in into is it its
    me my of on or our so than that the their them then there these they this to us was we were what when where
    which who why will with would you your yours hi hello please thanks thank
    au aux avec ce ces cet cette dans de des du elle en est et il ils je la le les leur mais me mes mon ne nos
    notre nous on ou par pas pour qu que qui sa se ses son sont sur ta te tes toi ton tu un une vos votre vous y
    quel quelle quels quelles comment combien bonjour merci
    """.split()
)


def fold(text: str) -> str:
    """Lowercase and strip accents, keeping CJK characters intact."""
    text = unicodedata.normalize("NFKC", text).lower()
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _stem(word: str) -> str:
    if word.isdigit() or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD.findall(fold(text)):
        if _CJK.search(word):
            tokens.extend(_cjk_tokens(word))
        elif word not in STOPWORDS:
            tokens.append(_stem(word))
    return tokens


def _cjk_tokens(word: str) -> list[str]:
    """Split a mixed run like 'e03是什么意思' into latin words and CJK character bigrams."""
    out: list[str] = []
    run: list[str] = []
    latin = ""

    def flush_run() -> None:
        if len(run) == 1:
            out.append(run[0])
        out.extend(a + b for a, b in zip(run, run[1:], strict=False))
        run.clear()

    for ch in word:
        if _CJK.match(ch):
            if latin:
                out.append(_stem(latin))
                latin = ""
            run.append(ch)
        else:
            if run:
                flush_run()
            latin += ch
    if run:
        flush_run()
    if latin:
        out.append(_stem(latin))
    return out


def clean_whitespace(text: str) -> str:
    """Collapse runs of spaces, keep paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
