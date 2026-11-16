"""Check imported real FRR observations and optional live Agent explanation."""
import argparse,json,sys
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from labagent.material_service import MaterialService
from labagent.model import load_env
from labagent.repair_lab import compare_run
from labagent.report import generate_report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live-model',action='store_true');args=parser.parse_args()
    load_env(ROOT/'.env');sample=ROOT/'data/examples/frr_repair_run.json'
    comparison=compare_run(json.loads(sample.read_text(encoding='utf-8')))
    assert [phase['passed_count'] for phase in comparison['phases']]==[1,1,3]
    folder=ROOT/'runtime/repair-verification';folder.mkdir(exist_ok=True)
    service=MaterialService(folder/'sessions.sqlite3')
    response=service.invoke({'materials':{'FRR修复实验.json':sample.read_text(encoding='utf-8')},'question':'比较实际实验中两种修复方案：为什么Ping通了仍未完成实验？推荐满足全部目标的方案并解释配置改动。','use_llm':args.live_model})
    (folder/'diagnosis.json').write_text(json.dumps(response,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'report.md').write_text(generate_report(service.store.history(response['session_id'])),encoding='utf-8')
    if args.live_model:
        assert response['status']=='success',response['reply']
        assert any(item['tool']=='compare_repair_candidates' and item['status']=='ok' for item in response['result']['llm']['tool_trace'])
        assert response['result']['status']=='comparison_complete',response['result']['status']
    print(json.dumps({'session_id':response['session_id'],'reply':response['reply'],'comparison':comparison['recommended'],'state':response['result']['status']},ensure_ascii=False))

if __name__=='__main__':main()
