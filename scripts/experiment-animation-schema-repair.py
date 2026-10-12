#!/usr/bin/env python3
"""One local schema-repair trial; preserve the frontier model's planning decisions."""
from pathlib import Path
import argparse
import copy
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from infofilm.animation_experiment import component_errors
from infofilm.core import Store, read, write, digest
from infofilm.llm import Models


def repair_preservation_errors(original, repaired, requirement):
    # This trial permits only the two known contract corrections, not semantic changes.
    expected=copy.deepcopy(original)
    expected['scene_id']=requirement['id']
    for obj in expected.get('objects',[]):
        if obj.get('kind') in ('text','rect','line') and 'required_capabilities' not in obj:
            obj['required_capabilities']=[]
    return [] if repaired==expected else ['Repair changed planning content beyond scene_id and missing native required_capabilities']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--url',default='http://10.8.0.5:8001/v1')
    args=parser.parse_args()
    experiment=args.experiment.resolve();candidate=args.candidate.resolve()
    if not candidate.is_relative_to(experiment/'planning'/'components'):
        parser.error('Candidate must belong to this experiment components phase')
    requirement=read(experiment/'requirement.json');original=read(candidate)
    contracts=read(experiment/'phase-contracts.json');schema=contracts['components']
    expected_ids={'requirement_id':requirement['id'],'scene_id':requirement['id'],
                  'visual_message':requirement['visual_message']}
    errors=component_errors(original,requirement)
    root=experiment/'schema-repair'/('components-'+digest({'original':original,'schema':schema,'url':args.url})[:16])
    store=Store(root)
    try:
        write(root/'original.json',original)
        write(root/'inputs.json',{'source_candidate':str(candidate),'source_sha256':digest(candidate.read_bytes()),
            'schema':schema,'required_values':expected_ids,'code_errors':errors,'purpose':'schema_repair_only'})
        prompt=('Your job is SCHEMA REPAIR ONLY. You are not a planning or semantic reviewer. '
            'Repair the supplied component-plan JSON against the schema and authoritative validator errors. '
            'Return the entire corrected component-plan object, not a report or patch. '
            'Preserve every object, request, ID, order, teaching role, assumption, capability, description and planning decision. '
            'Only correct scene_id to the authoritative required value, and add required_capabilities: [] '
            'where it is missing on native text, rect or line objects. Native objects need no SVG capabilities. '
            'Do not rename component IDs or substitute the chapter ID for the required scene ID. '
            'Do not add positions, motion, facts, content or commentary.\nSCHEMA:\n'+json.dumps(schema)+
            '\nAUTHORITATIVE_REQUIRED_VALUES:\n'+json.dumps(expected_ids)+
            '\nVALIDATOR_ERRORS:\n'+json.dumps(errors)+'\nCOMPONENT_PLAN_TO_REPAIR:\n'+json.dumps(original))
        model=Models({'reviewer_url':args.url.rstrip('/')},store)
        repaired=model.call('reviewer','component-schema-repair',prompt,max_tokens=18000)
        write(root/'repaired.json',repaired)
        validation=component_errors(repaired,requirement)
        preservation=repair_preservation_errors(original,repaired,requirement)
        changed=[{'field':'scene_id','before':original.get('scene_id'),'after':repaired.get('scene_id')}]
        if isinstance(repaired.get('objects'),list):
            fixed={o.get('id'):o for o in repaired['objects'] if isinstance(o,dict)}
            changed += [{'field':'objects.'+o['id']+'.required_capabilities','before':'missing',
                'after':fixed.get(o['id'],{}).get('required_capabilities')} for o in original['objects']
                if o['kind'] in ('text','rect','line') and 'required_capabilities' not in o]
        result={'passed':not validation and not preservation,'code_errors':validation,
            'preservation_errors':preservation,'changes':changed,'original_object_count':len(original['objects']),
            'repaired_object_count':len(repaired.get('objects',[])),
            'source_json_sha256':digest(original),'repaired_json_sha256':digest(repaired),
            'purpose':'schema_repair_only','semantic_review':'not_run','model':model.identities.get('reviewer')}
        write(root/'evaluation.json',result)
        store.event('schema-repair','passed' if result['passed'] else 'failed',
            {k:v for k,v in result.items() if k not in ('changes',)})
        print('Repair artifacts: '+str(root),flush=True)
        return 0 if result['passed'] else 2
    finally:store.db.close()


if __name__=='__main__':sys.exit(main())
