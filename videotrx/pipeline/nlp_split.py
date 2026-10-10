import warnings

import spacy
from rich import print as rprint
from spacy.cli import download

from videotrx import config, paths
from videotrx.language import SPACY_MODELS, get_joiner, source_language
from videotrx.pipeline.entity_repair import repair_rough_split_entities
from videotrx.utils import except_handler, read_json, write_json

warnings.filterwarnings("ignore", category=FutureWarning)

def get_spacy_model(language: str):
    model = SPACY_MODELS.get(language.lower(), "en_core_web_md")
    if language not in SPACY_MODELS:
        rprint(f"[yellow]Spacy model does not support '{language}', using en_core_web_md model as fallback...[/yellow]")
    return model

@except_handler("Failed to load NLP Spacy model")
def init_nlp():
    language = source_language()
    model = get_spacy_model(language)
    rprint(f"[blue]⏳ Loading NLP Spacy model: <{model}> ...[/blue]")
    try:
        nlp = spacy.load(model)
    except:
        rprint(f"[yellow]Downloading {model} model...[/yellow]")
        rprint("[yellow]If download failed, please check your network and try again.[/yellow]")
        download(model)
        nlp = spacy.load(model)
    rprint("[green]✅ NLP Spacy model loaded successfully![/green]")
    return nlp


def rough_split(nlp):
    """
    按标点分句：
    1. 先根据时间间隔把文本分成多个段落
    2. 对每个段落用 spacy 按标点分句
    这样既保留了 spacy 的智能分句，又能在时间断点处强制分开。
    """
    language = source_language()
    joiner = get_joiner(language)
    rprint(f"[blue]🔍 Using {language} language joiner: '{joiner}'[/blue]")
    
    # 读取时间间隔阈值
    time_gap_threshold = config.get("nlp_split.pause_threshold_sec")
    rprint(f"[blue]⏱️ Time gap threshold: {time_gap_threshold}s[/blue]")
    
    words = read_json(paths.TRANSCRIPT)["words"]
    
    # 第一步：根据时间间隔分成多个段落
    paragraphs = []  # 每个段落是一个词列表
    current_paragraph = []
    prev_end_time = None
    
    for row in words:
        word = row['text']
        start_time = row['start']
        end_time = row['end']
        
        if not word or str(word).isspace():
            continue
        
        # 检查是否需要开始新段落
        if prev_end_time is not None:
            time_gap = start_time - prev_end_time
            if time_gap > time_gap_threshold:
                # 保存当前段落，开始新段落
                if current_paragraph:
                    paragraphs.append(current_paragraph)
                current_paragraph = []
                rprint(f"[dim]📍 Time gap {time_gap:.2f}s at {start_time:.2f}s[/dim]")
        
        current_paragraph.append(word)
        prev_end_time = end_time
    
    # 保存最后一个段落
    if current_paragraph:
        paragraphs.append(current_paragraph)
    
    rprint(f"[blue]📍 Split into {len(paragraphs)} paragraphs based on time gaps[/blue]")
    
    # 第二步：对每个段落用 spacy 分句
    all_sentences = []
    
    for paragraph_words in paragraphs:
        # 用 joiner 拼接段落内的词
        paragraph_text = joiner.join(paragraph_words)
        
        # 用 spacy 分句
        doc = nlp(paragraph_text)
        
        if not doc.has_annotation("SENT_START"):
            # 如果 spacy 无法分句，保留整个段落
            all_sentences.append(paragraph_text)
            continue
        
        # 处理 spacy 分出的句子（合并 - 和 ... 开头/结尾的情况）
        current_sentence = []
        for sent in doc.sents:
            text = sent.text.strip()
            if not text:
                continue
            
            if current_sentence and (
                text.startswith('-') or 
                text.startswith('...') or
                current_sentence[-1].endswith('-') or
                current_sentence[-1].endswith('...')
            ):
                current_sentence.append(text)
            else:
                if current_sentence:
                    all_sentences.append(' '.join(current_sentence))
                    current_sentence = []
                current_sentence.append(text)
        
        if current_sentence:
            all_sentences.append(' '.join(current_sentence))

    sentences = []
    for i, sentence in enumerate(all_sentences):
        if i > 0 and sentence.strip() in [',', '.', '，', '。', '？', '！']:
            # 如果当前句只有标点，并入上一句
            sentences[-1] += sentence
        else:
            sentences.append(sentence)
    
    rprint(f"[green]✅ Split into {len(all_sentences)} sentences[/green]")
    return sentences

def split_by_spacy():
    nlp = init_nlp()
    sentences = rough_split(nlp)
    sentences = repair_rough_split_entities(sentences)

    sentences = [sentence.strip() for sentence in sentences]
    while sentences and not sentences[0]:
        sentences.pop(0)
    while sentences and not sentences[-1]:
        sentences.pop()
    write_json(paths.NLP_SPLIT, sentences)
    rprint(f"[green]💾 Saved to → `{paths.NLP_SPLIT}`[/green]")
