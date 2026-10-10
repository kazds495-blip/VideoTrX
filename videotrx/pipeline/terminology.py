import json
from videotrx import paths
from videotrx.prompts import get_summary_prompt
from rich import print as rprint
from videotrx import config
from videotrx.llm import ask_json
from videotrx.utils import read_json

def combine_chunks():
    """把语义断句结果合并成一段文本，只取前 terminology.source_chars 个字符"""
    sentences = read_json(paths.MEANING_SPLIT)
    cleaned_sentences = [line.strip() for line in sentences]
    combined_text = ' '.join(cleaned_sentences)
    return combined_text[:config.get('terminology.source_chars')]  #! Return only the first x characters

def search_things_to_note_in_prompt(sentence):
    """Search for terms to note in the given sentence"""
    things_to_note = read_json(paths.TERMINOLOGY)
    things_to_note_list = [term['src'] for term in things_to_note['terms'] if term['src'].lower() in sentence.lower()]
    if things_to_note_list:
        prompt = '\n'.join(
            f'{i+1}. "{term["src"]}": "{term["tgt"]}",'
            f' meaning: {term["note"]}'
            for i, term in enumerate(things_to_note['terms'])
            if term['src'] in things_to_note_list
        )
        return prompt
    else:
        return None

def get_summary():
    src_content = combine_chunks()
    custom_terms = read_json(paths.CUSTOM_TERMS) if paths.CUSTOM_TERMS.exists() else []
    custom_terms_json = {
        "terms": 
            [
                {
                    "src": str(term["src"]),
                    "tgt": str(term["tgt"]), 
                    "note": str(term.get("note") or "")
                }
                for term in custom_terms
            ]
    }
    if len(custom_terms) > 0:
        rprint(f"📖 Custom Terms Loaded: {len(custom_terms)} terms")
        rprint("📝 Terms Content:", json.dumps(custom_terms_json, indent=2, ensure_ascii=False))
    summary_prompt = get_summary_prompt(src_content, custom_terms_json)
    rprint("📝 Summarizing and extracting terminology ...")
    
    def valid_summary(response_data):
        required_keys = {'src', 'tgt', 'note'}
        if 'terms' not in response_data:
            return {"status": "error", "message": "Invalid response format"}
        for term in response_data['terms']:
            if not all(key in term for key in required_keys):
                return {"status": "error", "message": "Invalid response format"}   
        return {"status": "success", "message": "Summary completed"}

    summary_schema = {
        "type": "object",
        "properties": {
            "theme": {"type": "string"},
            "terms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "src": {"type": "string"},
                        "tgt": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["src", "tgt", "note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["theme", "terms"],
        "additionalProperties": False,
    }
    summary = ask_json(
        summary_prompt,
        summary_schema,
        valid_def=valid_summary,
        log_title='summary',
    )
    summary['terms'].extend(custom_terms_json['terms'])
    
    with open(paths.TERMINOLOGY, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=4)

    rprint(f'💾 Summary log saved to → `{paths.TERMINOLOGY}`')
