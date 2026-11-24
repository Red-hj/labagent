"""Exercise real video OCR, operation reconstruction and optional model diagnosis."""
import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent.recordings import Recordings
from labagent.investigation import source_bundle
from labagent.operation_history import inspect_history
from labagent.material_service import MaterialService
from labagent.model import load_env
from labagent.report import generate_report


def command_key(event):
    return event['device'].casefold(), ' '.join(event['command'].casefold().split())


def _pair_timed_events(labels, candidates, tolerance):
    """Match repeated commands in order, maximizing hits and then minimizing time error."""
    events = sorted((event for event in candidates if event['seconds'] is not None),
                    key=lambda event: event['seconds'])
    label_count, event_count = len(labels), len(events)
    scores = [[(0, 0.0)] * (event_count + 1) for _ in range(label_count + 1)]
    matched = [[False] * event_count for _ in labels]

    # Each cell scores the best remaining assignment from these two positions.
    for label_index in range(label_count - 1, -1, -1):
        for event_index in range(event_count - 1, -1, -1):
            best = max(scores[label_index + 1][event_index], scores[label_index][event_index + 1])
            error = abs(labels[label_index]['seconds'] - events[event_index]['seconds'])
            if error <= tolerance:
                count, negative_error = scores[label_index + 1][event_index + 1]
                score = count + 1, negative_error - error
                if score >= best:
                    best = score
                    matched[label_index][event_index] = True
            scores[label_index][event_index] = best

    pairs = {}
    label_index = event_index = 0
    while label_index < label_count and event_index < event_count:
        if matched[label_index][event_index]:
            pairs[labels[label_index]['id']] = events[event_index]
            label_index += 1
            event_index += 1
        elif scores[label_index][event_index] == scores[label_index + 1][event_index]:
            label_index += 1
        else:
            event_index += 1
    return pairs


def score_events(history, annotations, *, tolerance=0.5):
    """Pair a clip's annotated commands with reconstructed events once each."""
    expected = annotations['events']
    if not expected or len({item['id'] for item in expected}) != len(expected):
        raise ValueError('标注需要至少一条操作及唯一编号')
    predictions = history['events']
    labels_by_command, events_by_command = defaultdict(list), defaultdict(list)
    for label in sorted(expected, key=lambda item: item['seconds']):
        labels_by_command[command_key(label)].append(label)
    for event in predictions:
        events_by_command[command_key(event)].append(event)

    used, pairs = set(), {}
    for key, labels in labels_by_command.items():
        candidates = events_by_command[key]
        timed_pairs = _pair_timed_events(labels, candidates, tolerance)
        pairs.update(timed_pairs)
        used.update(event['id'] for event in timed_pairs.values())

        # Keep time misses visible in the command, view and status scores.
        for label in labels:
            if label['id'] in pairs:
                continue
            remaining = [event for event in candidates if event['id'] not in used]
            if remaining:
                event = min(remaining, key=lambda item: (abs(item['seconds'] - label['seconds'])
                                                         if item['seconds'] is not None else float('inf')))
                pairs[label['id']] = event
                used.add(event['id'])

    rows = []
    for label in expected:
        event = pairs.get(label['id'])
        seconds = event['seconds'] if event else None
        error = abs(seconds - label['seconds']) if seconds is not None else None
        retry = pairs.get(label.get('retry_to'))
        rows.append({'label': label['id'], 'event': event['id'] if event else None,
                     'expected_seconds': label['seconds'], 'observed_seconds': seconds,
                     'view_matches': bool(event and event['view'] == label['view']),
                     'status_matches': bool(event and event['status'] == label['status']),
                     'in_time': error is not None and error <= tolerance,
                     'retry_matches': None if not label.get('retry_to') else
                                      bool(event and retry and event.get('retry_event_id') == retry['id'])})

    summary = {'commands': {'matched': len(pairs), 'total': len(rows)},
               'unmatched_detected_events': len(predictions) - len(used)}
    for field in ('view_matches', 'status_matches', 'in_time', 'retry_matches'):
        values = [row[field] for row in rows if row[field] is not None]
        summary[field] = {'matched': sum(values), 'total': len(values)}
    return {'tolerance_seconds': tolerance, 'summary': summary, 'rows': rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--video", type=Path, help="待验证的终端录屏，默认使用项目合成示例")
    parser.add_argument("--annotations", type=Path, help="包含 events 的独立标注 JSON")
    parser.add_argument("--output", type=Path, help="项目 runtime/ 内尚未使用的结果目录")
    args = parser.parse_args()
    if args.live_model:
        load_env(ROOT / ".env")
    folder = (args.output or ROOT / "runtime/replay-verification" / time.strftime("%Y%m%d-%H%M%S")).resolve()
    runtime = (ROOT / "runtime").resolve()
    if folder == runtime or not folder.is_relative_to(runtime) or folder.exists():
        parser.error("请选择 runtime/ 内尚未使用的输出目录")
    video = args.video or ROOT / "data/examples/实验操作回放.webm"
    annotations = json.loads(args.annotations.read_text(encoding="utf-8-sig")) if args.annotations else None
    service = Recordings(folder / "recordings")
    with video.open("rb") as stream:
        job = service.upload(stream, video.stat().st_size, video.name)
    started = time.monotonic()
    while time.monotonic() - started < 240:
        status = service.status(job["id"])
        print(json.dumps({key: status.get(key) for key in ("status", "progress", "message")}, ensure_ascii=False), flush=True)
        if status["status"] == "error":
            raise RuntimeError(status["message"])
        if status["status"] == "ready":
            break
        time.sleep(3)
    else:
        raise RuntimeError("Video extraction timed out")
    materials = {status["material_name"]: status["transcript"]}
    if args.video is None:
        materials["实验要求.txt"] = "实验目标：R1通过OSPF访问R2的Loopback0地址10.2.2.2，Ping全部成功。"
    operations = inspect_history(source_bundle({"configs": materials})[0])
    (folder / "operations.json").write_text(json.dumps(operations, ensure_ascii=False, indent=2), encoding="utf-8")
    issues = [event for event in operations["events"] if event["status"] == "error"]
    issue = issues[0] if issues else None
    if args.video is None:
        assert issue, "No CLI error found in actual OCR transcript"
        assert issue["device"] == "R2" and issue["view"] == "interface"
        assert abs(issue["seconds"] - 3) <= 0.5
    if annotations is not None:
        scores = score_events(operations, annotations)
        (folder / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(scores["summary"], ensure_ascii=False), flush=True)
    if args.live_model:
        app = MaterialService(folder / "sessions.sqlite3")
        response = app.invoke({"materials": materials, "question": "回溯录屏中的操作，解释关键错误步骤及怎样继续。",
                               "evidence_kind": "user_submitted" if args.video else "demo", "recordings": [job["id"]]})
        (folder / "diagnosis.json").write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        assert response["status"] == "success", response["reply"]
        assert any(tool["tool"] == "inspect_operation_timeline" and tool["status"] == "ok" for tool in response["result"]["llm"]["tool_trace"])
        (folder / "report.md").write_text(generate_report(app.store.history(response["session_id"])), encoding="utf-8")
        print(json.dumps({"session_id": response["session_id"], "reply": response["reply"]}, ensure_ascii=False), flush=True)
    completion = {"ocr_completed": True, "folder": str(folder), "ocr_events": operations["event_count"],
                  "issue_time": issue["time_label"] if issue else None}
    if args.video is None:
        completion["passed"] = True
    print(json.dumps(completion, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
