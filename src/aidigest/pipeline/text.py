"""キーワード照合と類似度で共通に使う文字列の処理（specs/005、006）。"""

import re
import unicodedata

CONTENT_CHARS = 1000  # 照合に使う本文の先頭の文字数
_HTML_TAG = re.compile(r"<[^>]+>")


def normalize(text: str) -> str:
    """全角・半角と大文字・小文字の違いを無くす（NFKC 正規化 + casefold）。"""
    return unicodedata.normalize("NFKC", text).casefold()


def strip_tags(content: str) -> str:
    """HTML タグを空白に置き換える。"""
    return _HTML_TAG.sub(" ", content)


def content_excerpt(content: str) -> str:
    """本文から HTML タグを除き、先頭 `CONTENT_CHARS` 文字を返す（正規化はしない）。"""
    return strip_tags(content)[:CONTENT_CHARS]
