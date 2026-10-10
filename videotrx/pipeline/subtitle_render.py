import pandas as pd
import os
import re
from rich.panel import Panel
from rich.console import Console
import autocorrect_py as autocorrect
from videotrx import config, paths
from videotrx.utils import read_json
console = Console()

def get_ass_header():
    """从配置文件读取字体并生成 ASS 头部"""
    # 读取字幕样式配置，设置默认值
    chinese_font = config.get('subtitle.style.chinese_font', 'SimHei')
    english_font = config.get('subtitle.style.english_font', 'Arial')
    font_size = config.get('subtitle.style.font_size', 70)
    
    return f"""[Script Info]
Title: Converted from SRT
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: 英,{english_font},{font_size},&H00FFFFFF,&H000000FF,&H003B3C3D,&H00000000,0,0,0,0,100,100,1,0,1,1,0.1,2,0,0,5,1
Style: 中,{chinese_font},{font_size},&H00FFFFFF,&H000000FF,&H00200090,&H00000000,-1,0,0,0,110,100,1,0,1,2.5,1.5,2,0,0,57,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def convert_to_srt_format(start_time, end_time):
    """Convert time (in seconds) to the format: hours:minutes:seconds,milliseconds"""
    def seconds_to_hmsm(seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds = seconds % 60
        milliseconds = int(seconds * 1000) % 1000
        return f"{hours:02d}:{minutes:02d}:{int(seconds):02d},{milliseconds:03d}"

    start_srt = seconds_to_hmsm(start_time)
    end_srt = seconds_to_hmsm(end_time)
    return f"{start_srt} --> {end_srt}"

def remove_punctuation(text):
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text.strip()

def srt_time_to_ass_time(srt_time):
    """Convert SRT time format (HH:MM:SS,mmm) to ASS time format (H:MM:SS.cc) with rounding"""
    # Replace comma with period
    time_str = srt_time.replace(',', '.')
    time_parts = time_str.split(':')
    
    if len(time_parts) == 3:
        hours = str(int(time_parts[0]))  # Remove leading zero
        minutes = time_parts[1]
        
        # Handle seconds and milliseconds
        seconds_part = time_parts[2]
        if '.' in seconds_part:
            seconds_str, milliseconds_str = seconds_part.split('.')
            seconds = int(seconds_str)
            milliseconds = int(milliseconds_str)
            
            # Convert milliseconds to centiseconds with proper rounding
            centiseconds = round(milliseconds / 10)
            
            # Handle overflow when centiseconds >= 100
            if centiseconds >= 100:
                seconds += 1
                centiseconds = 0
            
            return f"{hours}:{minutes}:{seconds:02d}.{centiseconds:02d}"
        else:
            return f"{hours}:{minutes}:{seconds_part}.00"
    
    return time_str

def convert_srt_to_ass(srt_content, ass_file_path):
    """Convert SRT text to ASS format with specified styles and layers"""
    try:
        # Parse SRT content
        subtitle_blocks = re.findall(r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n((?:.|\n)*?)(?=\n\n|\Z)', srt_content)
        
        ass_events = []
        entry_num = 1 # <-- 保留这一行
        
        for block in subtitle_blocks:
            start_time = srt_time_to_ass_time(block[1])
            end_time = srt_time_to_ass_time(block[2])
            text = block[3].strip().replace('\n', '\\N')
            
            if any('\u4e00' <= char <= '\u9fff' for char in text):
                lines = text.split('\\N')
                for i, line in enumerate(lines):
                    style = "中" if i % 2 == 0 else "英"
                    layer = 0 if style == "中" else 1
                    ass_events.append(f"Dialogue: {layer},{start_time},{end_time},{style},,0,0,0,,{line}")
            else:
                ass_events.append(f"Dialogue: 1,{start_time},{end_time},英,,0,0,0,,{text}")
        
        with open(ass_file_path, 'w', encoding='utf-8') as f:
            f.write(get_ass_header())
            f.write('\n'.join(ass_events))
        
        console.print(f"Successfully converted subtitles to {os.path.basename(ass_file_path)}")
        return True
        
    except Exception as e:
        console.print(f"Error converting SRT to ASS: {str(e)}")
        return False

def show_difference(str1, str2):
    """Show the difference positions between two strings"""
    min_len = min(len(str1), len(str2))
    diff_positions = []
    
    for i in range(min_len):
        if str1[i] != str2[i]:
            diff_positions.append(i)
    
    if len(str1) != len(str2):
        diff_positions.extend(range(min_len, max(len(str1), len(str2))))
    
    print("Difference positions:")
    print(f"Expected sentence: {str1}")
    print(f"Actual match: {str2}")
    print("Position markers: " + "".join("^" if i in diff_positions else " " for i in range(max(len(str1), len(str2)))))
    print(f"Difference indices: {diff_positions}")

def get_sentence_timestamps(df_words, df_sentences):
    time_stamp_list = []

    # Build complete string and position mapping
    full_words_str = ''
    position_to_word_idx = {}
    
    for idx, word in enumerate(df_words['text']):
        clean_word = remove_punctuation(word.lower())
        start_pos = len(full_words_str)
        full_words_str += clean_word
        for pos in range(start_pos, len(full_words_str)):
            position_to_word_idx[pos] = idx
    
    current_pos = 0
    for idx, sentence in df_sentences['source'].items():
        clean_sentence = remove_punctuation(sentence.lower()).replace(" ", "")
        sentence_len = len(clean_sentence)
        
        match_found = False
        while current_pos <= len(full_words_str) - sentence_len:
            if full_words_str[current_pos:current_pos+sentence_len] == clean_sentence:
                start_word_idx = position_to_word_idx[current_pos]
                end_word_idx = position_to_word_idx[current_pos + sentence_len - 1]
                
                time_stamp_list.append((
                    float(df_words['start'][start_word_idx]),
                    float(df_words['end'][end_word_idx])
                ))
                
                current_pos += sentence_len
                match_found = True
                break
            current_pos += 1
            
        if not match_found:
            print(f"\n⚠️ Warning: No exact match found for sentence: {sentence}")
            show_difference(clean_sentence, 
                          full_words_str[current_pos:current_pos+len(clean_sentence)])
            print("\nOriginal sentence:", df_sentences['source'][idx])
            raise ValueError("❎ No match found for sentence.")

    return time_stamp_list

def align_timestamp(df_text, df_translate):
    """Align timestamps and add a new timestamp column to df_translate"""
    df_trans_time = df_translate.copy()

    # Process timestamps ⏰
    time_stamp_list = get_sentence_timestamps(df_text, df_translate)
    df_trans_time['timestamp'] = time_stamp_list
    df_trans_time['duration'] = df_trans_time['timestamp'].apply(lambda x: x[1] - x[0])

    # Remove gaps 🕳️
    for i in range(len(df_trans_time)-1):
        delta_time = df_trans_time.loc[i+1, 'timestamp'][0] - df_trans_time.loc[i, 'timestamp'][1]
        if 0 < delta_time < 1:
            df_trans_time.at[i, 'timestamp'] = (df_trans_time.loc[i, 'timestamp'][0], df_trans_time.loc[i+1, 'timestamp'][0])

    # Convert start and end timestamps to SRT format
    df_trans_time['timestamp'] = df_trans_time['timestamp'].apply(lambda x: convert_to_srt_format(x[0], x[1]))

    # Polish subtitles: apply Chinese punctuation filtering
    df_trans_time['translation'] = df_trans_time['translation'].apply(filter_chinese_punctuation)

    return df_trans_time

def generate_bilingual_srt(df):
    """双语 SRT 文本：每句先原文条目、再译文条目，二者共用同一时间段"""
    entries = []
    entry_num = 1
    for i, row in df.iterrows():
        # First language entry
        entries.append(f"{entry_num}\n{row['timestamp']}\n{row['source'].strip()}\n")
        entry_num += 1
        # Second language entry  
        entries.append(f"{entry_num}\n{row['timestamp']}\n{row['translation'].strip()}\n")
        entry_num += 1
    return '\n\n'.join(entries).strip()

# ✨ Beautify the translation
def filter_chinese_punctuation(text):
    """
    过滤中文标点，只保留感叹号(！)、问号(？)和「」引号
    将其他引号替换成「」样式，并在需要的地方添加空格
    """
    if pd.isna(text):
        return ''
    text = str(text)
    
    # 处理各种引号，统一替换为「」
    quote_patterns = [
        r'"([^"]*)"',      # 英文双引号
        r'"([^"]*)"',      # 中文弯引号
        r"'([^']*)'",      # 英文单引号(直引号)
        r"'([^']*)'",      # 另一种英文单引号
        r'『([^』]*)』',    # 中文书名号样式
        r'《([^》]*)》',    # 书名号
        r'【([^】]*)】',    # 方括号
    ]
    
    # 替换所有引号样式为「」
    for pattern in quote_patterns:
        text = re.sub(pattern, r'「\1」', text)
    
    # 需要替换为空格的标点
    punctuation_to_space = ['，', '、', '；', '：', '。']
    
    # 需要直接删除的标点（不替换为空格）
    punctuation_to_remove = [
        '……', '——', '—',
        '（', '）', '【', '】', '《', '》',
        '『', '』', '〈', '〉', '〔', '〕',
        '〖', '〗', '〘', '〙', '〚', '〛',
        '﹝', '﹞', '﹙', '﹚', '﹛', '﹜',
        '﹤', '﹥'
    ]
    
    # 替换为空格
    for punct in punctuation_to_space:
        text = text.replace(punct, ' ')
    
    # 直接删除
    for punct in punctuation_to_remove:
        text = text.replace(punct, '')
    
    # 清理多余空格
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def clean_translation(x):
    if pd.isna(x):
        return ''
    # Apply comprehensive punctuation filtering
    cleaned = filter_chinese_punctuation(str(x))
    return autocorrect.format(cleaned)

def align_timestamp_main():
    df_text = pd.DataFrame(read_json(paths.TRANSCRIPT)["words"], columns=["text", "start", "end"])
    df_translate = pd.DataFrame(read_json(paths.SUBTITLE_LINES), columns=["source", "translation"])
    df_translate['translation'] = df_translate['translation'].apply(clean_translation)
    
    df_trans_time = align_timestamp(df_text, df_translate)
    paths.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    convert_srt_to_ass(generate_bilingual_srt(df_trans_time), paths.FINAL_ASS)
    console.print(Panel("[bold green]🎉📝 Subtitles generation completed! Please check in the `output` folder 👀[/bold green]"))
