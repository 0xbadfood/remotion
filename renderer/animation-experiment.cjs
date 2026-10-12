const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {bundle}=require('@remotion/bundler');
const {openBrowser,selectComposition,renderStill,renderMedia}=require('@remotion/renderer');
const ts=require('typescript');
// The same authored validator serves the browser and CLI. Props remain data;
// generated plans never become executable JavaScript.
const chapterModule={exports:{}};
const chapterSource=ts.transpileModule(fs.readFileSync(path.join(__dirname,'src/animation-experiment/chapters.ts'),'utf8'),{
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020},
}).outputText;
new Function('exports','module',chapterSource)(chapterModule.exports,chapterModule);
const {validateChapterPlan,chapterCheckpoints}=chapterModule.exports;

async function main(){
  const [output,browserExecutable,propsFile,concurrencyArgument]=process.argv.slice(2);
  const concurrency=concurrencyArgument===undefined?3:Number(concurrencyArgument);
  if(!Number.isInteger(concurrency)||concurrency<1||concurrency>32)throw new Error('Render concurrency must be 1..32');
  if(!output||!browserExecutable||!propsFile)throw new Error('Usage: node animation-experiment.cjs OUTPUT BROWSER_EXECUTABLE PROPS_JSON');
  fs.mkdirSync(output,{recursive:true});
  const props=JSON.parse(fs.readFileSync(propsFile,'utf8'));
  const {plan}=props;
  if(!['infofilm.animation-motion.v1','infofilm.animation-beat-chapter.v1'].includes(plan.schema_version))throw new Error('Unsupported animation plan schema');
  for(const key of ['width','height','fps','duration_seconds'])if(!Number.isFinite(plan[key])||plan[key]<=0)throw new Error('Invalid animation metadata: '+key);
  if(plan.schema_version==='infofilm.animation-beat-chapter.v1')validateChapterPlan(plan);
  const sources=['src/animation-experiment/index.tsx','src/animation-experiment/AnimationExperiment.tsx','src/animation-experiment/motion.ts','src/animation-experiment/assets.ts','src/animation-experiment/chapters.ts','src/animation-experiment/LayoutMeasurement.tsx','animation-experiment.cjs'];
  const started=Date.now();
  const results={schema_version:'infofilm.animation-render-result.v1',scene_id:plan.scene_id,visual_message:plan.visual_message,complete:false,
    source_sha256:Object.fromEntries(sources.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,file))).digest('hex')])),
    props_sha256:crypto.createHash('sha256').update(fs.readFileSync(propsFile)).digest('hex'),plan_schema_version:plan.schema_version,
    beat_windows:plan.beats?.map(({beat_id,source_beat_id,start_frame,duration_frames})=>({beat_id,source_beat_id:source_beat_id??beat_id,start_frame,duration_frames}))??[],
    checkpoints:[],video:null,error:null};
  const save=()=>fs.writeFileSync(path.join(output,'render-results.json'),JSON.stringify(results,null,2));
  const layoutFile=path.join(output,'layout-frames.jsonl');
  fs.writeFileSync(layoutFile,'');
  let browser;
  try{
    const serveUrl=await bundle({entryPoint:path.join(__dirname,'src/animation-experiment/index.tsx'),outDir:path.join(output,'bundle'),webpackOverride:config=>({...config,cache:false})});
    browser=await openBrowser('chrome',{browserExecutable});
    const composition=await selectComposition({serveUrl,id:'AnimationExperiment',inputProps:props,puppeteerInstance:browser});
    for(const [index,checkpoint] of chapterCheckpoints(plan).entries()){
      const frame=Math.max(0,Math.min(composition.durationInFrames-1,checkpoint.frame??Math.round(checkpoint.time*plan.fps)));
      const filename=`checkpoint-${String(index+1).padStart(2,'0')}-${String(checkpoint.id).replace(/[^a-zA-Z0-9_-]/g,'_')}.png`;
      const measurements=[];
      await renderStill({serveUrl,composition,inputProps:props,puppeteerInstance:browser,output:path.join(output,filename),frame,imageFormat:'png',overwrite:true,logLevel:'error',onBrowserLog:log=>{
        if(log.text.startsWith('ANIMATION_LAYOUT '))measurements.push(JSON.parse(log.text.slice('ANIMATION_LAYOUT '.length)));
      }});
      const layout=measurements.findLast(value=>value.frame===frame);
      results.checkpoints.push({...checkpoint,frame,file:filename,layout:layout??null});
      save();
      console.log(JSON.stringify({state:'checkpoint',id:checkpoint.id,frame,layout_failures:layout?.failures??['Missing layout measurement']}));
    }
    const measuredFrames=new Set(),layoutFailures=[];
    let lastProgress=-1;
    const renderStarted=Date.now();
    const filename='animation.mp4';
    console.log(JSON.stringify({state:'rendering',scene_id:plan.scene_id,frames:composition.durationInFrames}));
    await renderMedia({serveUrl,composition,inputProps:props,puppeteerInstance:browser,outputLocation:path.join(output,filename),codec:'h264',crf:20,pixelFormat:'yuv420p',concurrency,
      muted:!props.audio_src,overwrite:true,logLevel:'error',onBrowserLog:log=>{
        if(!log.text.startsWith('ANIMATION_LAYOUT '))return;
        const layout=JSON.parse(log.text.slice('ANIMATION_LAYOUT '.length));
        measuredFrames.add(layout.frame);
        fs.appendFileSync(layoutFile,JSON.stringify(layout)+'\n');
        if(layout.failures.length)layoutFailures.push({frame:layout.frame,failures:layout.failures});
      },onProgress:({progress})=>{
        const bucket=Math.floor(progress*10);
        if(bucket>lastProgress){lastProgress=bucket;console.log(JSON.stringify({state:'rendering',progress:Math.round(progress*100)}));}
      }});
    results.video={file:filename,width:composition.width,height:composition.height,fps:composition.fps,frames:composition.durationInFrames,
      duration_seconds:composition.durationInFrames/composition.fps,render_seconds:(Date.now()-renderStarted)/1000,layout_measured_frames:measuredFrames.size,layout_failures:layoutFailures,
      layout_log:path.basename(layoutFile),audio_included:Boolean(props.audio_src)};
    const missing=composition.durationInFrames-measuredFrames.size;
    if(missing)throw new Error(`${missing} animation frames were not measured`);
    const checkpointFailures=results.checkpoints.filter(value=>!value.layout||value.layout.failures.length);
    if(layoutFailures.length||checkpointFailures.length)throw new Error(`Animation layout needs review: ${layoutFailures.length} video frame failures, ${checkpointFailures.length} checkpoint failures`);
    results.complete=true;
  }catch(error){results.error=String(error.stack||error);throw error;}
  finally{
    results.total_seconds=(Date.now()-started)/1000;
    save();
    if(browser)await browser.close({silent:true});
  }
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
