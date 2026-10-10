import concurrent.futures
from difflib import SequenceMatcher
import math
from videotrx import paths
from videotrx.prompts import get_split_prompt
from videotrx import config
from videotrx.language import get_joiner, source_language
from videotrx.llm import ask_gpt
from videotrx.utils import read_json, write_json
from rich.console import Console
from rich.table import Table
console = Console()

def find_split_positions(original, modified):
    split_positions = []
    parts = modified.split('[br]')
    start = 0
    language = source_language()
    joiner = get_joiner(language)

    for i in range(len(parts) - 1):
        max_similarity = 0
        best_split = None

        for j in range(start, len(original)):
            original_left = original[start:j]
            modified_left = joiner.join(parts[i].split())

            left_similarity = SequenceMatcher(None, original_left, modified_left).ratio()

            if left_similarity > max_similarity:
                max_similarity = left_similarity
                best_split = j

        if max_similarity < 0.9:
            console.print(f"[yellow]Warning: low similarity found at the best split point: {max_similarity}[/yellow]")
        if best_split is not None:
            split_positions.append(best_split)
            start = best_split
        else:
            console.print(f"[yellow]Warning: Unable to find a suitable split point for the {i+1}th part.[/yellow]")

    return split_positions

def split_sentence(sentence, num_parts, char_limit=100, index=-1, retry_attempt=0):
    """Split a long sentence using GPT and return the result as a string."""
    split_prompt = get_split_prompt(sentence, num_parts, char_limit)

    def valid_split(response_data):
        if not isinstance(response_data, dict) or "split" not in response_data:
            return {"status": "error", "message": "Missing required key: `split`"}
        if not isinstance(response_data["split"], str):
            return {"status": "error", "message": "Invalid value for `split`: expected a string"}
        split_text = response_data["split"]
        if "[br]" not in split_text:
            return {"status": "error", "message": "Split failed, no [br] found"}

        parts = split_text.split("[br]")
        if any(not part.strip() for part in parts):
            return {"status": "error", "message": "Split contains an empty part"}

        original_text = " ".join(str(sentence).split())
        returned_text = " ".join("".join(parts).split())
        if returned_text != original_text:
            matcher = SequenceMatcher(None, original_text, returned_text)
            changed_characters = sum(
                max(original_end - original_start, returned_end - returned_start)
                for tag, original_start, original_end, returned_start, returned_end
                in matcher.get_opcodes()
                if tag != "equal"
            )
            if changed_characters > 2 or matcher.ratio() < 0.96:
                return {
                    "status": "error",
                    "message": (
                        "Split changed the source text beyond the allowed tolerance: "
                        f"changed_characters={changed_characters}"
                    ),
                }

        return {"status": "success", "message": "Split completed"}
    
    response_data = ask_gpt(
        split_prompt + " " * retry_attempt,
        resp_type='json',
        valid_def=valid_split,
        log_title='split_by_meaning',
        json_schema={
            "type": "object",
            "properties": {"split": {"type": "string"}},
            "required": ["split"],
            "additionalProperties": False,
        },
    )
    validation = valid_split(response_data)
    if validation["status"] != "success":
        raise ValueError(f"API response error: {validation['message']}")
    best_split = response_data["split"]
    split_points = find_split_positions(sentence, best_split)
    # split the sentence based on the split points
    for i, split_point in enumerate(split_points):
        if i == 0:
            best_split = sentence[:split_point] + '\n' + sentence[split_point:]
        else:
            parts = best_split.split('\n')
            last_part = parts[-1]
            parts[-1] = last_part[:split_point - split_points[i-1]] + '\n' + last_part[split_point - split_points[i-1]:]
            best_split = '\n'.join(parts)
    if index != -1:
        console.print(f'[green]✅ Sentence {index} has been successfully split[/green]')
    table = Table(title="")
    table.add_column("Type", style="cyan")
    table.add_column("Sentence")
    table.add_row("Original", sentence, style="yellow")
    table.add_row("Split", best_split.replace('\n', ' ||'), style="yellow")
    console.print(table)
    
    return best_split

def parallel_split_sentences(sentences, max_length, max_workers, retry_attempt=0):
    """Split sentences in parallel using a thread pool."""
    new_sentences = [None] * len(sentences)
    futures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, sentence in enumerate(sentences):
            # 按字符总数判断，空格和标点均计入。
            num_parts = math.ceil(len(sentence) / max_length)
            if len(sentence) > max_length:
                future = executor.submit(split_sentence, sentence, num_parts, max_length, index=index, retry_attempt=retry_attempt)
                futures.append((future, index, num_parts, sentence))
            else:
                new_sentences[index] = [sentence]

        for future, index, num_parts, sentence in futures:
            split_result = future.result()
            if split_result:
                split_lines = split_result.strip().split('\n')
                new_sentences[index] = [line.strip() for line in split_lines]
            else:
                new_sentences[index] = [sentence]

    return [sentence for sublist in new_sentences for sentence in sublist]

def split_sentences_by_meaning():
    """The main function to split sentences by meaning."""
    # read input sentences
    sentences = [line.strip() for line in read_json(paths.NLP_SPLIT)]

    # 🔄 process sentences multiple times to ensure all are split
    for retry_attempt in range(3):
        sentences = parallel_split_sentences(sentences, max_length=config.get("subtitle.source_max_length"), max_workers=config.get("max_workers"), retry_attempt=retry_attempt)

    # 💾 save results
    write_json(paths.MEANING_SPLIT, sentences)
    console.print('[green]✅ All sentences have been successfully split![/green]')
