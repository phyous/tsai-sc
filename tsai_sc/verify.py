"""Verify a completed run's recorded evidence before publishing it."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import statistics
from PIL import Image
from . import combat
from .game import MISSIONS
from .render import load_trace, resolve_frame
from .typesafe import validate_response


def verify(directory):
    root = Path(directory)
    result = json.loads((root / 'result.json').read_text())
    if not isinstance(result, dict):
        raise ValueError('Recorded result must be an object')
    evidence = result.get('engine_evidence', {})
    if not isinstance(evidence, dict):
        raise ValueError('Recorded engine evidence must be an object')
    if result.get('status') != 'victory' or evidence.get('victory_value') != 3:
        raise ValueError('No original-engine victory was recorded')
    map_path = evidence.get('map_path')
    mission = MISSIONS.get(map_path.lower().replace('/', '\\')) if isinstance(map_path, str) else None
    if mission is None:
        raise ValueError('Outcome does not identify a supported original demo mission')
    victory_image = result.get('visible_victory_frame')
    victory_image_hash = result.get('visible_victory_frame_sha256')
    # A combat run must retain the original game's visible result presentation.
    # Hash/path checks establish image integrity, not recognition of its pixels.
    if mission['kind'] == 'combat' and not victory_image:
        raise ValueError('Combat victory requires a retained original victory-screen image')
    if victory_image is not None or victory_image_hash is not None:
        image_path = resolve_frame(root, victory_image)
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != victory_image_hash:
            raise ValueError('Victory-screen image hash differs from the recorded result')
        with Image.open(image_path) as captured:
            if captured.format != 'PNG' or captured.size != (640, 480) or captured.convert('RGB').getbbox() is None:
                raise ValueError('Victory-screen image must be a nonblank original 640x480 PNG')
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
    attempts = rejected = rejected_tokens = unknown_usage = 0
    for decision in decisions:
        response = validate_response(decision['response'], decision['request']['questions'])
        routing = decision.get('routing')
        candidates = decision.get('candidates')
        if routing is not None:
            if not isinstance(candidates, dict) or routing != combat.graph_routing(candidates):
                raise ValueError('Recorded graph routing differs from the candidate command categories')
            questions = decision['request']['questions']
            expected_questions = {routing['root_question']}
            if set(questions[routing['root_question']]['criteria']) != set(routing['branches']):
                raise ValueError('Recorded graph categories differ from the model question')
            for branch in routing['branches'].values():
                if branch['question'] is not None:
                    expected_questions.add(branch['question'])
                    if set(questions.get(branch['question'], {}).get('criteria', {})) != set(branch['candidate_ids']):
                        raise ValueError('Recorded graph branch differs from the model action question')
            if set(questions) != expected_questions:
                raise ValueError('Recorded graph questions do not match the routed branches')
            choice, child = combat.resolve_graph_choice(response, routing)
            graph = decision['response'].get('metadata', {}).get('decision_graph', {})
            if (graph.get('intent_question') != routing['root_question']
                    or graph.get('selected_intent') != response['answers'][routing['root_question']]['choice']
                    or graph.get('selected_action_question') != child or graph.get('selected_candidate') != choice):
                raise ValueError('Recorded graph display does not match the model-selected path')
        else:
            choice = response['answers']['action']['choice']
        if choice != decision['selected']:
            raise ValueError('Executed selection differs from the recorded model choice')
        if candidates is not None or mission['kind'] == 'combat':
            if (not isinstance(candidates, dict) or (routing is None and set(candidates) != set(decision['request']['questions']['action']['criteria']))
                    or decision.get('action') != candidates.get(choice)):
                raise ValueError('Recorded command differs from the model-selected candidate')
        models.add(response['model'])
        tokens += response['usage']['input_tokens']
        metadata = decision['response']['metadata']
        latency.append(metadata['latency_ms'])
        attempts += metadata.get('attempts', 1)
        rejected += metadata.get('rejected_response_attempts', 0)
        rejected_tokens += metadata.get('rejected_input_tokens', 0)
        unknown_usage += metadata.get('rejected_usage_unavailable_attempts', 0)
        for event in decision['input_result']['inputs']:
            if event['command'] not in {'clickHold', 'keyHold', 'key', 'move'}:
                raise ValueError('Unexpected command outside ordinary game inputs')
    final = result['final_state']
    if 'api_attempts' in result and result['api_attempts'] != attempts:
        raise ValueError('API-attempt count does not match the decision log')
    if 'accounted_input_tokens' in result and result['accounted_input_tokens'] != tokens + rejected_tokens:
        raise ValueError('Accounted input usage does not match the decision log')
    return {
        'mission': f'Original StarCraft shareware {mission["name"]} {mission["kind"]} mission',
        'outcome': 'victory', 'models': sorted(models),
        'elapsed_wall_seconds': round(result['elapsed_seconds'], 3),
        'model_calls': len(decisions), 'captured_frames': len(frames),
        'input_tokens': tokens + rejected_tokens,
        'api_attempts': attempts, 'rejected_response_attempts': rejected,
        'rejected_usage_unavailable_attempts': unknown_usage,
        'median_api_latency_ms': round(statistics.median(latency), 2),
        'mean_api_latency_ms': round(statistics.mean(latency), 2),
        'final_objectives': final['objective_progress'],
        'engine_evidence': evidence,
        'visible_victory_frame': victory_image,
        'visible_victory_frame_sha256': victory_image_hash,
        'victory_screen_integrity_checked': victory_image is not None,
        'visual_review': 'A human must inspect the retained image for the original victory dialog; this verifier does not recognize screen text',
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
