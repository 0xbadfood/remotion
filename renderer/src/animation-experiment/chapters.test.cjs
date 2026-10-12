const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {test}=require('node:test'),ts=require('typescript');
const load=file=>{
  const source=ts.transpileModule(fs.readFileSync(path.join(__dirname,file),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
  const mod={exports:{}};new Function('exports','module',source)(mod.exports,mod);return mod.exports;
};
const {validateChapterPlan,beatAtFrame,chapterCheckpoints,scopedAnimationId,animationDurationFrames}=load('chapters.ts');
const {computeSceneTransforms}=load('motion.ts');
const local=(width,durationFrames)=>({schema_version:'infofilm.animation-motion.v1',scene_id:'lesson',visual_message:'Show a ball',
  fps:30,width:1280,height:720,duration_seconds:durationFrames/30,background:'#071b29',captions:[],checkpoints:[],
  objects:[{id:'same-ball',asset_id:'ball-svg',kind:'svg',width,height:.1,
    tracks:[{kind:'linear',start:0,end:durationFrames/30,from:[.2,.3],to:[.5,.3]}]}]});
const chapter=()=>({schema_version:'infofilm.animation-beat-chapter.v1',scene_id:'lesson',visual_message:'Show a ball',
  fps:30,width:1280,height:720,duration_seconds:2,duration_frames:60,background:'#071b29',captions:[{start:1,end:2,text:'One chapter caption'}],checkpoints:[],
  beats:[{beat_id:'B01',start_frame:0,duration_frames:30,plan:local(.1,30)},
    {beat_id:'B02',start_frame:30,duration_frames:30,plan:local(.2,30)}]});

test('beat ownership covers pauses and tail; the local clock starts exactly at its boundary',()=>{
  const plan=chapter();validateChapterPlan(plan);
  assert.equal(beatAtFrame(plan,-1),null);
  assert.deepEqual(beatAtFrame(plan,29),{beat:plan.beats[0],local_frame:29});
  assert.deepEqual(beatAtFrame(plan,30),{beat:plan.beats[1],local_frame:0});
  assert.equal(beatAtFrame(plan,59).local_frame,29);
  assert.equal(beatAtFrame(plan,60),null);
});

test('same source object and cached asset may have independent size and local motion',()=>{
  const plan=chapter(),before=JSON.stringify(plan),assets={'ball-svg':{pivot:[.5,.5]}};
  const first=beatAtFrame(plan,15),second=beatAtFrame(plan,45);
  const a=computeSceneTransforms(first.beat.plan,first.local_frame/30,assets)['same-ball'];
  const b=computeSceneTransforms(second.beat.plan,second.local_frame/30,assets)['same-ball'];
  assert.deepEqual(a,b);assert.equal(a.x,.35);
  assert.equal(first.beat.plan.objects[0].width*1280,128);
  assert.equal(second.beat.plan.objects[0].width*1280,256);
  assert.equal(JSON.stringify(plan),before);
});

test('rejects temporal gaps, overlaps, missing tail and inconsistent local canvas or duration',()=>{
  for(const mutate of [p=>p.beats[1].start_frame++,p=>p.beats[1].start_frame--,
    p=>p.duration_seconds+=1,p=>p.beats[1].plan.width=720,
    p=>p.beats[1].plan.duration_seconds=.9,p=>p.beats[1].beat_id='B01']){
    const plan=chapter();mutate(plan);assert.throws(()=>validateChapterPlan(plan));
  }
  // Exact local frames tolerate harmless floating-point multiplication noise.
  const plan=chapter();plan.beats[0].duration_frames=31;plan.beats[0].plan.duration_seconds=31/30;
  plan.beats[1].start_frame=31;plan.duration_seconds=61/30;plan.duration_frames=61;
  validateChapterPlan(plan);
});

test('explicit chapter frames and legacy rounding never add a floating-point tail frame',()=>{
  const plan=chapter();plan.beats=[{beat_id:'B01',start_frame:0,duration_frames:31,plan:local(.1,31)}];
  plan.duration_frames=31;plan.duration_seconds=31/30;
  assert.ok(plan.duration_seconds*30>31,'Fixture must exercise floating-point excess');
  validateChapterPlan(plan);assert.equal(animationDurationFrames(plan),31);
  assert.equal(animationDurationFrames(plan.beats[0].plan),31);
  assert.equal(animationDurationFrames({...plan.beats[0].plan,duration_seconds:31.1/30}),32);
  assert.throws(()=>validateChapterPlan({...plan,duration_frames:32}),/duration|cover/);
});

test('namespaces identical source IDs without collisions or unsafe marker characters',()=>{
  const a=scopedAnimationId('B:01','arrow #1'),b=scopedAnimationId('B:02','arrow #1');
  assert.notEqual(a,b);assert.match(a,/^[a-z0-9-]+$/);
  assert.notEqual(scopedAnimationId('a','b--c'),scopedAnimationId('a--b','c'));
  assert.equal(scopedAnimationId('B01','ball'),scopedAnimationId('B01','ball'));
});

test('automatic checkpoint stills inspect both adjacent visible frames in global time',()=>{
  const plan=chapter();plan.checkpoints=[{id:'chapter-mid',time:.5,reason:'Demonstration'}];
  const points=chapterCheckpoints(plan);assert.equal(points.length,3);
  assert.equal(points[1].frame,29);assert.equal(points[1].beat_id,'B01');
  assert.equal(points[2].frame,30);assert.equal(points[2].beat_id,'B02');
  assert.equal(points[2].time,1);
  const legacy=local(.1,30);legacy.checkpoints=plan.checkpoints;
  assert.equal(chapterCheckpoints(legacy),legacy.checkpoints);
});
