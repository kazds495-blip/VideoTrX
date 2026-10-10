import concurrent.futures
import math

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from videotrx import config, paths
from videotrx.llm import ask_json
from videotrx.pipeline.polish import (
    _cleanup_single_pass_full_polish_progress,
    _write_polish_changelog,
    polish_single_pass_full_text,
)
from videotrx.pipeline.subtitle_render import get_sentence_timestamps
from videotrx.pipeline.subtitle_split import target_display_length
from videotrx.pipeline.terminology import search_things_to_note_in_prompt
from videotrx.prompts import generate_shared_prompt, get_prompt_single_pass
from videotrx.utils import read_json, write_json

console = Console()


def adjacent_line(n, line_count):
    """空行 n 的相邻行：n > 1 取上一行，n = 1 取下一行；整块只有一行时没有相邻行。"""
    if n > 1:
        return n - 1
    return 2 if line_count > 1 else None


def empty_line_feedback(result, empty_lines, line_count):
    """根据上一次的结果，为每个空行生成一条反馈，拼成追加到提示词末尾的段落。"""
    limit = config.get("subtitle.target_max_length")
    feedback = []
    for n in empty_lines:
        m = adjacent_line(n, line_count)
        if m is None:
            feedback.append(f"第 {n} 行是空行，请输出这一行的译文。")
            continue
        width = target_display_length(result[str(m)])
        # 与字幕切分相同：小数部分不计
        if math.floor(width) > limit:
            feedback.append(
                f"第 {n} 行是空行，而第 {m} 行有 {width:.1f} 个汉字宽，超出单行上限 {limit} 约 {width - limit:.1f}，"
                f"放不进一行。请把第 {m} 行和第 {n} 行的意思分配到这两行，每行各有自己的内容。"
            )
        else:
            feedback.append(
                f"第 {n} 行是空行。若第 {n} 行的意思已并入第 {m} 行，请让第 {n} 行输出与第 {m} 行完全相同的译文（重复行）。"
            )
    return "\n\n## 上次输出的问题\n上次输出中有空行，本次请修正：\n" + "\n".join(f"- {item}" for item in feedback)


def fill_empty_lines(result, line_count):
    """把空行填成相邻行的译文（重复行）；连续空行沿同一方向接续填充。"""
    lines = [result[str(i)] for i in range(1, line_count + 1)]
    for i in range(1, line_count):
        if not lines[i].strip():
            lines[i] = lines[i - 1]
    for i in range(line_count - 2, -1, -1):
        if not lines[i].strip():
            lines[i] = lines[i + 1]
    return {str(i): line for i, line in enumerate(lines, 1)}


def translate_lines(
    lines,
    previous_content_prompt,
    after_cotent_prompt,
    things_to_note_prompt,
    summary_prompt,
    index=0,
):
    shared_prompt = generate_shared_prompt(
        previous_content_prompt,
        after_cotent_prompt,
        summary_prompt,
        things_to_note_prompt,
    )
    line_count = len(lines.split("\n"))

    # Retry translation if the length of the original text and the translated text are not the same,
    # or if the specified key is missing.
    def retry_translation(prompt, length):
        def valid_single_pass(response_data):
            if not isinstance(response_data, dict):
                return {"status": "error", "message": "Response is not a JSON object"}
            expected = [str(i) for i in range(1, length + 1)]
            if set(response_data.keys()) != set(expected):
                missing = sorted(set(expected) - set(response_data.keys()))
                extra = sorted(set(response_data.keys()) - set(expected))
                return {
                    "status": "error",
                    "message": f"Line key mismatch. missing={missing} extra={extra}",
                }
            for key in expected:
                if not isinstance(response_data[key], str):
                    return {
                        "status": "error",
                        "message": f"Line {key} must be a string",
                    }
            return {"status": "success", "message": "Translation completed"}

        best_single_pass = None
        retry_prompt = prompt
        for retry in range(3):
            # 行号顶层键由 schema 严格约束：strict tool use 下模型无法再包一层
            line_keys = [str(i) for i in range(1, length + 1)]
            result = ask_json(
                retry_prompt,
                {
                    "type": "object",
                    "properties": {key: {"type": "string"} for key in line_keys},
                    "required": line_keys,
                    "additionalProperties": False,
                },
                valid_def=valid_single_pass,
                log_title="translate_single_pass",
                attempt=retry,
            )
            empty_lines = [int(key) for key in line_keys if not result[key].strip()]
            if empty_lines:
                if best_single_pass is None or len(empty_lines) < best_single_pass[0]:
                    best_single_pass = (len(empty_lines), result)
                console.print(
                    f"[yellow]Warning: single_pass translation of block {index} "
                    f"has empty lines {empty_lines}, retry...[/yellow]"
                )
                # 反馈只根据上一次的结果生成
                retry_prompt = prompt + empty_line_feedback(result, empty_lines, length)
                continue

            if len(result) == line_count:
                return result
            if retry != 2:
                console.print(
                    f"[yellow]Warning: single_pass translation of block {index} failed, retry...[/yellow]"
                )
        # 取空行最少的一次，空行填成相邻行的译文（重复行），不再往下游传
        if best_single_pass is not None:
            console.print(
                f"[yellow]Warning: filling {best_single_pass[0]} empty line(s) of block {index} "
                f"with adjacent lines.[/yellow]"
            )
            return fill_empty_lines(best_single_pass[1], length)
        raise ValueError(
            f"single_pass translation of block {index} failed after 3 retries. "
            "Please check `output/gpt_log/error.json` for more details."
        )

    # Single-pass mode: one API call with merged prompt.
    prompt_single_pass = get_prompt_single_pass(lines, shared_prompt)
    single_pass_result = retry_translation(prompt_single_pass, line_count)

    # 按 1..N 顺序取值，不依赖模型返回的 JSON 键顺序。
    source_lines = lines.split("\n")
    cleaned_result = {}
    for i in range(1, line_count + 1):
        cleaned_result[str(i)] = single_pass_result[str(i)].replace("\n", " ").strip()

    table = Table(title="Translation Results", show_header=False, box=box.ROUNDED)
    table.add_column("Translations", style="bold")
    for i in range(1, line_count + 1):
        table.add_row(f"[cyan]Origin:  {source_lines[i - 1]}[/cyan]")
        table.add_row(f"[green]Free:    {cleaned_result[str(i)]}[/green]")
        if i < line_count:
            table.add_row("[yellow]" + "-" * 50 + "[/yellow]")
    console.print(table)

    translate_result = "\n".join(
        [cleaned_result[str(i)] for i in range(1, line_count + 1)]
    )
    if line_count != len(translate_result.split("\n")):
        console.print(
            Panel(
                "[red]Translation failed (length mismatch). "
                "Please check `output/gpt_log/translate_single_pass.json`[/red]"
            )
        )
        raise ValueError(f"Origin >>>{lines}<<<,\nbut got >>>{translate_result}<<<")
    return translate_result, lines


