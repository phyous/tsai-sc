"""Verify a completed run's recorded evidence before publishing it."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import statistics
from .render import load_trace
from .typesafe import validate_response


def verify(directory):
    root = Path(directory)
    result = json.loads((root / 'result.json').read_text())
    evidence = result.get('engine_evidence', {})
    if result.get('status') != 'victory' or evidence.get('victory_value') != 3:
        raise ValueError('No original-engine victory was recorded')
    if evidence.get('map_path', '').lower().replace('/', '\\') != 'campaign\\terraned\\tutorial':
        raise ValueError('Outcome does not identify the original Boot Camp mission')
    for filename, key in [('trace.jsonl', 'trace_sha256'), ('decisions.jsonl', 'decisions_sha256')]:
        digest = hashlib.sha256((root / filename).read_bytes()).hexdigest()
        if digest != result.get(key):
            raise ValueError('Recorded evidence hash differs: ' + filename)
    frames = load_trace(root)
    if frames[-1]['status'] != 'victory':
        raise ValueError('The recording does not end at victory')
    decisions = [json.loads(line) for line in (root / 'decisions.jsonl').read_text().splitlines() if line.strip()]
    if not decisions or len(decisions) != result.get('model_calls'):
        raise ValueError('Model-call count does not match the decision log')
    tokens, latency, models = 0, [], set()
    for decision in decisions:
        response = validate_response(decision['response'], decision['request']['questions'])
        choice = response['answers']['action']['choice']
        if choice != decision['selected']:
            raise ValueError('Executed selection differs from the recorded model choice')
        models.add(response['model'])
        tokens += response['usage']['input_tokens']
        latency.append(decision['response']['metadata']['latency_ms'])
        for event in decision['input_result']['inputs']:
            if event['command'] not in {'clickHold', 'keyHold', 'move'}:
                raise ValueError('Unexpected command outside ordinary game inputs')
    final = result['final_state']
    return {
        'mission': 'Original StarCraft shareware Boot Camp tutorial',
        'outcome': 'victory', 'models': sorted(models),
        'elapsed_wall_seconds': round(result['elapsed_seconds'], 3),
        'model_calls': len(decisions), 'captured_frames': len(frames),
        'input_tokens': tokens, 'median_api_latency_ms': round(statistics.median(latency), 2),
        'mean_api_latency_ms': round(statistics.mean(latency), 2),
        'final_objectives': final['objective_progress'],
        'engine_evidence': evidence,
        'trace_sha256': result['trace_sha256'], 'decisions_sha256': result['decisions_sha256'],
        'control': 'Structured visible/owned game state; normal keyboard/mouse inputs; paused for snapshots and model inference',
        'probabilities': 'Actual Jev action distributions, not win probabilities',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir')
    parser.add_argument('--output')
    args = parser.parse_args()
    summary = verify(args.run_dir)
    text = json.dumps(summary, indent=2) + '\n'
    if args.output:
        Path(args.output).write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
