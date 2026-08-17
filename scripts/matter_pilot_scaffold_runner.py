#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

# This public harness runs inside the exact private checkout. It imports the frozen
# private pilot helpers but never loads/calls the reconstruction model.
sys.path.insert(0, os.path.abspath('l1'))
sys.path.insert(0, os.path.abspath('l1/tools'))
import run_research_matter_reconstruction_pilot as p
from l1_processor.research_local_semantics import adapt_shared_local_semantics
from l1_processor.research_matter_reconstruction_pilot import build_reconstruction_payload, select_relevant_business_background


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--shared-replay',type=Path,required=True)
    ap.add_argument('--matter-gold',type=Path,required=True)
    ap.add_argument('--fixture-zip',type=Path,required=True)
    ap.add_argument('--speaker-report',type=Path,required=True)
    ap.add_argument('--business-report',type=Path,required=True)
    ap.add_argument('--public-key-b64',required=True)
    ap.add_argument('--encrypted-out',type=Path,required=True)
    ap.add_argument('--flat',type=Path,required=True)
    args=ap.parse_args()

    shared=p._load_jsonl(args.shared_replay)
    assert len(shared)==83
    messages_all=p._load_fixture(args.fixture_zip)
    by_key_all={m.stable_key:m for m in messages_all}
    slice_keys=[str(r['stable_key']) for r in shared]
    messages=tuple(by_key_all[k] for k in slice_keys)
    shared_by={str(r['stable_key']):r for r in shared}
    research={}
    for m in messages:
        row=shared_by[m.stable_key]
        assert str(row.get('raw_message') or '')==str(m.original_text or '')
        research[m.stable_key]=adapt_shared_local_semantics(p._mapping(m),{'units':row.get('shared_local_units') or []})

    # Candidate generation before Gold, identical to frozen private runner.
    pools, structural_keys, identifier_index=p._build_candidate_pools(messages,research)

    gold=p._load_json(args.matter_gold)
    certain, uncertain, matters=p._gold_membership(gold)
    by_key={m.stable_key:m for m in messages}
    selected, features=p._select_seeds(slice_keys,by_key=by_key,research=research,pools=pools,certain=certain,uncertain=uncertain,matters=matters)
    assert len(selected)==12

    group_names={str(by_key[k].group_name or '') for k in selected}; assert len(group_names)==1
    background=select_relevant_business_background(
        group_name=next(iter(group_names)),
        speaker_report=args.speaker_report.read_text(encoding='utf-8'),
        business_report=args.business_report.read_text(encoding='utf-8'))

    seeds=[]
    total_gold=available_gold=cross_candidates=0
    for i,key in enumerate(selected,1):
        payload,id_map=build_reconstruction_payload(source=p._message_source(by_key[key]),research_local_semantics=research[key],candidates=pools[key],background=background)
        matter=p._matter_for(key,certain)
        gold_same=(set(matters.get(matter,set()))-{key}) if matter else set()
        pool_keys={str(x['stable_key']) for x in pools[key]}
        available=sorted(gold_same & pool_keys)
        cross=[]
        for cand in pool_keys:
            if p._classify_candidate(key,cand,certain,uncertain)=='CROSS': cross.append(cand)
        total_gold+=len(gold_same); available_gold+=len(available); cross_candidates+=len(cross)
        candidate_gold_class={str(c['stable_key']):p._classify_candidate(key,str(c['stable_key']),certain,uncertain) for c in pools[key]}
        seeds.append({
            'seed_index':i,'stable_key':key,'selection_features':features[key],
            'source':p._message_source(by_key[key]),'research_local_semantics':research[key],
            'candidate_pool':pools[key],'candidate_gold_class':candidate_gold_class,
            'background_used':background,'model_payload':payload,
            'gold_scaffold':{'matter_gold_id':matter,'gold_same_total':len(gold_same),'gold_same_available':len(available),'gold_same_available_keys':available,'gold_same_missing_keys':sorted(gold_same-pool_keys),'cross_candidate_keys':sorted(cross)}
        })
    detail={'task_id':'RESEARCH-MATTER-RECONSTRUCTION-PILOT-001','kind':'PREMODEL_REVIEW_SCAFFOLD','selected_seed_stable_keys':selected,'seed_details':seeds}
    args.encrypted_out.parent.mkdir(parents=True,exist_ok=True)
    args.encrypted_out.write_text(p._encrypt_detail(detail,args.public_key_b64),encoding='ascii')
    flat={'seed_count':12,'retrieval_gold_same_total':total_gold,'retrieval_gold_same_available':available_gold,'cross_candidate_total':cross_candidates}
    args.flat.parent.mkdir(parents=True,exist_ok=True); args.flat.write_text(json.dumps(flat),encoding='utf-8')

if __name__=='__main__': main()
