const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const typescript=require('typescript');
const {bundle}=require('@remotion/bundler');
const {openBrowser,selectComposition,renderStill,renderMedia}=require('@remotion/renderer');

async function main(){
  const [output,browserExecutable,...propsFiles]=process.argv.slice(2);
  const entry=path.join(__dirname,'src/animation-seed/index.tsx');
  const motionFile=path.join(__dirname,'src/animation-seed/motion.ts');
  const js=typescript.transpileModule(fs.readFileSync(motionFile,'utf8'),{compilerOptions:{module:typescript.ModuleKind.CommonJS}}).outputText;
  const mod={exports:{}};
  new Function('exports','module',js)(mod.exports,mod);
  const {landingTime,position,presentationTime}=mod.exports;
  const initialProps=JSON.parse(fs.readFileSync(propsFiles[0],'utf8'));
  const sources=['src/animation-seed/index.tsx','src/animation-seed/GravitySeed.tsx','src/animation-seed/motion.ts'];
  const results={schema_version:'infofilm.animation-seed-result.v1',scene_id:initialProps.plan.id,visual_message:initialProps.plan.visual_message,complete:false,
    source_sha256:Object.fromEntries(sources.map(file=>[file,crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,file))).digest('hex')])),
    physics:[],renders:[],limits:'Hand-authored seed; manual cue times and no audio; no autonomous planner or external object creator executed.'};
  const serveUrl=await bundle({entryPoint:entry,outDir:path.join(output,'bundle'),webpackOverride:config=>({...config,cache:false})});
  const browser=await openBrowser('chrome',{browserExecutable});
  try{
    for(const propsFile of propsFiles){
      const props=JSON.parse(fs.readFileSync(propsFile,'utf8'));
      const {plan}=props;
      const name=path.basename(propsFile,'.props.json');
      const tf=landingTime(plan.physics);
      for(const fraction of [0,.25,.5,.75,1]){
        const a=position(plan.physics,tf*fraction),b=position(plan.physics,tf*fraction,true);
        assert.ok(Math.abs(a.y-b.y)<1e-9,'Launched and dropped vertical motion differs');
        assert.ok(Math.abs(b.x-plan.physics.drop_x_m)<1e-9,'Dropped ball moves horizontally');
        assert.ok(a.y>=0 && a.y<=plan.physics.release_height_m,'Ball leaves physical height bounds');
        results.physics.push({orientation:name,fraction,time:tf*fraction,launched:a,dropped:b});
      }
      assert.equal(position(plan.physics,tf).y,0);
      assert.equal(position(plan.physics,tf,true).y,0);
      const clock=plan.animation.comparison;
      assert.equal(presentationTime(clock.start_seconds,clock.start_seconds,clock.end_seconds,plan.physics),0);
      assert.equal(presentationTime(clock.end_seconds,clock.start_seconds,clock.end_seconds,plan.physics),tf);
      const composition=await selectComposition({serveUrl,id:'GravitySeed',inputProps:props,puppeteerInstance:browser});
      const samples=[];
      for(const [label,seconds] of [['setup',2],['flight',5.5],['first-landing',8.5],['comparison',14.5],['final',18]]){
        const frame=Math.round(seconds*plan.fps);
        const filename=name+'-'+label+'.png';
        // Measure actual browser text boxes and chart/caption separation.
        const logs=[];
        await renderStill({serveUrl,composition,inputProps:props,puppeteerInstance:browser,output:path.join(output,filename),frame,imageFormat:'png',overwrite:true,
          onBrowserLog:log=>logs.push(log.text),logLevel:'error'});
        const log=logs.findLast(x=>x.startsWith('GRAVITY_LAYOUT ') && JSON.parse(x.slice('GRAVITY_LAYOUT '.length)).frame===frame);
        assert.ok(log,'Missing browser layout measurements');
        const layout=JSON.parse(log.slice('GRAVITY_LAYOUT '.length));
        assert.deepEqual(layout.failures,[],'Gravity seed layout check failed: '+JSON.stringify(layout.failures));
        samples.push({frame,seconds,file:filename,layout});
      }
      console.log(JSON.stringify({state:'rendering',orientation:name,frames:composition.durationInFrames}));
      const started=Date.now();
      let last=-1;
      const measuredFrames=new Set(),layoutFailures=[];
      await renderMedia({serveUrl,composition,inputProps:props,puppeteerInstance:browser,outputLocation:path.join(output,name+'.mp4'),codec:'h264',crf:20,pixelFormat:'yuv420p',concurrency:3,muted:true,
        logLevel:'error',onBrowserLog:log=>{
          if(log.text.startsWith('GRAVITY_LAYOUT ')){
            const layout=JSON.parse(log.text.slice('GRAVITY_LAYOUT '.length)); measuredFrames.add(layout.frame);
            if(layout.failures.length)layoutFailures.push({frame:layout.frame,failures:layout.failures});
          }
        },
        onProgress:({progress})=>{const bucket=Math.floor(progress*10);if(bucket>last){last=bucket;console.log(JSON.stringify({orientation:name,progress:Math.round(progress*100)}));}}});
      results.renders.push({orientation:name,seconds:(Date.now()-started)/1000,width:composition.width,height:composition.height,frames:composition.durationInFrames,
        layout_measured_frames:measuredFrames.size,layout_failures:layoutFailures,file:name+'.mp4',samples});
      fs.writeFileSync(path.join(output,'render-results.json'),JSON.stringify(results,null,2));
      assert.equal(measuredFrames.size,composition.durationInFrames,'Not every frame was measured');
      assert.deepEqual(layoutFailures,[],'Layout errors during animation');
    }
    results.complete=true;
  }finally{
    fs.writeFileSync(path.join(output,'render-results.json'),JSON.stringify(results,null,2));
    await browser.close({silent:true});
  }
}
main().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