# 拆分文本块的函数
def split_chunks_by_chars(chunk_size, max_i): 
    """根据字符数将文本拆分为块，返回多行文本块列表"""
    sentences = read_json(paths.MEANING_SPLIT)

    chunks = []
    chunk = ''
    sentence_count = 0
    for sentence in sentences:
        if len(chunk) + len(sentence + '\n') > chunk_size or sentence_count == max_i:
            chunks.append(chunk.strip())
            chunk = sentence + '\n'
            sentence_count = 1
        else:
            chunk += sentence + '\n'
            sentence_count += 1
    chunks.append(chunk.strip())
    return chunks

# 获取相邻块的上下文
def get_previous_content(chunks, chunk_index):
    return None if chunk_index == 0 else chunks[chunk_index - 1].split('\n')[-8:] # 获取最后8行作为上下文
def get_after_content(chunks, chunk_index):
    return None if chunk_index == len(chunks) - 1 else chunks[chunk_index + 1].split('\n')[:8] # 获取前8行作为上下文

# 🔍 翻译单个块
def translate_chunk(chunk, chunks, theme_prompt, i):
    things_to_note_prompt = search_things_to_note_in_prompt(chunk)
    previous_content_prompt = get_previous_content(chunks, i)
    after_content_prompt = get_after_content(chunks, i)
    translation, english_result = translate_lines(chunk, previous_content_prompt, after_content_prompt, things_to_note_prompt, theme_prompt, i)
    return i, english_result, translation

# 🚀 翻译所有块的主函数
def translate_all():
    console.print("[bold green]Start Translating All...[/bold green]")
    chunks = split_chunks_by_chars(chunk_size=600, max_i=10)
    theme_prompt = read_json(paths.TERMINOLOGY).get('theme')

    # 🔄 使用并发执行翻译
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        task = progress.add_task("[cyan]Translating chunks...", total=len(chunks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.get("max_workers")) as executor:
            futures = []
            for i, chunk in enumerate(chunks):
                future = executor.submit(translate_chunk, chunk, chunks, theme_prompt, i)
                futures.append(future)
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
                progress.update(task, advance=1)

    results.sort(key=lambda x: x[0])  # 按原始顺序排序结果
    
    # 💾 保存结果到列表
    src_text, trans_text = [], []
    for i, chunk in enumerate(chunks):
        chunk_lines = chunk.split('\n')
        src_text.extend(chunk_lines)
        
        trans_text.extend(results[i][2].split('\n'))

    # add one full-text polish pass after chunk translation
    console.print("[cyan]Running full-text polish...[/cyan]")
    try:
        trans_text = polish_single_pass_full_text(src_text, trans_text, theme_prompt)
        console.print("[green]✅ Full-text polish completed.[/green]")
    except Exception as e:
        console.print(
            "[yellow]Warning: Full-text polish failed; fallback to original single-pass result. "
            f"Reason: {e}[/yellow]"
        )
        _write_polish_changelog(
            src_text, trans_text, trans_text,
            stage="polish_fallback", note=str(e),
        )

    # 只校验每句原文都能与转写词对上，时间戳在生成字幕时再算
    words_df = pd.DataFrame(read_json(paths.TRANSCRIPT)["words"], columns=["text", "start", "end"])
    df_translate = pd.DataFrame({'source': src_text, 'translation': trans_text})
    get_sentence_timestamps(words_df, df_translate)
    console.print(df_translate)

    write_json(paths.TRANSLATION, df_translate.to_dict("records"))
    _cleanup_single_pass_full_polish_progress()
    console.print("[bold green]✅ Translation completed and results saved.[/bold green]")
