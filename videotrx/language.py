from videotrx import config, paths
from videotrx.utils import read_json

SPACY_MODELS = {
    "en": "en_core_web_md",
    "ru": "ru_core_news_md",
    "fr": "fr_core_news_md",
    "ja": "ja_core_news_md",
    "es": "es_core_news_md",
    "de": "de_core_news_md",
    "it": "it_core_news_md",
    "zh": "zh_core_web_md",
    "ko": "ko_core_news_sm",
}
SPACE_JOINED = ["en", "es", "fr", "de", "it", "ru", "ko"]
NO_SPACE_JOINED = ["zh", "ja"]


def source_language() -> str:
    """配置的源语言；为 auto 时取转写结果 transcript.json 里检测到的语言。"""
    language = str(config.get("source_language", "auto") or "auto").strip()
    if language != "auto":
        return language
    detected = ""
    if paths.TRANSCRIPT.is_file():
        detected = str(read_json(paths.TRANSCRIPT).get("language") or "").strip()
    if not detected:
        raise RuntimeError(
            "无法确定源语言：transcript.json 中没有检测到的语言，请在 config.yaml 中设置 source_language"
        )
    return detected


def get_joiner(language):
    if language in SPACE_JOINED:
        return " "
    elif language in NO_SPACE_JOINED:
        return ""
    else:
        raise ValueError(f"Unsupported language code: {language}")
