// Run: node --test renderer/src/animation-experiment/motion.test.cjs
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const ts=require('typescript');
const compiled=ts.transpileModule(fs.readFileSync(path.join(__dirname,'motion.ts'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020},
}).outputText;
const compiledModule={exports:{}};
new Function('exports','module',compiled)(compiledModule.exports,compiledModule);
const {computeTransform}=compiledModule.exports;
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-12,`${actual} != ${expected}`);
const flight={kind:'projectile',start:2,end:10,origin:[.2,.3],fraction_per_meter:[.02,.02],gravity:10,vx:5,vy:10,physical_seconds:2};

test('hold preserves exact pivot position, constant opacity and rotation',()=>{
  const track={kind:'hold',start:1,end:5,x:.25,y:.7,opacity:.4,rotation:90};
  for(const seconds of [1,3,5,12])assert.deepEqual(computeTransform([track],seconds),{x:.25,y:.7,opacity:.4,rotation:90});
  assert.deepEqual(computeTransform([track],0),{x:.25,y:.7,opacity:0,rotation:90});
});

test('linear uses narration progress and clamps at endpoints',()=>{
  const track={kind:'linear',start:2,end:6,from:[.1,.2],to:[.5,.6],opacity:.8,rotation:-45};
  assert.deepEqual(computeTransform([track],1),{x:.1,y:.2,opacity:0,rotation:-45});
  const midpoint=computeTransform([track],4);
  near(midpoint.x,.3);near(midpoint.y,.4);
  assert.equal(midpoint.opacity,.8);assert.equal(midpoint.rotation,-45);
  assert.deepEqual(computeTransform([track],100),{x:.5,y:.6,opacity:.8,rotation:-45});
});

test('projectile maps physical seconds independently of narration runtime',()=>{
  // Four narration seconds mean one physical second: a 10 m/s upward
  // launch under 10 m/s² gravity reaches its 5 m apex at this point.
  const apex=computeTransform([flight],6);
  near(apex.x,.3);near(apex.y,.2);
  const returned=computeTransform([flight],10);
  near(returned.x,.4);near(returned.y,.3);
  assert.deepEqual(computeTransform([flight],100),returned);
});

test('horizontal speed cannot alter vertical acceleration',()=>{
  const launched={...flight,vy:0,vx:12},dropped={...flight,vy:0,vx:0};
  for(const seconds of [2,3,5,6,8,10]){
    const a=computeTransform([launched],seconds),b=computeTransform([dropped],seconds);
    assert.equal(a.y,b.y);
    assert.equal(b.x,.2);
  }
  near(computeTransform([launched],6).y,.4);
});

test('physical motion has no implicit viewport or ground clamp',()=>{
  const unrestricted={...flight,physical_seconds:4};
  near(computeTransform([unrestricted],10).y,1.1);
  // Placement validation owns safety. The primitive must not silently
  // distort a physically specified trajectory to make the frame fit.
});

test('later-started track owns the object including gaps and final state',()=>{
  const tracks=[{kind:'hold',start:0,end:1,x:.1,y:.1},
    {kind:'linear',start:3,end:5,from:[.2,.2],to:[.6,.4],opacity:.7},
    {kind:'hold',start:7,end:8,x:.5,y:.5,opacity:0}];
  assert.deepEqual(computeTransform(tracks,2),{x:.1,y:.1,opacity:1,rotation:0});
  assert.deepEqual(computeTransform(tracks,6),{x:.6,y:.4,opacity:.7,rotation:0});
  assert.deepEqual(computeTransform(tracks,7),{x:.5,y:.5,opacity:0,rotation:0});
  assert.deepEqual(computeTransform(tracks,99),{x:.5,y:.5,opacity:0,rotation:0});
});

test('ownership is determined by start time, with later array item winning a tie',()=>{
  const early={kind:'hold',start:0,end:12,x:.1,y:.1};
  const late={kind:'hold',start:5,end:6,x:.7,y:.4};
  assert.deepEqual(computeTransform([late,early],8),{x:.7,y:.4,opacity:1,rotation:0});
  assert.deepEqual(computeTransform([early,{...early,x:.9}],8),{x:.9,y:.1,opacity:1,rotation:0});
});

test('evaluation does not mutate the frozen plan or permit an empty track set',()=>{
  const tracks=[{...flight,origin:Object.freeze([.2,.3]),fraction_per_meter:Object.freeze([.02,.02])}];
  Object.freeze(tracks[0]);Object.freeze(tracks);
  const before=JSON.stringify(tracks);
  computeTransform(tracks,5);
  assert.equal(JSON.stringify(tracks),before);
  assert.throws(()=>computeTransform([],0),/at least one track/);
});

test('a carried cycle begins at its declared phase and stays synchronized without restarting',()=>{
  const track={kind:'hold',start:0,end:8,x:.5,y:.4,rotation_from:0,rotation_to:360,cycle_id:'orbit'};
  const cycle={id:'orbit',start:0,end:8,period_seconds:2,phase_offset:.375};
  near(computeTransform([track],0,[cycle]).rotation,135);
  near(computeTransform([track],.25,[cycle]).rotation,180);
  near(computeTransform([track],2,[cycle]).rotation,135);
  near(computeTransform([track],0,[{...cycle,phase_offset:undefined}]).rotation,0);
  for(const phase_offset of [-.1,1,Infinity,NaN])assert.throws(()=>computeTransform([track],0,[{...cycle,phase_offset}]),/phase_offset/);
});
